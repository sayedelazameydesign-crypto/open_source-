"""``distill_run`` — turn a finished run into one compressed episode.

The idea is borrowed from ``claude-mem`` (capturing what an agent did, compacting
it, replaying the relevant part later) and *only* the idea: that project is a
TypeScript hook plugin for coding assistants, not a Python library we can import.
What we need is the compaction step, and it is done here with no model call at
all — the run state already contains the evidence, so an episode can be produced
deterministically, offline, inside the test suite.
"""

from __future__ import annotations

from typing import Any

from os_core.redact import truncate
from os_core.state import RunState

_ANSWER_CHARS = 320
_TASK_CHARS = 160


def tool_trace(state: RunState, *, limit: int = 6) -> list[str]:
    """``python_execute→ok`` shaped trail of what the run actually called."""
    trail: list[str] = []
    for result in state.results[-limit:]:
        trail.append(f"{result.tool_name}→{result.status.value}")
    return trail


def _entities(ai: Any, text: str, *, cap: int = 5) -> list[str]:
    """Best-effort entity names from the local NLP engine — never fatal.

    ``LocalAIEngine.extract_entities`` is optional here on purpose: a distilled
    episode is still useful if the entity pass is unavailable, and a memory
    layer that raises in the middle of a run's teardown would cost more than the
    detail it recovers.
    """
    if ai is None:
        return []
    try:
        found = ai.extract_entities(text)
    except Exception:
        return []
    raw = found.get("entities") if isinstance(found, dict) else None
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            value = item.get("text") or item.get("value") or item.get("name")
        else:
            value = item
        if value is not None and str(value).strip() and str(value) not in names:
            names.append(str(value).strip())
        if len(names) >= cap:
            break
    return names


def distill_run(state: RunState, *, ai: Any = None, max_chars: int = 900) -> str:
    """One-paragraph episode: what was asked, what was done, what came back.

    Returns ``""`` for a run that never produced an answer, because a failed
    attempt remembered as a fact is how a platform starts believing itself.
    """
    if not state.final_answer.strip():
        return ""
    parts = [
        f"Task: {truncate(state.task, _TASK_CHARS, note='…')}",
        f"Mode: {state.mode}, finished {state.status.value} "
        f"in {state.steps_used} steps ({state.tokens_used} tokens, {state.replans} replans).",
    ]
    trace = tool_trace(state)
    if trace:
        parts.append(f"Tools: {', '.join(trace)}.")
    if state.artifacts:
        parts.append("Artifacts: " + ", ".join(a.filename for a in state.artifacts[:5]) + ".")
    entities = _entities(ai, state.task)
    if entities:
        parts.append(f"Entities: {', '.join(entities)}.")
    parts.append(f"Answer: {truncate(state.final_answer, _ANSWER_CHARS, note='…')}")
    return truncate(" ".join(parts), max_chars, note="…")


__all__ = ["distill_run", "tool_trace"]
