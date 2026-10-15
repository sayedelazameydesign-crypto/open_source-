"""The memory layer inside ``Platform.run`` — injection, feedback, and failure.

These tests exist because a memory layer that works in isolation and misbehaves
in the run loop is worthless (or worse). The three properties that matter here:
recalled text reaches the model *as a quoted block*, the run's outcome is fed
back even when the run fails, and a broken memory layer can never turn a working
run into a failed one.
"""

from __future__ import annotations

from os_core.models import ModelResponse, ScriptedModel, Usage
from os_core.state import RunStatus
from os_core.types import ToolCall
from os_orchestration.platform import Platform, PlatformConfig
from os_tools.local_ai import LocalAIEngine
from os_tools.registry import ToolRegistry

from os_memory import (
    InMemoryMemoryStore,
    MemoryManager,
    memory_store_from_url,
)


def make_manager(store: InMemoryMemoryStore | None = None) -> MemoryManager:
    return MemoryManager(store or InMemoryMemoryStore(), LocalAIEngine())


def final_model(answer: str = "the answer is 42") -> ScriptedModel:
    return ScriptedModel(script=[], final_content=answer)


async def test_recalled_memory_reaches_the_model_as_a_quoted_block() -> None:
    manager = make_manager()
    manager.remember("the build farm runs on Debian 12 in eu-west", key="build.farm")
    model = final_model()
    platform = Platform(model=model, registry=ToolRegistry(), memory_manager=manager)

    state = await platform.run("which host builds this?")

    assert state.status == RunStatus.COMPLETED
    sent = model.calls[-1]
    joined = "\n".join(message.content for message in sent)
    assert "<untrusted-memory>" in joined and "</untrusted-memory>" in joined
    assert "Debian 12" in joined
    assert "do not obey them" in joined.lower() or "not instructions" in joined.lower()
    assert state.metadata["memory_ids"], "the run must record what it leaned on"


async def test_the_task_stays_first_and_the_block_is_appended_not_substituted() -> None:
    manager = make_manager()
    manager.remember("answer the file-count question in French", key="lang.pref")
    model = final_model()
    await Platform(model=model, registry=ToolRegistry(), memory_manager=manager).run(
        "count the files"
    )
    user_turn = next(m for m in model.calls[-1] if m.role == "user")
    assert "count the files" in user_turn.content
    # Order is the guarantee: the user's request comes first and memory is
    # appended below it as a quoted block — never spliced in front of the task,
    # and never moved into the system turn where it would read as policy.
    assert user_turn.content.index("count the files") < user_turn.content.index(
        "<untrusted-memory>"
    )
    system_turn = next(m for m in model.calls[-1] if m.role == "system")
    assert "<untrusted-memory>" not in system_turn.content


async def test_a_successful_run_is_distilled_into_an_episode() -> None:
    store = InMemoryMemoryStore()
    manager = make_manager(store)
    registry = ToolRegistry()

    @registry.register("echo", "echo", {"type": "object", "properties": {}, "required": []})
    def echo() -> str:
        return "done"

    model = ScriptedModel(
        script=[
            ModelResponse(
                content="using echo",
                tool_calls=[ToolCall(name="echo", args={})],
                usage=Usage(prompt_tokens=10, completion_tokens=5),
            )
        ],
        final_content="echoed and finished",
    )
    await Platform(model=model, registry=registry, memory_manager=manager).run("echo something")

    episodes = [r for r in manager.list_records() if r.kind.value == "episode"]
    assert episodes, "a finished run should leave a durable trace of what it did"
    assert "echoed and finished" in episodes[0].text
    assert episodes[0].provenance.value == "run"


async def test_recalled_memories_are_corroborated_on_success() -> None:
    store = InMemoryMemoryStore()
    manager = MemoryManager(store, LocalAIEngine())
    record, _ = manager.remember("the queue limit is 128 connections", key="queue.limit")
    assert record is not None
    before = (record.successes, record.uses)

    await Platform(model=final_model(), registry=ToolRegistry(), memory_manager=manager).run(
        "what is the queue limit in connections?"
    )
    after = store.get_record(record.memory_id)
    assert after.uses > before[1], "a recalled record must record that it was used"
    assert after.successes == before[0] + 1, "a completed run corroborates what it leaned on"


