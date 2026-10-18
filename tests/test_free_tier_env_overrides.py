"""os_server's free-tier provider honours the documented <NAME>_MODEL/_BASE_URL.

Backward compatibility: with none of the overrides set, the built-in defaults
are used exactly as before; blank values count as unset.
"""

from __future__ import annotations

import pytest
from os_server.demo import free_tier_model_from_env

GEMINI_DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_BASE_URL",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_no_key_returns_none(clean_env):
    assert free_tier_model_from_env() is None


def test_defaults_when_only_key_is_set(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "k")
    m = free_tier_model_from_env()
    assert m.model_id == "gemini-2.5-flash"
    assert m.base_url == GEMINI_DEFAULT_URL
    assert m.api_key_env == "GEMINI_API_KEY"


def test_model_and_base_url_overrides_are_read(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "k")
    clean_env.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    clean_env.setenv("GEMINI_BASE_URL", "https://relay.example/v1beta/openai")
    m = free_tier_model_from_env()
    assert m.model_id == "gemini-2.5-pro"
    assert m.base_url == "https://relay.example/v1beta/openai"


def test_blank_overrides_fall_back_to_defaults(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "k")
    clean_env.setenv("GEMINI_MODEL", "   ")
    clean_env.setenv("GEMINI_BASE_URL", "")
    m = free_tier_model_from_env()
    assert m.model_id == "gemini-2.5-flash"
    assert m.base_url == GEMINI_DEFAULT_URL


def test_key_value_is_never_stored_on_model(clean_env):
    clean_env.setenv("GEMINI_API_KEY", "super-secret-value")
    m = free_tier_model_from_env()
    assert "super-secret-value" not in m.model_dump_json()


def test_reported_identity_follows_model_override(clean_env):
    from os_core.models import describe_execution

    clean_env.setenv("GEMINI_API_KEY", "k")
    clean_env.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    info = describe_execution(free_tier_model_from_env())
    assert info.model_name == "gemini-2.5-pro"


def test_reported_identity_default_is_unchanged(clean_env):
    from os_core.models import describe_execution

    clean_env.setenv("GEMINI_API_KEY", "k")
    info = describe_execution(free_tier_model_from_env())
    assert info.model_name == "gemini-2.5-flash"


def test_groq_model_env_is_not_applied_to_gemini_or_groq(clean_env):
    # Overrides are Gemini-only; GROQ_MODEL must not leak into the Groq adapter.
    clean_env.setenv("GROQ_API_KEY", "k")
    clean_env.setenv("GROQ_MODEL", "some-other-model")
    m = free_tier_model_from_env()
    assert m.model_id == "llama-3.3-70b-versatile"
    assert m.base_url == "https://api.groq.com/openai/v1"
