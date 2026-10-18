#!/usr/bin/env python3
"""A1/A6 — real Gemini provider acceptance test.

Run from the repository root:

    read -rsp "Gemini API key: " GEMINI_API_KEY && printf '\\n'
    export GEMINI_API_KEY
    export CELA_PROVIDER_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai"
    export CELA_PROVIDER_MODEL="gemini-2.5-flash"
    export CELA_PROVIDER_CREDENTIAL_VAR=GEMINI_API_KEY
    export CELA_USE_LIVE_PROVIDER=1
    export CELA_API_TOKEN=test-token-only
    python scripts/test_gemini_provider.py

The script never prints the key. It reports PASS/FAIL for the five acceptance
criteria from the deployment plan:

  1. a real outbound HTTP request reaches the configured provider and its 200
     response drives the task; the key never appears in output, events, logs,
     or the recorded request metadata.
  2. the task performs real work through a successful ``python_execute`` call.
  3. ``execution.kind == "provider_adapter"`` after the task, with fallback
     never triggered and the provider identity matching the configuration.
  4. a forced HTTP 401 is answered by the scripted fallback: the reason names
     the 401, ``fallback_triggered`` is true, and ``kind == "scripted"``.
  5. missing credentials fail closed at the *real* entry points:
     ``create_app()`` raises before serving and ``os_server.cli run --live``
     exits non-zero. Neither silently serves scripted output.

This is a LIVE test: it performs real network calls and may spend provider
quota. It is intentionally not part of CI. Exit codes: 0 = all criteria pass,
1 = at least one criterion failed, 2 = missing key / invalid configuration.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

# Resolve the package layout when running from a fresh checkout.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

from os_core.events import EventType  # noqa: E402
from os_core.models import describe_execution  # noqa: E402

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_KEY_VAR = "GEMINI_API_KEY"

# Every environment variable that can select or authenticate a provider. The
# fail-closed probes remove all of them, then re-add only the base URL/model so
# the test exercises "endpoint configured, credential missing" — the realistic
# misconfiguration — rather than an empty-URL error.
_PROVIDER_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "CELA_PROVIDER_BASE_URL",
    "CELA_PROVIDER_MODEL",
    "CELA_PROVIDER_CREDENTIAL_VAR",
    "CELA_PROVIDER_API_KEY_ENV",
    "CELA_MODEL_BASE_URL",
    "CELA_MODEL_ID",
    "GEMINI_MODEL",
    "GEMINI_BASE_URL",
    "GROQ_MODEL",
    "GROQ_BASE_URL",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
)

# Only these packages need to be importable in the subprocess probes: the probe
# must work in a fresh checkout that was not ``pip install -e .``-ed first.
_PACKAGE_DIRS = (
    "os_core",
    "os_agents",
    "os_tools",
    "os_orchestration",
    "os_server",
    "os_skills",
    "os_reach",
    "os_memory",
    "os_know",
    "os_mcp",
)

RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"

results: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": ok, "detail": detail})
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"\n[{mark}] {name}\n    {detail}")


def assert_no_key_leak(text: str, key: str) -> None:
    if key and key in text:
        raise AssertionError("API key leaked into output!")


def _scrub_key(text: str, key: str) -> str:
    """Scrub the specific API key from diagnostic output."""
    if not key:
        return text
    return text.replace(key, "[REDACTED_API_KEY]")


def provider_config() -> tuple[str, str, str]:
    """Resolve the provider target the same way the production code does.

    Defaults are applied before the platform is built, so a missing
    ``CELA_PROVIDER_BASE_URL`` can never turn the forced-401 check into an
    invalid-URL error: the request always targets a syntactically valid,
    OpenAI-compatible endpoint and the assertion is about HTTP 401.
    """
    base_url = (
        os.environ.get("CELA_PROVIDER_BASE_URL", "").strip()
        or os.environ.get("CELA_MODEL_BASE_URL", "").strip()
        or os.environ.get("GEMINI_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")
    model_id = (
        os.environ.get("CELA_PROVIDER_MODEL", "").strip()
        or os.environ.get("CELA_MODEL_ID", "").strip()
        or os.environ.get("GEMINI_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    key_var = (
        os.environ.get("CELA_PROVIDER_CREDENTIAL_VAR", "").strip()
        or os.environ.get("CELA_PROVIDER_API_KEY_ENV", "").strip()
        or DEFAULT_KEY_VAR
    )
    return base_url, model_id, key_var


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class RecordingTransport(httpx.AsyncHTTPTransport):
    """Forward real HTTP requests while recording their public shape.

    Subclasses the real transport (rather than wrapping it) because httpx
    enters and exits the transport as an async context manager. The key
    travels in the ``Authorization`` header; only its *presence* is recorded,
    never its value. Method, URL, status and the model sent in the body are
    captured from the actual outbound exchange.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: Any) -> Any:
        response = await super().handle_async_request(request)
        model = None
        try:
            model = json.loads(request.content.decode("utf-8")).get("model")
        except Exception:
            model = None
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "status": response.status_code,
                "model": model,
                "has_authorization": bool(request.headers.get("authorization")),
            }
        )
        return response