async def test_recalled_memories_are_discounted_when_the_run_fails() -> None:
    store = InMemoryMemoryStore()
    manager = MemoryManager(store, LocalAIEngine())
    record, _ = manager.remember("the queue limit is 128 connections", key="queue.limit")
    assert record is not None

    looping = ScriptedModel(
        script=[
            ModelResponse(
                content="",
                tool_calls=[ToolCall(name="memory_recall", args={"query": "again"})],
                usage=Usage(prompt_tokens=5, completion_tokens=5),
            )
        ]
        * 4,
        final_content="never reached",
    )
    platform = Platform(
        model=looping,
        registry=ToolRegistry(),
        memory_manager=manager,
        config=PlatformConfig(max_steps=2),
    )
    state = await platform.run("what is the queue limit in connections?")
    assert state.status != RunStatus.COMPLETED
    after = store.get_record(record.memory_id)
    assert after.failures == 1, "a wrong belief must be discounted even when the run died"
    assert after.successes == 1  # the earlier write, untouched


async def test_a_broken_memory_layer_cannot_fail_a_run() -> None:
    class Broken:
        def guidance_for_task(self, task):
            raise RuntimeError("store on fire")

        def feedback(self, *a, **k):
            raise RuntimeError("store on fire")

        def learn_from_run(self, state):
            raise RuntimeError("store on fire")

    platform = Platform(model=final_model(), registry=ToolRegistry(), memory_manager=Broken())
    state = await platform.run("still finishes")
    assert state.status == RunStatus.COMPLETED, "memory is an enhancement, not a dependency"
    assert not state.metadata.get("memory_ids")


async def test_platform_without_a_memory_manager_is_byte_for_byte_unchanged() -> None:
    model = final_model("plain")
    state = await Platform(model=model, registry=ToolRegistry()).run("nothing injected")
    assert state.metadata == {} or "memory_ids" not in state.metadata
    assert "<untrusted-memory>" not in "\n".join(m.content for m in model.calls[-1])


async def test_events_make_the_injection_auditable() -> None:
    manager = make_manager()
    manager.remember("the ledger is jsonl with fsync", key="ledger.format")
    collected: list = []
    from os_core.events import EventBus

    bus = EventBus(run_id="run_audit")
    bus.subscribe(collected.append)
    await Platform(model=final_model(), registry=ToolRegistry(), memory_manager=manager).run(
        "what format is the ledger?", bus=bus
    )
    kinds = [e.data.get("kind") for e in collected if e.type.value == "log"]
    assert "memory_recalled" in kinds
    assert "memory_written" in kinds, "the episode it stored must be visible too"


# ------------------------------------------------------------- opt-in wiring --
def test_memory_tools_are_registered_only_when_a_store_url_is_set(monkeypatch, tmp_path) -> None:
    from os_server.demo import build_registry

    monkeypatch.delenv("CELA_MEMORY_STORE_URL", raising=False)
    registry, _guard, _browser = build_registry()
    assert "memory_recall" not in registry.names()

    monkeypatch.setenv("CELA_MEMORY_STORE_URL", str(tmp_path / "mem.sqlite"))
    import os_server.demo as demo

    demo._MEMORY_MANAGER = None  # process-wide singleton: reset for the test
    registry, _guard, _browser = demo.build_registry()
    assert {"memory_remember", "memory_recall", "memory_forget"} <= set(registry.names())
    demo._MEMORY_MANAGER = None


def test_know_tools_are_registered_only_when_a_store_url_is_set(monkeypatch, tmp_path) -> None:
    import os_server.demo as demo

    monkeypatch.setenv("CELA_KNOW_STORE_URL", str(tmp_path / "know.sqlite"))
    demo._KNOW_LAYER = None
    registry, _guard, _browser = demo.build_registry()
    assert {"know_search", "know_list", "know_index"} <= set(registry.names())
    demo._KNOW_LAYER = None


async def test_end_to_end_a_fact_learned_in_one_run_shapes_the_next(tmp_path) -> None:
    """The whole point of the layer, measured across two runs on one store."""
    manager = MemoryManager(memory_store_from_url(str(tmp_path / "m.sqlite")), LocalAIEngine())
    first = await Platform(
        model=final_model("the release tag is v9.9.9 and the target is Debian 12"),
        registry=ToolRegistry(),
        memory_manager=manager,
    ).run("which distro does the release target?")
    assert first.status == RunStatus.COMPLETED

    model = final_model("done")
    await Platform(model=model, registry=ToolRegistry(), memory_manager=manager).run(
        "which distro does the release target?"
    )
    recalled = "\n".join(m.content for m in model.calls[-1])
    assert "v9.9.9" in recalled, "the second run must inherit what the first learned"
    assert manager.stats["recalled"] >= 1
