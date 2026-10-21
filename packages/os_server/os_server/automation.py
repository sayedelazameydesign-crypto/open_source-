"""Optional Activepieces & signed-webhook automation connector for Celia.sh — Mission OS.

Security & free-tier design principles:

1. **Zero required paid dependencies.** Mission OS works 100% without
   Activepieces configured. When ``CELA_ACTIVEPIECES_WEBHOOK_URL`` is unset,
   outbound dispatch returns a clean ``disabled`` status and never blocks a run.
2. **Secrets stay in process environment (ADR-0011).** The HMAC signing secret is
   read from ``os.environ[secret_env]`` at request time and is never stored on
   an instance attribute or logged.
3. **SSRFGuard + EgressPolicy on outbound webhooks.** Webhook URLs cannot point
   at loopback, RFC1918 private networks, link-local, or cloud metadata
   endpoints (`169.254.169.254`).
4. **Free-tier credit circuit breaker.** Activepieces Cloud Free caps tasks
   (e.g. 100/day or 1,000/month). :class:`ActivepiecesQuotaGuard` halts outbound
   triggers cleanly once the budget is reached so runs still complete locally.
5. **Idempotency + constant-time HMAC.** Both outbound and inbound webhooks
   carry ``X-Celia-Idempotency-Key`` and ``X-Celia-Signature-256``.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from os_core.redact import scrub_credentials
from os_tools.egress import EgressPolicy, EgressRule
from os_tools.pinned_http import PinnedAsyncHTTPTransport
from os_tools.ssrf import SSRFGuard

ENV_ACTIVEPIECES_URL = "CELA_ACTIVEPIECES_WEBHOOK_URL"
ENV_ACTIVEPIECES_SECRET = "CELA_ACTIVEPIECES_WEBHOOK_SECRET"
ENV_ACTIVEPIECES_DAILY_LIMIT = "CELA_ACTIVEPIECES_DAILY_LIMIT"
ENV_ACTIVEPIECES_MONTHLY_LIMIT = "CELA_ACTIVEPIECES_MONTHLY_LIMIT"


def sign_webhook_payload(payload: bytes, secret: str) -> str:
    """Return ``sha256=<hex>`` HMAC-SHA256 signature for *payload*."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Constant-time verification of ``sha256=<hex>`` HMAC-SHA256 *signature*."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = sign_webhook_payload(payload, secret)
    return hmac.compare_digest(expected, signature.strip())


def default_webhook_egress_policy(*, resolve_dns: bool = False) -> EgressPolicy:
    """Allow public HTTPS/HTTP webhook endpoints on 80/443 above the SSRF floor."""
    rules = [
        EgressRule(
            action="allow",
            host="*",
            schemes={"http", "https"},
            ports={80, 443},
            tools={"activepieces_webhook"},
        )
    ]
    return EgressPolicy(rules=rules, guard=SSRFGuard(resolve_dns=resolve_dns))


