"""Whole-system secret file loading (ADR-0021): `.env` from app root / `/etc/secrets`.

Hermetic by construction: every test works under ``tmp_path`` with an explicit
mapping or an explicit ``CELA_SECRET_FILE``, and the shared conftest disables
default discovery (``CELA_SECRET_FILE=off``) so a developer's local ``.env``
can never leak into the suite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from os_server.secret_file import (
    SecretFileError,
    candidate_secret_files,
    load_secret_files,
    parse_secret_file,
)

from os_server import secret_file

_ENV_NAMES = (
    "CELA_SECRET_FILE",
    "CELA_API_TOKEN",
    "OS_API_TOKEN",
    "CELA_PRIVATE_API_TOKEN",
)


@pytest.fixture(autouse=True)
def _restore_secret_env() -> Iterator[None]:
    """Undo values the loader copies into the real process environment.

    Loading deliberately mutates ``os.environ`` (that is the whole-system
    bridge), and ``monkeypatch`` cannot see assignments it did not make, so
    tests that resolve tokens or build the app must hand the environment back
    exactly as they found it — a leaked file token otherwise breaks the
    env-token tests that run later in the suite.
    """
    before = {name: os.environ.get(name) for name in _ENV_NAMES}
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_parse_supports_the_documented_dotenv_subset() -> None:
    parsed = parse_secret_file(
        "\n".join(
            [
                "# a comment",
                "",
                "CELA_API_TOKEN=plain-token",
                "export CELA_PRIVATE_API_TOKEN='single quoted'",
                'GEMINI_API_KEY="line\\nbreak \\"quoted\\""',
                "CELA_RUN_STORE_URL=sqlite:///./cela_runs.db  # trailing comment",
                "EMPTY_VALUE=",
                "DUP=first",
                "DUP=last-wins",
            ]
        )
    )

    assert parsed == {
        "CELA_API_TOKEN": "plain-token",
        "CELA_PRIVATE_API_TOKEN": "single quoted",
        "GEMINI_API_KEY": 'line\nbreak "quoted"',
        "CELA_RUN_STORE_URL": "sqlite:///./cela_runs.db",
        "EMPTY_VALUE": "",
        "DUP": "last-wins",
    }


def test_parse_error_never_echoes_the_offending_line() -> None:
    leaked = "do-not-print-this-value"

    with pytest.raises(SecretFileError) as excinfo:
        parse_secret_file(f"this is not an assignment {leaked}\n", source="secret.env")

    message = str(excinfo.value)
    assert "secret.env:1" in message
    assert leaked not in message


def test_parse_rejects_invalid_names_and_unterminated_quotes_without_echoing() -> None:
    with pytest.raises(SecretFileError):
        parse_secret_file("9BAD=value\n")
    with pytest.raises(SecretFileError) as excinfo:
        parse_secret_file('KEY="unterminated-secret\n')
    assert "unterminated-secret" not in str(excinfo.value)


def test_load_fills_missing_values_and_real_environment_wins(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text(
        "CELA_API_TOKEN=from-file\nCELA_PRIVATE_API_TOKEN=private-from-file\n",
        encoding="utf-8",
    )
    env = {"CELA_SECRET_FILE": str(secret), "CELA_API_TOKEN": "from-environment"}

    result = load_secret_files(env, cwd=tmp_path)

    assert env["CELA_API_TOKEN"] == "from-environment"
    assert env["CELA_PRIVATE_API_TOKEN"] == "private-from-file"
    assert result.paths == (secret,)
    # Names only: the result must be safe to log, so no value may appear in it.
    assert result.keys == ("CELA_PRIVATE_API_TOKEN",)
    assert "from-file" not in repr(result)
    assert "private-from-file" not in repr(result)


def test_missing_default_files_are_a_silent_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(secret_file, "SECRET_FILE_DIR", tmp_path / "etc-secrets")
    env: dict[str, str] = {}

    result = load_secret_files(env, cwd=tmp_path)

    assert result.loaded is False
    assert result.paths == ()
    assert env == {}


def test_default_candidates_are_app_root_then_etc_secrets(tmp_path: Path, monkeypatch) -> None:
    etc_dir = tmp_path / "etc-secrets"
    etc_dir.mkdir()
    (tmp_path / ".env").write_text("SHARED=from-root\nROOT_ONLY=1\n", encoding="utf-8")
    (etc_dir / ".env").write_text("SHARED=from-etc\nETC_ONLY=1\n", encoding="utf-8")
    monkeypatch.setattr(secret_file, "SECRET_FILE_DIR", etc_dir)
    env: dict[str, str] = {}

    result = load_secret_files(env, cwd=tmp_path)

    assert result.paths == (tmp_path / ".env", etc_dir / ".env")
    assert env == {"SHARED": "from-root", "ROOT_ONLY": "1", "ETC_ONLY": "1"}


def test_explicit_secret_file_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"

    with pytest.raises(SecretFileError) as excinfo:
        load_secret_files({"CELA_SECRET_FILE": str(missing)}, cwd=tmp_path)

    assert str(missing) in str(excinfo.value)


def test_explicit_relative_path_resolves_against_cwd_and_off_disables(tmp_path: Path) -> None:
    (tmp_path / "custom.env").write_text("KEY=relative\n", encoding="utf-8")
    env = {"CELA_SECRET_FILE": "custom.env"}

    assert candidate_secret_files(env, cwd=tmp_path) == (tmp_path / "custom.env",)
    load_secret_files(env, cwd=tmp_path)
    assert env["KEY"] == "relative"

    assert candidate_secret_files({"CELA_SECRET_FILE": "off"}, cwd=tmp_path) == ()
    assert load_secret_files({"CELA_SECRET_FILE": "off"}, cwd=tmp_path).loaded is False


def test_malformed_explicit_file_fails_without_echoing_values(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("BROKEN LINE do-not-print\n", encoding="utf-8")

    with pytest.raises(SecretFileError) as excinfo:
        load_secret_files({"CELA_SECRET_FILE": str(secret)}, cwd=tmp_path)

    assert "do-not-print" not in str(excinfo.value)


def test_resolve_token_uses_secret_file_but_explicit_empty_bypasses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from os_server.runtime import resolve_token

    secret = tmp_path / ".env"
    secret.write_text("CELA_API_TOKEN=file-platform-token\n", encoding="utf-8")
    monkeypatch.setenv("CELA_SECRET_FILE", str(secret))
    monkeypatch.delenv("CELA_API_TOKEN", raising=False)
    monkeypatch.delenv("OS_API_TOKEN", raising=False)

    assert resolve_token() == "file-platform-token"
    # ``explicit=""`` is the documented way to force the unconfigured state;
    # it must not consult the file at all — even a broken explicit path.
    monkeypatch.setenv("CELA_SECRET_FILE", str(tmp_path / "missing.env"))
    assert resolve_token("") == ""


def test_create_app_authenticates_with_tokens_from_secret_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from os_server.app import create_app
    from os_server.demo import build_demo_platform_for

    secret = tmp_path / ".env"
    secret.write_text(
        "CELA_API_TOKEN=file-platform-token\nCELA_PRIVATE_API_TOKEN=file-private-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CELA_SECRET_FILE", str(secret))
    for name in ("CELA_API_TOKEN", "OS_API_TOKEN", "CELA_PRIVATE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    app = create_app(lambda mode, max_steps: build_demo_platform_for(mode))
    with fastapi_testclient.TestClient(app) as client:
        assert (
            client.get("/runs", headers={"Authorization": "Bearer file-platform-token"}).status_code
            == 200
        )
        assert (
            client.get(
                "/cela/v1/whoami", headers={"Authorization": "Bearer file-private-token"}
            ).status_code
            == 200
        )
        # The two surfaces stay isolated even when both tokens come from a file.
        assert (
            client.get(
                "/cela/v1/whoami", headers={"Authorization": "Bearer file-platform-token"}
            ).status_code
            == 401
        )
