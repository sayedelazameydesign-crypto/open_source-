"""C6 acceptance-script guard: never write to the production database.

``scripts/test_postgres_persistence.py`` takes a DSN from ``--dsn`` or
``CELA_TEST_POSTGRES_DSN`` only — never ``CELA_RUN_STORE_URL`` — and refuses a
target that resolves to the same database as the production reference.

Supabase exposes one project through two different DSNs, and both shapes are
handled here:

* direct: ``postgres@db.<ref>.supabase.co:5432/postgres`` (ref in the **host**);
* session pooler: ``postgres.<ref>@...pooler.supabase.com:5432/postgres``
  (ref in the **username**; the host is shared by every project in a region).

So a pooler→direct pair for one project must be refused, two projects on the
shared pooler host must be allowed, and — where no project ref can be proven —
the same host/port/database must be refused **even with different usernames**.

The guard also fails closed when there is no production reference at all: an
absent reference is not evidence of separation. ``--no-production-reference``
is the explicit opt-out and labels the run unverified.

These tests load the script as a module (it is an operator entry point, not an
importable package) and exercise the identity logic and DSN resolution.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_postgres_persistence.py"

# Session pooler: ref lives in the username, host is shared per region.
PRODUCTION = (
    "postgresql://postgres.aaaa:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
SECOND_PROJECT_SAME_POOLER = (
    "postgresql://postgres.bbbb:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
# Direct connections: username is plain `postgres`, ref lives in the host.
PRODUCTION_DIRECT = "postgresql://postgres:pw@db.aaaa.supabase.co:5432/postgres"
SECOND_PROJECT_DIRECT = "postgresql://postgres:pw@db.bbbb.supabase.co:5432/postgres"
RENDER_TEST_DB = "postgresql://cela_test:pw@dpg-abc:5432/cela_test"


def _args(**overrides):
    """The subset of CLI arguments ``_resolve_dsn`` reads."""
    values = {
        "dsn": None,
        "production_dsn": None,
        "no_production_reference": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("c6_postgres_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELA_RUN_STORE_URL", raising=False)
    monkeypatch.delenv("CELA_TEST_POSTGRES_DSN", raising=False)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_identity_never_exposes_userinfo(script) -> None:
    identity = script._db_identity(PRODUCTION)
    assert identity == "aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    assert "pw" not in identity
    assert "aaaa" not in identity


@pytest.mark.parametrize(
    ("dsn", "ref"),
    [
        (PRODUCTION, "aaaa"),
        (SECOND_PROJECT_SAME_POOLER, "bbbb"),
        (PRODUCTION_DIRECT, "aaaa"),
        (PRODUCTION_DIRECT.replace("db.", "DB."), "aaaa"),
        (RENDER_TEST_DB, None),
        ("postgresql://postgres:pw@db.example.com:5432/postgres", None),
    ],
)
def test_project_ref_is_read_from_the_host_or_the_username(script, dsn, ref) -> None:
    assert script._supabase_project_ref(dsn) == ref


# --------------------------------------------------------------------------
# same-database detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # One project, two advertised endpoints — the same database.
        (PRODUCTION, PRODUCTION_DIRECT),
        (PRODUCTION_DIRECT, PRODUCTION),
        # Identical strings.
        (PRODUCTION, PRODUCTION),
        # No provable ref: same endpoint, different roles, still one database.
        (
            "postgresql://cela_test:pw@db.internal:5432/postgres",
            "postgresql://postgres:pw@db.internal:5432/postgres",
        ),
        # Two projects sharing the shared pooler host is *not* this case; but two
        # DSNs to one non-Supabase host with different users must be refused.
        (
            "postgresql://postgres.aaaa:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
            "postgresql://other:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres",
        ),
    ],
)
def test_same_database_is_detected(script, left, right) -> None:
    assert script._same_database(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Two Supabase projects behind one regional pooler host.
        (PRODUCTION, SECOND_PROJECT_SAME_POOLER),
        # Two projects, direct form.
        (PRODUCTION_DIRECT, SECOND_PROJECT_DIRECT),
        # Mixed shapes across different projects.
        (PRODUCTION, SECOND_PROJECT_DIRECT),
        # A different database name on the same server is a different target.
        ("postgresql://u:p@db.internal:5432/cela_test", "postgresql://u:p@db.internal:5432/postgres"),
        # Unrelated hosts.
        (PRODUCTION, RENDER_TEST_DB),
    ],
)
def test_different_databases_are_allowed(script, left, right) -> None:
    assert not script._same_database(left, right)


# --------------------------------------------------------------------------
# DSN resolution
# --------------------------------------------------------------------------


def test_dsn_is_not_read_from_the_production_variable(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    dsn, error = script._resolve_dsn(_args())
    assert dsn == ""
    assert "CELA_TEST_POSTGRES_DSN" in error


def test_production_variable_alone_cannot_select_the_target(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    monkeypatch.setenv("CELA_TEST_POSTGRES_DSN", PRODUCTION)
    dsn, error = script._resolve_dsn(_args())
    assert dsn == ""
    assert "refusing to write" in error
    # The refusal names the endpoint, never the credentials or the project ref.
    assert "postgres.aaaa" not in error and "pw" not in error


def test_pooler_production_vs_direct_target_for_the_same_project_is_refused(
    script, monkeypatch
) -> None:
    """The original gap: the ref used to be read from the username only."""
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    dsn, error = script._resolve_dsn(_args(dsn=PRODUCTION_DIRECT))
    assert dsn == ""
    assert "refusing to write" in error


def test_direct_production_vs_pooler_target_for_the_same_project_is_refused(
    script, monkeypatch
) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION_DIRECT)
    dsn, error = script._resolve_dsn(_args(dsn=PRODUCTION))
    assert dsn == ""
    assert "refusing to write" in error


def test_same_endpoint_with_a_different_username_is_refused(script) -> None:
    """No provable ref: a different role must not read as a different database."""
    production = "postgresql://postgres:pw@db.internal:5432/postgres"
    dsn, error = script._resolve_dsn(
        _args(dsn="postgresql://cela_test:pw@db.internal:5432/postgres", production_dsn=production)
    )
    assert dsn == ""
    assert "refusing to write" in error


def test_a_separate_test_project_is_accepted(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    dsn, error = script._resolve_dsn(_args(dsn=SECOND_PROJECT_SAME_POOLER))
    assert error == ""
    assert dsn == SECOND_PROJECT_SAME_POOLER


def test_direct_separate_project_is_accepted(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    dsn, error = script._resolve_dsn(_args(dsn=SECOND_PROJECT_DIRECT))
    assert error == ""
    assert dsn == SECOND_PROJECT_DIRECT


def test_production_reference_may_come_from_the_flag(script) -> None:
    """A test shell without CELA_RUN_STORE_URL can still prove separation."""
    dsn, error = script._resolve_dsn(
        _args(dsn=SECOND_PROJECT_SAME_POOLER, production_dsn=PRODUCTION)
    )
    assert error == "" and dsn == SECOND_PROJECT_SAME_POOLER


def test_missing_production_reference_fails_closed(script) -> None:
    dsn, error = script._resolve_dsn(_args(dsn=RENDER_TEST_DB))
    assert dsn == ""
    assert "no production reference" in error
    assert "--production-dsn" in error and "--no-production-reference" in error


def test_missing_reference_is_never_treated_as_separation(script) -> None:
    """The opt-out must be explicit; the default must not assume isolation."""
    refused, error = script._resolve_dsn(_args(dsn=RENDER_TEST_DB))
    allowed, no_error = script._resolve_dsn(
        _args(dsn=RENDER_TEST_DB, no_production_reference=True)
    )
    assert refused == "" and "refusing to write" in error
    assert allowed == RENDER_TEST_DB and no_error == ""


def test_non_postgres_dsn_is_rejected(script) -> None:
    dsn, error = script._resolve_dsn(
        _args(dsn="sqlite:////tmp/cela.db", no_production_reference=True)
    )
    assert dsn == ""
    assert "PostgreSQL" in error
