"""Fail-closed wiring for the live LLM provider (A1/A6).

These tests never make a network call. They pin the decisions that make a
"live" deployment honest:

  * ``create_app()`` refuses to build the server when live mode is requested
    but no credential is configured;
  * ``use_env_provider=False`` forces the offline path even when
    ``CELA_USE_LIVE_PROVIDER=1`` is set (the CLI's ``--demo`` depends on it);
  * ``use_env_provider=True`` resolves the provider even when the ambient
    environment gate is off (explicit caller intent wins);
  * the CLI ``run --live`` entry point exits 2 instead of serving scripted
    output without a provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from os_core.models import describe_execution
from os_server.app import create_app
from os_server.demo import build_demo_platform

_ROOT = Path(__file__).resolve().parents[1]
_PROVIDER_KEYS = (
    "CELA_PROVIDER_BASE_URL",
    "CELA_PROVIDER_MODEL",
    "CELA_PROVIDER_CREDENTIAL_VAR",
    "CELA_PROVIDER_API_KEY_ENV",
    "CELA_MODEL_BASE_URL",
    "CELA_MODEL_ID",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "CELA_USE_LIVE_PROVIDER",
)


@pytest.fixture
def clean_provider_env(monkeypatch):
    for name in _PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _configure_endpoint(monkeypatch, *, key_var: str = "CELA_TEST_MISSING_KEY") -> None:
    monkeypatch.setenv("CELA_PROVIDER_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("CELA_PROVIDER_MODEL", "test-model")
    monkeypatch.setenv("CELA_PROVIDER_CREDENTIAL_VAR", key_var)
    monkeypatch.delenv(key_var, raising=False)


def test_create_app_fails_closed_when_live_mode_has_no_credential(clean_provider_env):
    _configure_endpoint(clean_provider_env)
    clean_provider_env.setenv("CELA_USE_LIVE_PROVIDER", "1")

    with pytest.raises(RuntimeError, match="no valid LLM provider"):
        create_app(api_token="test-api-token", private_api_token="test-private-token")


def test_use_env_provider_false_forces_offline_even_with_the_flag_set(clean_provider_env):
    clean_provider_env.setenv("CELA_USE_LIVE_PROVIDER", "1")
    clean_provider_env.setenv("GEMINI_API_KEY", "test-key")

    platform = build_demo_platform(mode="react", use_env_provider=False)

    assert describe_execution(platform.model).kind == "scripted"


def test_use_env_provider_true_resolves_an_explicit_provider(clean_provider_env):
    _configure_endpoint(clean_provider_env, key_var="CELA_PROVIDER_TEST_KEY")
    clean_provider_env.setenv("CELA_PROVIDER_TEST_KEY", "test-key")

    platform = build_demo_platform(mode="react", use_env_provider=True)

    info = describe_execution(platform.model)
    assert info.kind == "provider_adapter"
    assert info.model_name


def test_cli_live_without_a_provider_exits_nonzero(clean_provider_env):
    _configure_endpoint(clean_provider_env)
    clean_provider_env.setenv("CELA_USE_LIVE_PROVIDER", "1")
    clean_provider_env.setenv("CELA_LEDGER_PATH", "off")

    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-m", "os_server.cli", "run", "--live", "no provider probe"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert "no valid LLM provider" in result.stderr