class ActivepiecesQuotaGuard:
    """Circuit breaker protecting the Activepieces free-tier task budget."""

    def __init__(
        self,
        *,
        daily_limit: int | None = None,
        monthly_limit: int | None = None,
    ) -> None:
        env_daily = int(os.environ.get(ENV_ACTIVEPIECES_DAILY_LIMIT, "100"))
        env_monthly = int(os.environ.get(ENV_ACTIVEPIECES_MONTHLY_LIMIT, "1000"))
        self.daily_limit = max(1, daily_limit if daily_limit is not None else env_daily)
        self.monthly_limit = max(1, monthly_limit if monthly_limit is not None else env_monthly)
        self._current_day: str = ""
        self._current_month: str = ""
        self._daily_used: int = 0
        self._monthly_used: int = 0

    def _roll_windows(self, now: datetime | None = None) -> None:
        ts = now or datetime.now(UTC)
        day_key = ts.strftime("%Y-%m-%d")
        month_key = ts.strftime("%Y-%m")
        if day_key != self._current_day:
            self._current_day = day_key
            self._daily_used = 0
        if month_key != self._current_month:
            self._current_month = month_key
            self._monthly_used = 0

    def check_and_consume(self, cost: int = 1, *, now: datetime | None = None) -> dict[str, Any]:
        """Consume *cost* credits if within daily and monthly caps."""
        self._roll_windows(now)
        amount = max(1, int(cost))
        if self._daily_used + amount > self.daily_limit:
            return {
                "allowed": False,
                "reason": "daily_quota_exceeded",
                "reason_ar": (
                    f"تم بلوغ الحد اليومي للحصة المجانية في Activepieces "
                    f"({self._daily_used}/{self.daily_limit}). أُكملت المهمة محليًا "
                    "دون إرسال الويب هوك."
                ),
                **self.snapshot(now=now),
            }
        if self._monthly_used + amount > self.monthly_limit:
            return {
                "allowed": False,
                "reason": "monthly_quota_exceeded",
                "reason_ar": (
                    f"تم بلوغ الحد الشهري للحصة المجانية في Activepieces "
                    f"({self._monthly_used}/{self.monthly_limit}). أُكملت المهمة محليًا "
                    "دون إرسال الويب هوك."
                ),
                **self.snapshot(now=now),
            }
        self._daily_used += amount
        self._monthly_used += amount
        return {
            "allowed": True,
            "reason": "ok",
            "reason_ar": "ضمن حدود الحصة المجانية.",
            **self.snapshot(now=now),
        }

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        self._roll_windows(now)
        return {
            "daily_used": self._daily_used,
            "daily_limit": self.daily_limit,
            "remaining_daily": max(0, self.daily_limit - self._daily_used),
            "monthly_used": self._monthly_used,
            "monthly_limit": self.monthly_limit,
            "remaining_monthly": max(0, self.monthly_limit - self._monthly_used),
        }


class ActivepiecesDispatcher:
    """Safe, credential-scrubbed outbound webhook sender for Activepieces."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        secret_env: str = ENV_ACTIVEPIECES_SECRET,
        quota_guard: ActivepiecesQuotaGuard | None = None,
        policy: EgressPolicy | None = None,
        transport: Any = None,
    ) -> None:
        self._configured_url = webhook_url
        self.secret_env = secret_env
        self.quota_guard = quota_guard or ActivepiecesQuotaGuard()
        self.policy = policy or default_webhook_egress_policy()
        self.transport = transport

    @property
    def webhook_url(self) -> str:
        return (self._configured_url or os.environ.get(ENV_ACTIVEPIECES_URL, "")).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def __repr__(self) -> str:
        safe_url = scrub_credentials(self.webhook_url) if self.webhook_url else "<unconfigured>"
        return f"ActivepiecesDispatcher(url={safe_url!r}, secret_env={self.secret_env!r})"

    async def dispatch(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a signed, credential-scrubbed webhook if enabled and within quota."""
        target = self.webhook_url
        if not target:
            return {
                "dispatched": False,
                "status": "disabled",
                "reason_ar": "موصّل Activepieces الاختياري غير مفعّل (لم يُحدَّد رابط الويب هوك).",
            }

        # 1. Enforce SSRFGuard + EgressPolicy before consuming quota or touching network
        try:
            verdict = self.policy.enforce(
                target,
                tool="activepieces_webhook",
                resolve_dns=not isinstance(self.transport, httpx.MockTransport),
            )
        except Exception as exc:
            return {
                "dispatched": False,
                "status": "blocked_by_policy",
                "error": scrub_credentials(f"{type(exc).__name__}: {exc}"),
                "reason_ar": "رفض حارس SSRF أو سياسة الخروج (EgressPolicy) وجهة الويب هوك.",
            }

        # 2. Enforce free-tier quota guard
        quota_check = self.quota_guard.check_and_consume(1)
        if not quota_check["allowed"]:
            return {
                "dispatched": False,
                "status": quota_check["reason"],
                "reason_ar": quota_check["reason_ar"],
                "quota": self.quota_guard.snapshot(),
            }

        # 3. Scrub secrets from payload and compute deterministic idempotency key
        idem_key = idempotency_key or hashlib.sha256(f"{run_id}:{event_type}".encode()).hexdigest()
        envelope = {
            "schema": "celia.mission.webhook.v1",
            "run_id": scrub_credentials(run_id),
            "event_type": scrub_credentials(event_type),
            "idempotency_key": idem_key,
            "sent_at": datetime.now(UTC).isoformat(),
            "data": json.loads(
                scrub_credentials(json.dumps(payload, ensure_ascii=False, default=str))
            ),
        }
        raw_bytes = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")

        headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Celia-Idempotency-Key": idem_key,
            "User-Agent": "celia-mission-os-webhook/0.1",
        }
        secret = os.environ.get(self.secret_env, "")
        if secret:
            headers["X-Celia-Signature-256"] = sign_webhook_payload(raw_bytes, secret)

        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                transport=PinnedAsyncHTTPTransport(verdict, transport=self.transport),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                resp = await client.post(target, content=raw_bytes, headers=headers)
            return {
                "dispatched": 200 <= resp.status_code < 300,
                "status": "sent" if 200 <= resp.status_code < 300 else "http_error",
                "status_code": int(resp.status_code),
                "idempotency_key": idem_key,
                "quota": self.quota_guard.snapshot(),
            }
        except Exception as exc:
            return {
                "dispatched": False,
                "status": "network_error",
                "error": scrub_credentials(f"{type(exc).__name__}: {exc}"),
                "idempotency_key": idem_key,
                "quota": self.quota_guard.snapshot(),
            }


