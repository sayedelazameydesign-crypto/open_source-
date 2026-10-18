"""os_skills stores: the three backends, held to one contract.

The manager tests drive ``InMemorySkillStore`` only, so the durable backends
held the part that decides whether a learned skill survives a restart — and,
in the Postgres case, whether ``rollback`` does anything at all. A store that
silently loses a version is worse than no store, because ``save_skill``
reports success either way.

The contract is asserted once against all three backends: save/get/list/find,
versioning on change, rollback of the previous card, and ``None`` for every
missing lookup. No live Postgres exists in CI, so the DBAPI connection is a
sqlite3 connection translating ``%s`` placeholders — the same double
``tests/test_distributed_store.py`` uses for the run store. The dialect
actually emitted (``ON CONFLICT ... EXCLUDED``) is common to both engines, so
the SQL under test is the real SQL; only the driver is exchanged.

One wrinkle worth knowing: ``Skill.signature`` is a **derived** property (a
hash of tools + step shape), not a field. It cannot be passed to the
constructor — pydantic ignores unknown kwargs silently — so lookups below use
the value the card actually computed.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest
from os_tools.local_ai import LocalAIEngine

from os_skills import (
    InMemorySkillStore,
    PostgresSkillStore,
    Skill,
    SkillManager,
    SQLiteSkillStore,
    skill_store_from_url,
)

_BACKENDS = ("memory", "sqlite", "postgres")


def _skill(
    skill_id: str = "sk_sum", *, tools: tuple[str, ...] = ("python_execute",), **kw
) -> Skill:
    """One valid card. ``tools`` varies the derived signature between cards."""
    fields: dict[str, object] = {
        "skill_id": skill_id,
        "name": f"skill {skill_id}",
        "description": "sum a range of integers",
        "tools": list(tools),
    }
    fields.update(kw)
    return Skill(**fields)  # type: ignore[arg-type]


class _FakePgConnection:
    """A DBAPI connection that speaks the Postgres dialect into sqlite3."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")

    def execute(self, sql, params=()):
        return self._conn.execute(sql.replace("%s", "?"), tuple(params))

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _store(kind: str, tmp_path):
    if kind == "memory":
        return InMemorySkillStore()
    if kind == "sqlite":
        return SQLiteSkillStore(tmp_path / "skills.db")
    return PostgresSkillStore("postgres://fake/cela", connection_factory=_FakePgConnection)


# ------------------------------------------------------- the shared contract --
@pytest.mark.parametrize("kind", _BACKENDS)
def test_save_get_list_and_delete(kind: str, tmp_path) -> None:
    store = _store(kind, tmp_path)
    store.save_skill(_skill("a", tools=("python_execute",)))
    store.save_skill(_skill("b", tools=("browser_navigate",)))

    assert store.get_skill("a") is not None
    assert {s.skill_id for s in store.list_skills()} == {"a", "b"}
    assert store.delete_skill("a") is True
    assert store.delete_skill("a") is False
    store.close()


@pytest.mark.parametrize("kind", _BACKENDS)
def test_find_by_signature_uses_the_derived_hash(kind: str, tmp_path) -> None:
    store = _store(kind, tmp_path)
    card = _skill("a", tools=("python_execute",))
    other = _skill("b", tools=("browser_navigate",))
    store.save_skill(card)
    store.save_skill(other)
    assert card.signature != other.signature, "distinct tool sets must not collide"

    found = store.find_by_signature(card.signature)
    assert found is not None and found.skill_id == "a"
    assert store.find_by_signature("no-such-signature") is None
    store.close()


@pytest.mark.parametrize("kind", _BACKENDS)
def test_missing_lookups_return_none_not_a_blank_card(kind: str, tmp_path) -> None:
    store = _store(kind, tmp_path)
    assert store.get_skill("absent") is None
    assert store.list_skills() == []
    assert store.list_versions("absent") == []
    assert store.rollback("absent") is None
    store.close()


@pytest.mark.parametrize("kind", _BACKENDS)
def test_rollback_restores_the_card_as_it_was_before_the_change(kind: str, tmp_path) -> None:
    store = _store(kind, tmp_path)
    store.save_skill(_skill())
    store.save_skill(_skill(success_count=4))

    versions = store.list_versions("sk_sum")
    assert [v.success_count for v in versions] == [1], "the overwritten card is the version"

    restored = store.rollback("sk_sum")
    assert restored is not None and restored.success_count == 1
    assert store.get_skill("sk_sum").success_count == 1, "rollback rewrites the live row"
    assert store.rollback("sk_sum") is None, "the popped version is gone, not replayable"
    store.close()


# ------------------------------------------------------------ sqlite itself --
def test_sqlite_roundtrip_survives_a_reopen(tmp_path) -> None:
    store = SQLiteSkillStore(tmp_path / "nested" / "skills.db")
    store.save_skill(_skill("a", tools=("python_execute",)))
    store.close()

    reopened = SQLiteSkillStore(tmp_path / "nested" / "skills.db")
    assert reopened.get_skill("a") is not None
    assert reopened.find_by_signature(reopened.get_skill("a").signature) is not None
    reopened.close()