async def run_and_collect(platform: Any, task: str, *, prefix: str) -> dict[str, Any]:
    """Run one task with a single run id shared by the bus and the run."""
    run_id = f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    bus = platform.make_bus(run_id)
    events: list[Any] = []
    bus.subscribe(events.append)
    state = await platform.run(task, run_id=run_id, bus=bus)
    return {
        "run_id": run_id,
        "state": state,
        "events": events,
        "execution": describe_execution(platform.model).model_dump(),
    }


def _tool_calls(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type is EventType.TOOL_CALL]


def _tool_results(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type is EventType.TOOL_RESULT]


def _log_lines(events: list[Any]) -> list[str]:
    out: list[str] = []
    for e in events:
        if e.type in (EventType.LOG, EventType.WARNING):
            out.append(str(e.data.get("text", "")))
    return out


def _probe_env(base_url: str, model_id: str) -> dict[str, str]:
    """A clean environment: endpoint configured, every credential removed."""
    env = {k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV_KEYS}
    env.pop("CELA_RUN_STORE_URL", None)
    env["CELA_USE_LIVE_PROVIDER"] = "1"
    env["CELA_PROVIDER_BASE_URL"] = base_url
    env["CELA_PROVIDER_MODEL"] = model_id
    env["CELA_PROVIDER_CREDENTIAL_VAR"] = "CELA_TEST_MISSING_KEY"
    env.pop("CELA_TEST_MISSING_KEY", None)
    env["CELA_LEDGER_PATH"] = "off"
    env["PYTHONPATH"] = os.pathsep.join(
        [*(os.path.join(_ROOT, "packages", name) for name in _PACKAGE_DIRS), _ROOT]
    )
    return env


