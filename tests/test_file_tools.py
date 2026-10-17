"""Read-only exploration tools — confinement, denylist, redaction, caps."""

from __future__ import annotations

import pytest
from os_core.types import ToolCall
from os_tools.files import file_reader_tool, grep_search_tool, resolve_under
from os_tools.registry import ToolRegistry


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text(
        "# Notes\n\nThe gateway runs on port 8080.\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (tmp_path / "token.pem").write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
    (tmp_path / "leak.txt").write_text("password = hunter2-correct-horse\n", encoding="utf-8")
    return tmp_path


async def test_file_reader_returns_a_window(workspace) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(workspace))
    result = await registry.execute(ToolCall(name="file_reader", args={"path": "src/app.py"}))
    assert result.ok, result.error
    assert "def main()" in result.output


async def test_file_reader_confines_paths(workspace) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(workspace))
    result = await registry.execute(
        ToolCall(name="file_reader", args={"path": "../../../etc/passwd"})
    )
    assert not result.ok
    assert "escapes" in (result.error or "")


async def test_file_reader_refuses_secret_files_but_allows_templates(workspace) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(workspace))
    refused = await registry.execute(ToolCall(name="file_reader", args={"path": ".env"}))
    assert not refused.ok and "sensitive" in (refused.error or "")

    key = await registry.execute(ToolCall(name="file_reader", args={"path": "token.pem"}))
    assert not key.ok

    template = await registry.execute(ToolCall(name="file_reader", args={"path": ".env.example"}))
    assert template.ok, template.error


async def test_file_reader_scrubs_credentials_in_normal_files(workspace) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(workspace))
    result = await registry.execute(ToolCall(name="file_reader", args={"path": "leak.txt"}))
    assert result.ok
    assert "hunter2" not in result.output
    assert "REDACTED" in result.output


async def test_grep_search_finds_lines_and_skips_secrets(workspace) -> None:
    registry = ToolRegistry()
    registry.add(grep_search_tool(workspace))
    result = await registry.execute(
        ToolCall(name="grep_search", args={"pattern": "gateway|API_KEY"})
    )
    assert result.ok, result.error
    assert "notes.md" in result.output  # the gateway line is found
    assert "super-secret-value" not in result.output  # .env never enters results


async def test_grep_search_respects_glob_and_cap(workspace) -> None:
    registry = ToolRegistry()
    registry.add(grep_search_tool(workspace, max_hits=1))
    result = await registry.execute(
        ToolCall(name="grep_search", args={"pattern": "def|return|gateway", "glob": "*.py"})
    )
    assert result.ok
    assert "src/app.py" in result.output
    assert "notes.md" not in result.output


async def test_grep_search_rejects_invalid_regex(workspace) -> None:
    registry = ToolRegistry()
    registry.add(grep_search_tool(workspace))
    result = await registry.execute(ToolCall(name="grep_search", args={"pattern": "(unclosed"}))
    assert not result.ok
    assert "invalid regex" in (result.error or "")


def test_resolve_under_allows_root_itself(workspace) -> None:
    assert resolve_under(workspace, ".") == workspace.resolve()
