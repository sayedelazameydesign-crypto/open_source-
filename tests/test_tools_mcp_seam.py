"""The MCP seam inside os_tools: filter, argument parsing, default wiring.

``os_tools/mcp.py`` is the surface an external MCP client would drive, but only
the two paths the ReAct loop happens to use were covered. The rest are
decisions, not glue:

* ``ToolFilter`` decides which tools an agent may even see — if ``deny`` can
  lose to ``allow``, the filter is not a control;
* ``parse_tool_arguments`` absorbs whatever a model emits, and a malformed
  string must degrade to ``{"_raw": ...}`` rather than raise inside a run;
* ``build_default_registry`` is what a bare install actually gets, so its
  contents are the platform's capability claim.
"""

from __future__ import annotations

import pytest
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot
from os_tools.mcp import (
    LocalMCPServer,
    ToolFilter,
    build_default_registry,
    parse_tool_arguments,
)


def _specs(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n}} for n in names]


# ------------------------------------------------------------- ToolFilter ----
def test_filter_is_open_when_no_allow_list_is_given() -> None:
    filt = ToolFilter()
    assert filt.permits("anything") and filt.permits("python_execute")


def test_deny_beats_allow_rather_than_being_merged_with_it() -> None:
    # The whole point of a deny list is that it is not negotiable: a tool named
    # in both lists must stay hidden.
    filt = ToolFilter(allow={"read_file", "write_file"}, deny={"write_file"})
    assert filt.permits("read_file") is True
    assert filt.permits("write_file") is False
    assert filt.permits("unlisted") is False


def test_filter_specs_drops_every_spec_it_does_not_permit() -> None:
    filt = ToolFilter(allow={"a", "b", "c"}, deny={"b"})
    assert [s["function"]["name"] for s in filt.filter_specs(_specs("a", "b", "c", "d"))] == [
        "a",
        "c",
    ]


# ----------------------------------------------------- parse_tool_arguments --
def test_a_dict_passes_through_untouched() -> None:
    args = {"code": "print(1)"}
    assert parse_tool_arguments(args) is args


def test_a_json_object_string_is_parsed() -> None:
    assert parse_tool_arguments('{"code": "print(1)"}') == {"code": "print(1)"}


def test_malformed_json_degrades_to_a_raw_slot_instead_of_raising() -> None:
    # A model that emits broken JSON must not crash the run: the raw text is
    # kept under a reserved key so the tool can report it intelligibly.
    assert parse_tool_arguments("{not json") == {"_raw": "{not json"}


def test_valid_json_that_is_not_an_object_is_wrapped_not_discarded() -> None:
    assert parse_tool_arguments("[1, 2]") == {"_raw": [1, 2]}
    assert parse_tool_arguments("42") == {"_raw": 42}
    assert parse_tool_arguments('"just a string"') == {"_raw": "just a string"}


# --------------------------------------------------- build_default_registry --
def test_default_registry_always_carries_the_sandbox() -> None:
    names = build_default_registry().names()
    assert "python_execute" in names


def test_default_registry_adds_browser_tools_only_when_a_browser_is_supplied() -> None:
    bare = build_default_registry().names()
    assert not any(n.startswith("browser_") for n in bare)

    backend = FakeBrowser(
        pages={"https://example.org": PageSnapshot(url="https://example.org", title="Example")}
    )
    names = build_default_registry(browser_tool=BrowserTool(backend=backend)).names()
    assert "browser_navigate" in names


@pytest.mark.asyncio
async def test_an_injected_backend_is_what_the_registered_tool_calls() -> None:
    class _Backend:
        async def execute(self, code: str, **kwargs):
            from os_core.types import ToolResult, ToolStatus

            return ToolResult(
                tool_call_id="c",
                tool_name="python_execute",
                status=ToolStatus.SUCCESS,
                output="injected",
            )

    registry = build_default_registry(code_backend=_Backend())
    server = LocalMCPServer(registry=registry)
    result = await server.call_tool("python_execute", {"code": "print(1)"})
    # No subprocess ran: the injected backend answered, which is the whole
    # point of the seam (E2B/Firecracker drop in without touching the tools).
    assert result.output == "injected"


# ----------------------------------------------------------- LocalMCPServer --
@pytest.mark.asyncio
async def test_local_server_lists_what_the_registry_holds_and_describes_itself() -> None:
    registry = build_default_registry()
    server = LocalMCPServer(registry=registry, server_name="test-server", version="9.9")
    described = server.describe()
    assert described["server"] == "test-server" and described["version"] == "9.9"
    assert "python_execute" in described["tools"]
    assert {s["function"]["name"] for s in await server.list_tools()} == set(described["tools"])