class InboundWebhookVerifier:
    """Constant-time HMAC verifier + replay/idempotency guard for inbound webhooks."""

    def __init__(
        self,
        *,
        secret_env: str = ENV_ACTIVEPIECES_SECRET,
        max_keys: int = 2048,
    ) -> None:
        self.secret_env = secret_env
        self.max_keys = max(64, max_keys)
        self._seen: OrderedDict[str, str] = OrderedDict()

    def verify_and_record(
        self,
        *,
        payload: bytes,
        signature: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        secret = os.environ.get(self.secret_env, "")
        if not secret:
            raise PermissionError(f"missing webhook signing secret in {self.secret_env}")
        if not signature or not verify_webhook_signature(payload, signature, secret):
            raise ValueError("invalid webhook HMAC signature")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("missing X-Celia-Idempotency-Key header")

        clean_key = idempotency_key.strip()
        if clean_key in self._seen:
            return {
                "accepted": True,
                "duplicate": True,
                "idempotency_key": clean_key,
                "first_seen_at": self._seen[clean_key],
            }
        now_iso = datetime.now(UTC).isoformat()
        self._seen[clean_key] = now_iso
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return {
            "accepted": True,
            "duplicate": False,
            "idempotency_key": clean_key,
            "received_at": now_iso,
        }


_SECRET_NAME_TOKENS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "DSN")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _postgres_driver_available() -> bool:
    """Is the optional ``psycopg`` driver importable in *this* environment?

    ``PostgresRunStore`` imports psycopg lazily, so a deployment can start
    cleanly, pass ``/health``, and only then fail on the first run with "psycopg
    is not installed". The doctor must catch that at deploy time instead.
    A seam so tests can simulate both environments without installing a driver.
    """
    return importlib.util.find_spec("psycopg") is not None


_CLOUDFLARE_BFF_PATH = Path("packages/web/functions/api/backend/[[path]].ts")
_NEXTJS_BFF_PATH = Path("packages/web/app/api/backend/[...path]/route.ts")