def _fail_closed_probe(base_url: str, model_id: str) -> tuple[bool, str]:
    """Exercise the real server and CLI entry points with no credential."""
    env = _probe_env(base_url, model_id)

    app_probe_src = (
        "import json\n"
        "try:\n"
        "    from os_server.app import create_app\n"
        "    create_app(api_token='probe-token', private_api_token='probe-private-token')\n"
        "except RuntimeError as exc:\n"
        "    print(json.dumps({'raised': 'RuntimeError', 'message': str(exc)}))\n"
        "else:\n"
        "    print(json.dumps({'raised': None}))\n"
    )
    app_probe = subprocess.run(
        [sys.executable, "-c", app_probe_src],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    app_ok = False
    app_detail = f"exit={app_probe.returncode}"
    if app_probe.returncode == 0:
        try:
            payload = json.loads(app_probe.stdout.strip().splitlines()[-1])
        except Exception as exc:
            app_detail = f"create_app probe output was not JSON: {exc}"
        else:
            app_ok = payload.get("raised") == "RuntimeError" and (
                "no valid LLM provider" in str(payload.get("message", ""))
            )
            app_detail = (
                "create_app() raised RuntimeError before serving"
                if app_ok
                else f"create_app() did not fail closed: {payload!r}"
            )
    else:
        app_detail = f"create_app probe crashed: {app_probe.stderr[-300:]!r}"

    cli_probe = subprocess.run(
        [sys.executable, "-m", "os_server.cli", "run", "--live", "fail-closed probe"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    cli_output = f"{cli_probe.stdout}\n{cli_probe.stderr}"
    cli_ok = cli_probe.returncode == 2 and "no valid LLM provider" in cli_output
    cli_detail = (
        "CLI run --live exited 2 with the fail-closed message"
        if cli_ok
        else f"CLI exit={cli_probe.returncode}; output={cli_output[-300:]!r}"
    )

    return (
        app_ok and cli_ok,
        f"{app_detail}; {cli_detail}",
    )


async def main() -> int:
    from os_core.models import OpenAICompatModel, ScriptedModel
    from os_server.demo import ResilientModel, build_demo_platform, demo_react_model

    base_url, model_id, key_var = provider_config()
    api_key = os.environ.get(key_var, "").strip()

    print("=" * 72)
    print("Gemini provider acceptance test (A1/A6)")
    print("=" * 72)
    # The URL may carry userinfo and the credential variable name is
    # credential-shaped, so neither is printed: only whether they are set.
    print(f"  base_url       : {'configured' if base_url else '(not set)'}")
    print(f"  model          : {model_id}")
    print("  credential var : configured (name not printed)")
    print(f"  key present    : {'yes' if api_key else 'no'}")
    print()

    if not _valid_http_url(base_url):
        record(
            "precondition: valid provider URL",
            False,
            "CELA_PROVIDER_BASE_URL is not a valid http(s) URL",
        )
        _print_summary()
        return 2

    if not api_key:
        record(
            "precondition: key present",
            False,
            f"{key_var} is empty — set it before running this test.",
        )
        _print_summary()
        return 2
    record(
        "precondition: key present",
        True,
        f"key length={len(api_key)} (value not printed)",
    )

    # Pin the environment to the resolved target so the production resolver
    # and this test cannot disagree about which endpoint/model is under test.
    os.environ["CELA_USE_LIVE_PROVIDER"] = "1"
    os.environ["CELA_PROVIDER_BASE_URL"] = base_url
    os.environ["CELA_PROVIDER_MODEL"] = model_id
    os.environ["CELA_PROVIDER_CREDENTIAL_VAR"] = key_var

    # ---- Criterion 5: misconfigured provider fails closed -------------------
    # This runs create_app() and the CLI in clean subprocesses — the real entry
    # points, not a helper function — so a successful probe is actual evidence
    # that the service refuses to start without a credential.
    fail_closed_ok, fail_closed_detail = _fail_closed_probe(base_url, model_id)
    record(
        "5. fail-closed: real entry points refuse a keyless live start",
        fail_closed_ok,
        fail_closed_detail,
    )

    # ---- Criterion 1+2+3: real run with a tool call -------------------------
    print("\n--- Running a real Gemini-driven task that must invoke python_execute ---")
    transport = RecordingTransport()
    platform = build_demo_platform(
        mode="react",
        use_env_provider=True,
        interactive=True,
        transport=transport,
    )
    exec_before = describe_execution(platform.model)
    print(f"  execution.kind (before run): {exec_before.kind} / {exec_before.model_name}")

    if exec_before.kind != "provider_adapter":
        record(
            "1. real outbound request, key not logged",
            False,
            "the live provider was not resolved from the environment; "
            "check CELA_PROVIDER_BASE_URL / CELA_PROVIDER_MODEL / the key variable",
        )
        record("2. task performs real work via a tool call", False, "no live run attempted")
        record(
            "3. execution.kind == provider_adapter on provider success",
            False,
            f"kind={exec_before.kind}",
        )
    else:
        report = await run_and_collect(
            platform,
            "Use the sandboxed python_execute tool to compute the 12th Fibonacci number. "
            "Call the tool exactly once and report the numeric result.",
            prefix="accept",
        )
        state = report["state"]
        events = report["events"]
        exec_after = describe_execution(platform.model)

        calls = _tool_calls(events)
        results_ev = _tool_results(events)
        logs = "\n".join(_log_lines(events))
        for text in (logs, state.final_answer or "", state.error or ""):
            assert_no_key_leak(text, api_key)
        assert_no_key_leak(json.dumps(transport.requests), api_key)

        requests = transport.requests
        statuses = [r["status"] for r in requests]
        endpoint_ok = bool(requests) and all(
            r["url"].startswith(f"{base_url}/") and r["url"].endswith("/chat/completions")
            for r in requests
        )
        models_ok = bool(requests) and all(r["model"] == model_id for r in requests)
        provider_answered = any(status == 200 for status in statuses)
        key_in_logs = api_key in logs or api_key in (state.final_answer or "")

        record(
            "1. real outbound request, key not logged",
            provider_answered and endpoint_ok and models_ok and not key_in_logs,
            f"outbound requests={len(requests)}; statuses={statuses}; "
            f"endpoint_ok={endpoint_ok}; model_ok={models_ok}; "
            f"key in logs/answer? {'YES (BAD)' if key_in_logs else 'no'}",
        )

        python_calls = [c for c in calls if c.get("name") == "python_execute"]
        python_results = [r for r in results_ev if r.get("name") == "python_execute"]
        tool_succeeded = any(r.get("status") == "success" for r in python_results)
        sample = (python_results[0].get("output", "") if python_results else "")[:120]
        record(
            "2. task performs real work via a tool call",
            bool(python_calls) and tool_succeeded,
            f"python_execute calls={len(python_calls)}; success={tool_succeeded}; "
            f"result sample: {sample!r}",
        )

        primary = getattr(platform.model, "primary", None)
        identity_ok = (
            isinstance(primary, OpenAICompatModel)
            and primary.model_id == model_id
            and primary.base_url.rstrip("/") == base_url
            and primary.api_key_env == key_var
        )
        fallback_triggered = getattr(platform.model, "fallback_triggered", True)
        record(
            "3. execution.kind == provider_adapter on provider success",
            exec_after.kind == "provider_adapter" and not fallback_triggered and identity_ok,
            f"kind={exec_after.kind}; fallback_triggered={fallback_triggered}; "
            f"identity_matches_config={identity_ok}",
        )

    # ---- Criterion 4: forced failure -> scripted fallback -------------------
    print("\n--- Forcing a 401 to verify the honest scripted fallback ---")
    unauth_urls: list[str] = []

    def _unauth_handler(request: Any) -> Any:
        unauth_urls.append(str(request.url))
        return httpx.Response(401, json={"error": {"message": "INVALID_KEY"}})

    failing_primary = OpenAICompatModel(
        name="gemini-forced-fail",
        model_id=model_id,
        base_url=base_url,
        api_key_env=key_var,
        timeout=30.0,
        transport=httpx.MockTransport(_unauth_handler),
    )
    fb_model = demo_react_model("")
    plat_fail = build_demo_platform(
        mode="react",
        use_env_provider=False,
        primary_model=ResilientModel(
            fallback=fb_model,
            primary=failing_primary,
            label="forced-fail",
        ),
        interactive=True,
    )
    r_fail = await run_and_collect(plat_fail, "What is 2+2?", prefix="forced")
    kind_after = describe_execution(plat_fail.model).kind
    fb_triggered = getattr(plat_fail.model, "fallback_triggered", False)
    fb_reason = getattr(plat_fail.model, "fallback_reason", None) or ""
    fallback_active = isinstance(getattr(plat_fail.model, "active_model", None), ScriptedModel)
    scrubbed = api_key not in fb_reason
    reason_names_401 = "401" in fb_reason or "Unauthorized" in fb_reason
    for text in (
        fb_reason,
        r_fail["state"].final_answer or "",
        "\n".join(_log_lines(r_fail["events"])),
    ):
        assert_no_key_leak(text, api_key)
    record(
        "4. on provider failure -> kind == scripted (honest fallback)",
        bool(unauth_urls)
        and reason_names_401
        and kind_after == "scripted"
        and fb_triggered
        and fallback_active
        and scrubbed,
        f"requests_seen={len(unauth_urls)}; kind={kind_after}; "
        f"fallback_triggered={fb_triggered}; fallback_active={fallback_active}; "
        f"reason_names_401={reason_names_401}; "
        f"scrubbed={'yes' if scrubbed else 'NO (LEAK)'}; "
        f"reason={_scrub_key(fb_reason, api_key)[:160]!r}",
    )

    # ---- Final status -------------------------------------------------------
    print()
    return _print_summary()


def _print_summary() -> int:
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    fails = 0
    for r in results:
        mark = f"{GREEN}PASS{RESET}" if r["ok"] else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {r['name']}")
        fails += 0 if r["ok"] else 1
    print()
    if fails == 0:
        print(f"{GREEN}All A1/A6 criteria passed.{RESET}")
        print("Proceed to C5 (Render) only after seeing this green.")
        return 0
    print(f"{RED}{fails} criterion/criteria FAILED.{RESET}")
    print("Do NOT deploy to Render until these are resolved in your local environment.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
