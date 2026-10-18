"""C6 acceptance-script guard: never write to the production database.

``scripts/test_postgres_persistence.py`` takes a DSN from ``--dsn`` or
``CELA_TEST_POSTGRES_DSN`` only — never ``CELA_RUN_STORE_URL`` — and refuses a
target that resolves to the same database as the production variable.

That refusal has to stay precise, because the documented C6 workflow stores the
test database in a *second* Supabase project: every project in a region shares
one pooler host, port and database name, so a host/port/database comparison
would reject a genuinely separate project. The project ref in the username is
what distinguishes them. Conversely, the same project reached through the
pooler and through the direct host is still one database and must be refused.

These tests load the script as a module (it is an operator entry point, not an
importable package) and exercise the identity logic and DSN resolution.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_postgres_persistence.py"

PRODUCTION = (
    "postgresql://postgres.aaaa:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
SECOND_PROJECT_SAME_POOLER = (
    "postgresql://postgres.bbbb:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
SECOND_PROJECT_DIRECT = "postgresql://postgres.bbbb:pw@db.bbbb.supabase.co:5432/postgres"
PRODUCTION_DIRECT = "postgresql://postgres.aaaa:pw@db.aaaa.supabase.co:5432/postgres"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("c6_postgres_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_never_exposes_userinfo(script) -> None:
    identity = script._db_identity(PRODUCTION)
    assert identity == "aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
    assert "pw" not in identity
    assert "postgres.aaaa" not in identity


def test_second_supabase_project_on_the_same_pooler_is_allowed(script) -> None:
    assert not script._same_database(SECOND_PROJECT_SAME_POOLER, PRODUCTION)
    assert not script._same_database(SECOND_PROJECT_DIRECT, PRODUCTION)


def test_same_project_is_refused_across_pooler_and_direct_hosts(script) -> None:
    assert script._same_database(PRODUCTION_DIRECT, PRODUCTION)
    assert script._same_database(PRODUCTION, PRODUCTION)


def test_unrelated_render_database_is_allowed(script) -> None:
    assert not script._same_database("postgresql://u:p@dpg-abc:5432/cela", PRODUCTION)


def test_dsn_is_not_read_from_the_production_variable(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    monkeypatch.delenv("CELA_TEST_POSTGRES_DSN", raising=False)

    class Args:
        dsn = None

    dsn, error = script._resolve_dsn(Args())
    assert dsn == ""
    assert "CELA_TEST_POSTGRES_DSN" in error


def test_production_variable_alone_cannot_select_the_target(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)
    monkeypatch.setenv("CELA_TEST_POSTGRES_DSN", PRODUCTION)

    class Args:
        dsn = None

    dsn, error = script._resolve_dsn(Args())
    assert dsn == ""
    assert "refusing to write" in error
    # The refusal names the database identity, never the credentials.
    assert "postgres.aaaa" not in error and "pw" not in error


def test_a_separate_test_project_is_accepted(script, monkeypatch) -> None:
    monkeypatch.setenv("CELA_RUN_STORE_URL", PRODUCTION)

    class Args:
        dsn = SECOND_PROJECT_SAME_POOLER

    dsn, error = script._resolve_dsn(Args())
    assert error == ""
    assert dsn == SECOND_PROJECT_SAME_POOLER


def test_non_postgres_dsn_is_rejected(script, monkeypatch) -> None:
    monkeypatch.delenv("CELA_RUN_STORE_URL", raising=False)
    monkeypatch.delenv("CELA_TEST_POSTGRES_DSN", raising=False)

    class Args:
        dsn = "sqlite:////tmp/cela.db"

    dsn, error = script._resolve_dsn(Args())
    assert dsn == ""
    assert "PostgreSQL" in error