def test_sqlite_records_a_version_even_when_nothing_changed(tmp_path) -> None:
    """Documents a sharp edge rather than the intent.

    ``updated_at`` is part of the persisted JSON and is bumped *before* the
    comparison, so every save looks like a change to this backend — including a
    no-op re-save. InMemory compares before bumping and therefore disagrees.
    Harmless for correctness (rollback still restores an equivalent card) but it
    grows ``skill_versions`` on every write, so it is recorded here instead of
    being quietly relied upon.
    """
    store = SQLiteSkillStore(tmp_path / "skills.db")
    card = _skill()
    store.save_skill(card)
    store.save_skill(card)
    assert len(store.list_versions("sk_sum")) == 1
    store.close()


def test_sqlite_accepts_an_in_memory_url_and_a_bare_path(tmp_path) -> None:
    target = tmp_path / "bare.db"
    for store in (
        skill_store_from_url(":memory:"),
        skill_store_from_url(str(target)),
        skill_store_from_url(f"sqlite:///{target}"),
    ):
        assert isinstance(store, SQLiteSkillStore)
        store.save_skill(_skill())
        assert store.get_skill("sk_sum") is not None
        store.close()


# ---------------------------------------------------------- postgres itself --
def test_postgres_without_a_driver_demands_it_loudly(monkeypatch) -> None:
    # A store that quietly degraded to "nothing persisted" would cost a week of
    # learned skills before anybody noticed, so the missing driver raises. The
    # driver is made unimportable rather than assumed absent: the optional
    # ``distributed`` extra is installed on some machines, and a test that
    # skips there leaves the error path unverified everywhere.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    store = PostgresSkillStore("postgres://localhost/cela")
    with pytest.raises(RuntimeError, match="distributed"):
        store.list_skills()


def test_postgres_close_without_ever_connecting_is_a_no_op() -> None:
    PostgresSkillStore("postgres://localhost/cela").close()


# ------------------------------------------------------------- url selection --
def test_skill_store_from_url_defaults_to_memory() -> None:
    for url in (None, "", "memory://"):
        assert isinstance(skill_store_from_url(url), InMemorySkillStore)


def test_skill_store_from_url_routes_postgres_without_connecting() -> None:
    # The URL is parsed, not dialled: constructing the store must not touch the
    # network or the driver, so a misconfigured URL fails at first use — with a
    # driver error naming the extra to install — not at import time.
    assert isinstance(skill_store_from_url("postgres://localhost/cela"), PostgresSkillStore)
    assert isinstance(skill_store_from_url("postgresql://localhost/cela"), PostgresSkillStore)


def test_skill_store_from_url_rejects_two_slash_sqlite() -> None:
    with pytest.raises(ValueError, match="three slashes"):
        skill_store_from_url("sqlite://relative.db")


def test_skill_store_from_url_rejects_an_unknown_backend() -> None:
    with pytest.raises(RuntimeError, match="unsupported skill store backend"):
        skill_store_from_url("redis://cache/skills")


# ------------------------------------------- manager, over durable backends --
@pytest.mark.parametrize("kind", _BACKENDS)
def test_manager_rollback_keeps_live_grants_on_every_backend(kind: str, tmp_path) -> None:
    """The security half of rollback must not depend on the store choice.

    ``SkillManager.rollback`` re-attaches the live permission grants to the
    restored card; it can only do that if the store handed back a previous
    version. With a backend whose ``rollback`` always returned ``None``, the
    grant ledger would silently follow the restored card instead.
    """
    store = _store(kind, tmp_path)
    manager = SkillManager(store, LocalAIEngine())
    skill = manager.learn_from_run(_completed_run())
    assert skill is not None

    updated = manager.expand_permissions(skill.skill_id, ["browser_navigate"], approved=True)
    grant_id = updated.permission_grants[0].grant_id

    updated.description = "behaviour change"
    manager.store.save_skill(updated)
    restored = manager.rollback(skill.skill_id)

    assert restored is not None, "a durable store must be able to hand back a version"
    assert restored.description != "behaviour change"
    assert [g.grant_id for g in restored.permission_grants] == [grant_id]
    store.close()


def _completed_run() -> object:
    from os_core.state import RunState, RunStatus
    from os_core.types import PlanStep, StepStatus, ToolResult, ToolStatus

    state = RunState(task="Sum the integers from 0 to 100", mode="react")
    step = PlanStep(id="s1", title="Compute the sum", tool="python_execute", status=StepStatus.DONE)
    step.result = ToolResult(
        tool_call_id="c1", tool_name="python_execute", status=ToolStatus.SUCCESS, output="5050"
    )
    state.steps.append(step)
    state.record_result(step.result)
    state.mark_finished(RunStatus.COMPLETED, answer="5050")
    return state