def backend_url_problem(raw: str, *, var_name: str = "CELA_BACKEND_URL") -> str | None:
    """Return why *raw* must not be used as a service base URL, or ``None``.

    Mirrors the refusals of the Cloudflare Pages proxy: bearer tokens may only
    leave the edge over TLS, and credentials must not be embedded in a URL that
    intermediaries log. ``var_name`` names the setting in the message, so the
    same rule can be reported for the edge proxy and for a probed service.
    """
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return f"{var_name} must be an absolute http(s) URL"
    if parts.username or parts.password:
        return f"{var_name} must not embed credentials"
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        return f"{var_name} must use https outside loopback: tokens must not travel in cleartext"
    return None


# Kept for the callers that used the private name.
_backend_url_problem = backend_url_problem


def _render_blueprint_code_exec_modes(workspace: Path) -> tuple[str | None, ...] | None:
    """Read target Render web-service modes; return None when YAML is unreadable.

    PyYAML is a development/doctor dependency. If it is unavailable or the
    target Blueprint is malformed, production readiness fails closed instead
    of trusting an environment value that could disagree with the deploy file.
    """
    try:
        import yaml
    except ImportError:
        return None

    try:
        blueprint = yaml.safe_load((workspace / "render.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(blueprint, dict) or not isinstance(blueprint.get("services"), list):
        return None

    modes: list[str | None] = []
    for service in blueprint["services"]:
        if not isinstance(service, dict) or service.get("type") != "web":
            continue
        env_vars = service.get("envVars", [])
        if not isinstance(env_vars, list):
            modes.append(None)
            continue
        configured = [
            str(item.get("value", "")).strip().lower()
            for item in env_vars
            if isinstance(item, dict) and item.get("key") == "CELA_CODE_EXEC_MODE"
        ]
        # Reject missing or duplicate declarations; an ambiguous target config
        # is not safe to deploy even if one entry happens to say "disabled".
        modes.append(configured[0] if len(configured) == 1 else None)
    return tuple(modes)


def check_deployment_readiness(
    env: Mapping[str, str] | None = None,
    *,
    root: str | Path = ".",
    strict_production: bool = False,
) -> dict[str, Any]:
    """Inspect *env* and *root* and return a credential-scrubbed readiness report."""
    active_env = dict(os.environ if env is None else env)
    workspace = Path(root).resolve()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for key in sorted(active_env):
        upper = key.upper()
        if upper.startswith("NEXT_PUBLIC_") and any(tok in upper for tok in _SECRET_NAME_TOKENS):
            errors.append(
                {
                    "id": "browser_exposed_secret_var",
                    "variable": key,
                    "message_ar": (
                        f"المتغير {key} يبدأ بـ NEXT_PUBLIC_ ويحمل اسم سر/مفتاح، مما يكشفه "
                        "في متصفح المستخدم. انقله إلى بيئة الخادم فقط."
                    ),
                }
            )

    api_token = active_env.get("CELA_API_TOKEN", "").strip()
    private_token = active_env.get("CELA_PRIVATE_API_TOKEN", "").strip()
    store_url = active_env.get("CELA_RUN_STORE_URL", "").strip()
    is_render = active_env.get("RENDER", "").lower() in {"1", "true", "yes"}
    # Cloudflare Pages sets CF_PAGES=1 in every build environment, so the doctor
    # can tell which edge the front end is being built for.
    is_cloudflare_pages = active_env.get("CF_PAGES", "").lower() in {"1", "true", "yes"}
    # Vercel sets VERCEL=1 in every build environment (and runtime). Next.js on
    # Vercel always runs with NODE_ENV=production, so a missing
    # CELA_WEB_ACCESS_SECRET means every /api/backend/* request fails closed
    # with 503 access_gate_unconfigured — the published UI never reaches the
    # backend. The doctor must warn about that even though the secret may live
    # in Vercel dashboard settings that the build env cannot see (hence warning
    # by default, error under --strict).
    is_vercel = active_env.get("VERCEL", "").lower() in {"1", "true", "yes"}
    backend_url = active_env.get("CELA_BACKEND_URL", "").strip()

    if is_render:
        code_exec_mode = active_env.get("CELA_CODE_EXEC_MODE", "").strip().lower()
        target_modes = _render_blueprint_code_exec_modes(workspace)
        target_safe = bool(target_modes) and all(mode == "disabled" for mode in target_modes)
        if code_exec_mode != "disabled" or not target_safe:
            errors.append(
                {
                    "id": "code_exec_not_disabled",
                    "variable": "CELA_CODE_EXEC_MODE",
                    "message_ar": (
                        "يجب أن يحدد Render Blueprint وكل بيئة تشغيل CELA_CODE_EXEC_MODE=disabled "
                        "صراحةً قبل أي نشر؛ لا يُقبل الافتراضي المحلي ولا يُتجاوز الفحص "
                        "باختيار مزوّد بديل."
                    ),
                }
            )

    if strict_production or is_render:
        if not api_token:
            errors.append(
                {
                    "id": "missing_api_token",
                    "variable": "CELA_API_TOKEN",
                    "message_ar": "المتغير CELA_API_TOKEN مطلوب في بيئة الإنتاج.",
                }
            )
        if not private_token:
            errors.append(
                {
                    "id": "missing_private_api_token",
                    "variable": "CELA_PRIVATE_API_TOKEN",
                    "message_ar": "المتغير CELA_PRIVATE_API_TOKEN مطلوب في بيئة الإنتاج.",
                }
            )
        if api_token and private_token and api_token == private_token:
            errors.append(
                {
                    "id": "identical_public_and_private_tokens",
                    "variable": "CELA_PRIVATE_API_TOKEN",
                    "message_ar": (
                        "يجب أن يختلف CELA_PRIVATE_API_TOKEN عن CELA_API_TOKEN لعزل المسار الخاص."
                    ),
                }
            )

    # The server fails fast at startup when live mode is requested without a
    # usable provider (os_server.app.create_app). The doctor must predict that
    # failure, not report "ready" for a deploy that cannot boot: it resolves the
    # provider with the *same* function the server uses, against the inspected
    # environment (never the process environment, and never a network call).
    live_flag = active_env.get("CELA_USE_LIVE_PROVIDER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if live_flag:
        from os_server.demo import free_tier_model_from_env

        if free_tier_model_from_env(env=active_env) is None:
            errors.append(
                {
                    "id": "live_provider_unconfigured",
                    "variable": "CELA_USE_LIVE_PROVIDER",
                    "message_ar": (
                        "CELA_USE_LIVE_PROVIDER مفعّل لكن لا يوجد مزوّد صالح: يجب ضبط "
                        "CELA_PROVIDER_BASE_URL وCELA_PROVIDER_MODEL ومتغير المفتاح "
                        "(افتراضيًا GEMINI_API_KEY)، وإلا سيرفض الخادم الإقلاع. إما أضف "
                        "المفتاح أو اضبط CELA_USE_LIVE_PROVIDER=0 لتشغيل الوضع التجريبي."
                    ),
                }
            )

    # Any provider with an ephemeral filesystem (containers, free dynos, CI
    # runners) loses SQLite and memory stores on redeploy. The flag lets a
    # provider-agnostic pipeline require the same guarantee Render enforces.
    require_persistent_store = is_render or active_env.get(
        "CELA_DEPLOY_REQUIRE_PERSISTENT_STORE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if require_persistent_store:
        postgres_store = store_url.startswith(("postgres://", "postgresql://"))
        distributed_store = store_url in {"distributed://", "distributed"} and all(
            active_env.get(name, "").strip() for name in ("CELA_POSTGRES_URL", "CELA_REDIS_URL")
        )
        if not postgres_store and not distributed_store:
            if is_render:
                message_ar = (
                    "يتطلب نشر Render مخزن تشغيل دائمًا: اضبط CELA_RUN_STORE_URL على "
                    "postgresql:// أو postgres://؛ أو استخدم distributed:// مع ضبط "
                    "CELA_POSTGRES_URL وCELA_REDIS_URL. SQLite والذاكرة لا يصمدان أمام "
                    "إعادة تشغيل Render Free، لذا يُرفض النشر بدل فقد التشغيلات."
                )
            else:
                message_ar = (
                    "هذا النشر مُعلَن كدائم (CELA_DEPLOY_REQUIRE_PERSISTENT_STORE=1) لكن "
                    "CELA_RUN_STORE_URL ليس Postgres: اضبط postgresql:// أو postgres://؛ "
                    "أو استخدم distributed:// مع CELA_POSTGRES_URL وCELA_REDIS_URL. "
                    "قرص الحاوية والذاكرة يُفقدان عند كل إعادة نشر أو استبدال نسخة، "
                    "لذا يُرفض النشر بدل فقد التشغيلات."
                )
            errors.append(
                {
                    "id": "persistent_run_store_required",
                    "variable": "CELA_RUN_STORE_URL",
                    "message_ar": message_ar,
                }
            )

    if store_url.startswith(("postgres://", "postgresql://", "distributed")) and not (
        _postgres_driver_available()
    ):
        errors.append(
            {
                "id": "postgres_driver_missing",
                "variable": "CELA_RUN_STORE_URL",
                "message_ar": (
                    "CELA_RUN_STORE_URL يشير إلى Postgres بينما حزمة psycopg غير مثبتة في "
                    "هذه البيئة؛ ثبّت extra التوزيع: pip install -e '.[server,distributed]' "
                    "(يتوقف الخادم عند أول تشغيل وليس عند الإقلاع، لذا يفشل الفحص هنا)."
                ),
            }
        )

    if is_cloudflare_pages:
        if backend_url:
            problem = _backend_url_problem(backend_url)
            if problem is not None:
                errors.append(
                    {
                        "id": "insecure_backend_url",
                        "variable": "CELA_BACKEND_URL",
                        "message_ar": (
                            f"{problem}. وكيل Pages Function يرفض هذا العنوان كي لا تنتقل "
                            "رموز الوصول على الشبكة مكشوفة."
                        ),
                    }
                )
        else:
            warnings.append(
                {
                    "id": "cloudflare_backend_url_unset",
                    "variable": "CELA_BACKEND_URL",
                    "message_ar": (
                        "لم يُضبط CELA_BACKEND_URL في بيئة بناء Cloudflare Pages. لا يفحص "
                        "هذا الفحص أسرار وقت التشغيل في Pages Function؛ تأكد من ضبطه مع "
                        "CELA_API_TOKEN و CELA_PRIVATE_API_TOKEN في إعدادات المشروع."
                    ),
                }
            )

    if is_vercel:
        if backend_url:
            problem = _backend_url_problem(backend_url)
            if problem is not None:
                errors.append(
                    {
                        "id": "insecure_backend_url",
                        "variable": "CELA_BACKEND_URL",
                        "message_ar": (
                            f"{problem}. وكيل Next.js يرفض هذا العنوان كي لا تنتقل "
                            "رموز الوصول على الشبكة مكشوفة."
                        ),
                    }
                )
        else:
            warnings.append(
                {
                    "id": "vercel_backend_url_unset",
                    "variable": "CELA_BACKEND_URL",
                    "message_ar": (
                        "لم يُضبط CELA_BACKEND_URL في بيئة بناء Vercel. لا يفحص "
                        "هذا الفحص أسرار وقت التشغيل في Vercel؛ تأكد من ضبطه مع "
                        "CELA_API_TOKEN و CELA_PRIVATE_API_TOKEN في إعدادات المشروع."
                    ),
                }
            )

        web_access_secret = active_env.get("CELA_WEB_ACCESS_SECRET", "").strip()
        gate_required = active_env.get("CELA_WEB_GATE_REQUIRED", "").strip()

        if not web_access_secret:
            entry = {
                "id": "vercel_web_access_secret_unset",
                "variable": "CELA_WEB_ACCESS_SECRET",
                "message_ar": (
                    "لم يُضبط CELA_WEB_ACCESS_SECRET في بيئة بناء Vercel. على Vercel يعمل "
                    "Next.js دائمًا بـ NODE_ENV=production، فغياب السر يعني فشلًا مغلقًا: كل "
                    "/api/backend/* يعيد 503 access_gate_unconfigured والواجهة المنشورة لا تصل "
                    "إلى الـbackend إطلاقاً. أضف السر في إعدادات Vercel (Production وPreview) "
                    "أو كسرّ Actions يُمرَّر إلى vercel deploy --env، ولا تضعه في نص "
                    "الـworkflow ولا في NEXT_PUBLIC_*."
                ),
            }
            if strict_production:
                errors.append(entry)
            else:
                warnings.append(entry)

        if gate_required != "1":
            entry = {
                "id": "vercel_gate_required_unset",
                "variable": "CELA_WEB_GATE_REQUIRED",
                "message_ar": (
                    "لم يُضبط CELA_WEB_GATE_REQUIRED=1 في بيئة بناء Vercel. اضبطه صراحةً "
                    "لتوثيق أن البوابة مطلوبة في الإنتاج؛ القيمة 0 تعني فتحًا مقصودًا فقط."
                ),
            }
            if strict_production:
                errors.append(entry)
            else:
                warnings.append(entry)

    # AI Indigo external API (ADR-0021) is strictly optional and completely
    # independent of core Render (C5) / Postgres (C6) readiness. Only inspect
    # its key when the operator explicitly turns CELA_INDIGO_ENABLE=1 on.
    if active_env.get("CELA_INDIGO_ENABLE", "").strip() == "1":
        from os_server.indigo_api import resolve_indigo_credential_var

        indigo_var = resolve_indigo_credential_var(active_env)
        if not active_env.get(indigo_var, "").strip():
            entry = {
                "id": "indigo_api_key_unset",
                "variable": indigo_var,
                "message_ar": (
                    f"تم تفعيل CELA_INDIGO_ENABLE=1 دون ضبط {indigo_var}؛ مسارات "
                    "AI Indigo المحمية ستعيد 503 حتى يُضبط المفتاح في بيئة الخادم."
                ),
            }
            if strict_production:
                errors.append(entry)
            else:
                warnings.append(entry)

    return {
        "ready": len(errors) == 0,
        "strict_production": strict_production or is_render,
        "store_backend_scheme": (
            scrub_credentials(store_url.split("://", 1)[0])
            if "://" in store_url
            else ("memory" if not store_url else "custom")
        ),
        "files": {
            "render_yaml": (workspace / "render.yaml").is_file(),
            "env_example": (workspace / ".env.example").is_file(),
            "cloudflare_bff_proxy": (workspace / _CLOUDFLARE_BFF_PATH).is_file(),
            "next_bff_proxy": (workspace / _NEXTJS_BFF_PATH).is_file(),
        },
        "errors": errors,
        "warnings": warnings,
    }


__all__ = [
    "ENV_ACTIVEPIECES_DAILY_LIMIT",
    "ENV_ACTIVEPIECES_MONTHLY_LIMIT",
    "ENV_ACTIVEPIECES_SECRET",
    "ENV_ACTIVEPIECES_URL",
    "ActivepiecesDispatcher",
    "ActivepiecesQuotaGuard",
    "InboundWebhookVerifier",
    "check_deployment_readiness",
    "default_webhook_egress_policy",
    "sign_webhook_payload",
    "verify_webhook_signature",
]
