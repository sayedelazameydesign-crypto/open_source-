"""CELA.PRO — الواجهة العربية لنظام Sovereign AI OS v2026.

تطبيق Gradio (RTL) مع:
- قائمة اختيار مزوّد الذكاء الاصطناعي (Groq, Gemini, OpenRouter, Kimi, Ollama)
- محادثة متوافقة مع OpenAI Chat Completions دون تسريب المفاتيح للواجهة أو التبديل التلقائي
- فحص دقيق (Smoke Test) لاتصال المزوّد يُصنّف النتيجة إلى BLOCKED / FAIL / INCONCLUSIVE / PASS
- حجب تلقائي لقيمة مفتاح API ([REDACTED]) في رسائل الخطأ ومخرجات الفحص
- محرك مهارات (Skill Engine) + OrionAgent مع موافقة بشرية (HITL)
- ألوان HEX فقط لتجنّب SyntaxError في Python
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
from typing import Any

import httpx

try:
    import gradio as gr
except ModuleNotFoundError:  # pragma: no cover - fallback for headless CI without gradio

    class _DummyComponent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.choices = kwargs.get("choices", [])
            self.value = kwargs.get("value")
            self.label = kwargs.get("label")

        def click(self, *args: Any, **kwargs: Any) -> None:
            return None

        def change(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _DummyBlocks(_DummyComponent):
        def __enter__(self) -> _DummyBlocks:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def launch(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _DummyGradio:
        Blocks = _DummyBlocks
        Row = _DummyBlocks
        Column = _DummyBlocks
        Markdown = _DummyComponent
        Dropdown = _DummyComponent
        ChatInterface = _DummyComponent
        Textbox = _DummyComponent
        Button = _DummyComponent
        Checkbox = _DummyComponent

    gr = _DummyGradio()  # type: ignore[assignment]

# ---------- الألوان (HEX فقط) ----------
BG = "#0b0f19"
PANEL = "#111827"
ACCENT = "#22d3ee"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"
BORDER = "#1f2937"
SUCCESS = "#34d399"
WARN = "#fbbf24"
DANGER = "#f87171"

# ---------- إعدادات المزوّدين ----------
PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "Groq": {
        "default_model": "openai/gpt-oss-20b",
        "default_base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "base_url_env": "GROQ_BASE_URL",
        "extra_body_env": "GROQ_REQUEST_EXTRA_JSON",
        "requires_key": True,
    },
    "Gemini": {
        "default_model": "gemini-2.5-flash",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "base_url_env": "GEMINI_BASE_URL",
        "extra_body_env": "GEMINI_REQUEST_EXTRA_JSON",
        "requires_key": True,
    },
    "OpenRouter": {
        "default_model": "openrouter/free",
        "default_base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "base_url_env": "OPENROUTER_BASE_URL",
        "extra_body_env": "OPENROUTER_REQUEST_EXTRA_JSON",
        "requires_key": True,
    },
    "Kimi": {
        "default_model": "moonshot-v1-8k",
        "default_base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "KIMI_API_KEY",
        "model_env": "KIMI_MODEL",
        "base_url_env": "KIMI_BASE_URL",
        "extra_body_env": "KIMI_REQUEST_EXTRA_JSON",
        "requires_key": True,
    },
    "Ollama": {
        "default_model": "llama3.2:latest",
        "default_base_url": "http://localhost:11434/v1",
        "api_key_env": None,
        "optional_key_env": "OLLAMA_API_KEY",
        "model_env": "OLLAMA_MODEL",
        "base_url_env": "OLLAMA_BASE_URL",
        "extra_body_env": "OLLAMA_REQUEST_EXTRA_JSON",
        "requires_key": False,
    },
}

PROVIDER_CHOICES: list[str] = list(PROVIDER_SPECS.keys())
_PROVIDER_LOOKUP: dict[str, str] = {name.lower(): name for name in PROVIDER_CHOICES}

# توافق خلفي مع الإصدار السابق
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.getenv("KIMI_MODEL", "moonshot-v1-8k")


def resolve_provider_name(provider: str | None = None) -> str:
    """تطبيع اسم المزوّد والتحقق من صحته."""
    raw = (provider or os.getenv("CELA_DEFAULT_PROVIDER", "Groq")).strip()
    if not raw:
        return "Groq"
    canonical = _PROVIDER_LOOKUP.get(raw.lower())
    if canonical is None:
        supported = ", ".join(PROVIDER_CHOICES)
        raise ValueError(f"مزوّد غير مدعوم: {raw}. الخيارات المتاحة: {supported}")
    return canonical


def get_default_provider() -> str:
    """إرجاع المزوّد الافتراضي المعتمد للواجهة."""
    try:
        return resolve_provider_name(os.getenv("CELA_DEFAULT_PROVIDER", "Groq"))
    except ValueError:
        return "Groq"


def get_provider_config(provider: str | None = None) -> dict[str, Any]:
    """قراءة إعدادات المزوّد ديناميكيًا من متغيرات البيئة."""
    name = resolve_provider_name(provider)
    spec = PROVIDER_SPECS[name]
    model = os.getenv(spec["model_env"], "").strip() or spec["default_model"]
    base_url = (
        os.getenv(spec["base_url_env"], "").strip().rstrip("/")
        or spec["default_base_url"]
    )
    api_key_env: str | None = spec["api_key_env"]
    requires_key: bool = bool(spec["requires_key"])
    if api_key_env:
        api_key = os.getenv(api_key_env, "").strip()
    else:
        optional_env = spec.get("optional_key_env", "OLLAMA_API_KEY")
        api_key = os.getenv(optional_env, "").strip()
    configured = (not requires_key) or bool(api_key)
    return {
        "name": name,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "model_env": spec["model_env"],
        "base_url_env": spec["base_url_env"],
        "extra_body_env": spec["extra_body_env"],
        "requires_key": requires_key,
        "configured": configured,
        "api_key": api_key,
    }


def format_provider_status(provider: str | None = None) -> str:
    """تنسيق حالة *إعداد* المزوّد للعرض في الواجهة دون كشف أي مفاتيح سرية.

    يصف هذا النص وجود المفتاح وتهيئته فقط، ولا يُعلن اتصالًا ناجحًا: وجود
    ``GROQ_API_KEY`` في البيئة لا يعني أن المفتاح صالح أو أن النموذج والعنوان
    يعملان. التحقق الفعلي مصدره الفحص الدقيق (``smoke_test_provider``) وحده.
    """
    try:
        cfg = get_provider_config(provider)
    except ValueError as exc:
        return f"<span class='status-err'>●</span> {exc}"

    unverified = "الاتصال غير مُتحقَّق منه — اضغط «🧪 اختبار اتصال المزوّد»"
    if not cfg["requires_key"]:
        key_state = (
            f"<span class='status-info'>●</span> مُهيّأ محليًا (لا يحتاج مفتاحًا) — {unverified}"
        )
    elif cfg["configured"]:
        key_state = (
            f"<span class='status-info'>●</span> المفتاح `{cfg['api_key_env']}` مُعيّن — "
            f"{unverified}"
        )
    else:
        key_state = (
            f"<span class='status-warn'>●</span> غير مُعيّن — "
            f"عيّن `{cfg['api_key_env']}` لتفعيل {cfg['name']}"
        )

    return (
        f"**المزوّد الحالي:** `{cfg['name']}`  |  "
        f"**حالة الإعداد:** {key_state}  |  "
        f"**النموذج:** `{cfg['model']}` (`{cfg['model_env']}`)  |  "
        f"**العنوان:** `{cfg['base_url']}`"
    )


def format_all_providers_summary() -> str:
    """ملخص *إعداد* المزوّدين الخمسة دون إظهار قيم المفاتيح ودون ادعاء اتصال ناجح."""
    lines = ["### 🌐 حالة إعداد مزوّدي الذكاء الاصطناعي"]
    for name in PROVIDER_CHOICES:
        cfg = get_provider_config(name)
        if not cfg["requires_key"]:
            badge = "🔵 مُهيّأ محليًا (بدون مفتاح) — غير مُختبَر"
        elif cfg["configured"]:
            badge = f"🔑 `{cfg['api_key_env']}` مُعيّن — غير مُختبَر"
        else:
            badge = f"⚠️ ينقصه `{cfg['api_key_env']}`"
        lines.append(f"- **{name}** (`{cfg['model']}`): {badge}")
    lines.append(
        "\nℹ️ «مُعيّن» تعني وجود المفتاح في متغيرات البيئة فحسب، لا أن الاتصال يعمل؛ "
        "التحقق الفعلي بزر «🧪 اختبار اتصال المزوّد» ونتيجته `PASS`."
    )
    return "\n".join(lines)


#: البديل المعروض دائمًا مكان قيمة أي مفتاح سري في السجلات ومخرجات الواجهة.
REDACTED = "[REDACTED]"


def redact_secret(text: Any, secret: str | None) -> str:
    """حجب قيمة المفتاح السري من أي نص قبل عرضه أو تسجيله.

    تُستخدم لرسائل الاستثناءات (التي قد تتضمن ترويسة ``Authorization`` كاملة)
    ولردود المزوّد على حد سواء، حتى لا يتسرّب مفتاح API إلى الواجهة أو السجلات.
    """
    cleaned = str(text)
    if secret:
        cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def get_provider_extra_body(cfg: dict[str, Any]) -> dict[str, Any]:
    """قراءة وسائط الطلب الإضافية الاختيارية من ``<PROVIDER>_REQUEST_EXTRA_JSON``.

    تُدمج في حمولة الطلب لضبط وسائط خاصة بمزوّد أو بنقطة وسيطة — مثل
    ``{"reasoning_effort": "none"}`` لنماذج الاستدلال خلف وسيط Modal — دون أي تعديل
    في الكود. المتغير اختياري تمامًا: إن كان فارغًا أو غير مُعيّن تبقى الحمولة كما كانت.

    يرفع ``ValueError`` إذا لم تكن القيمة كائن JSON صالحًا، ليتولّى المستدعي عرض رسالة
    واضحة بدل إرسال طلب ناقص.
    """
    env_name = str(cfg.get("extra_body_env") or "")
    raw = os.getenv(env_name, "").strip()
    if not env_name or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"قيمة `{env_name}` ليست JSON صالحًا ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"قيمة `{env_name}` يجب أن تكون كائن JSON (قاموسًا) لا {type(data).__name__}"
        )
    return data


def call_provider_chat(messages: list[dict[str, str]], provider: str | None = None) -> str:
    """استدعاء المزوّد المحدد فقط دون تبديل تلقائي عند غياب المفتاح أو الفشل."""
    try:
        cfg = get_provider_config(provider)
    except ValueError as exc:
        return f"❌ {exc}"

    if cfg["requires_key"] and not cfg["configured"]:
        return (
            f"⚠️ مفتاح {cfg['name']} غير مُعيّن (`{cfg['api_key_env']}`) — وضع تجريبي.\n"
            f"أضف المتغير `{cfg['api_key_env']}` في البيئة أو Colab Secrets ثم أعد المحاولة.\n"
            "ملاحظة: لا يتم الانتقال تلقائيًا إلى مزوّد آخر."
        )

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    try:
        extra_body = get_provider_extra_body(cfg)
    except ValueError as exc:
        return (
            f"❌ {exc}.\n"
            f"الصيغة المتوقعة لـ `{cfg['extra_body_env']}`: كائن JSON مثل "
            '{"reasoning_effort": "none"} — أو اتركه فارغًا.'
        )

    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.7,
    }
    payload.update(extra_body)
    # هوية الطلب لا تُشترى من متغير إضافي: النموذج من إعداد المزوّد، والرسائل من المحادثة.
    payload["model"] = cfg["model"]
    payload["messages"] = messages

    endpoint = f"{cfg['base_url']}/chat/completions"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = str(data["choices"][0]["message"]["content"])
            # لا يُعاد المفتاح السري إلى الواجهة حتى لو أعاد المزوّد صدىً له.
            return redact_secret(content, cfg["api_key"])
    except Exception as exc:
        # رسائل الاستثناء قد تحمل ترويسة Authorization كاملة، لذا تُحجب قبل العرض.
        detail = redact_secret(exc, cfg["api_key"])
        return (
            f"❌ فشل الاتصال بمزوّد {cfg['name']} "
            f"(النموذج `{cfg['model']}` على `{cfg['base_url']}`): {detail}"
        )


def kimi_chat(messages: list[dict[str, str]]) -> str:
    """دالة توافقية لاستدعاء Kimi مباشرة."""
    return call_provider_chat(messages, provider="Kimi")


# ---------- الفحص الدقيق لاتصال المزوّد (Smoke Test) ----------
SMOKE_PASS = "PASS"
SMOKE_INCONCLUSIVE = "INCONCLUSIVE"
SMOKE_FAIL = "FAIL"
SMOKE_BLOCKED = "BLOCKED"
SMOKE_STATES: tuple[str, ...] = (SMOKE_BLOCKED, SMOKE_FAIL, SMOKE_INCONCLUSIVE, SMOKE_PASS)

#: العبارة التي يجب أن يتضمنها رد المزوّد ليُعتبر الفحص ناجحًا.
SMOKE_VERIFICATION_PHRASE = "اختبار الاتصال ناجح"

_SMOKE_ICONS: dict[str, str] = {
    SMOKE_PASS: "✅",
    SMOKE_INCONCLUSIVE: "🟡",
    SMOKE_FAIL: "❌",
    SMOKE_BLOCKED: "⛔",
}

SMOKE_TEST_MESSAGES: list[dict[str, str]] = [
    {
        "role": "system",
        "content": (
            "أنت مساعد فحص اتصال آلي لنظام CELA.PRO. "
            "ردّك يُقرأ آليًا، فأجب بعبارة واحدة فقط دون شرح أو مقدمات."
        ),
    },
    {
        "role": "user",
        "content": (
            "اكتب العبارة التالية كما هي بالحرف الواحد دون أي إضافة أو ترجمة: "
            f"{SMOKE_VERIFICATION_PHRASE}"
        ),
    },
]


def classify_smoke_reply(reply: str | None) -> str:
    """تصنيف رد المزوّد إلى FAIL / INCONCLUSIVE / PASS (دون BLOCKED التي تُقاس قبل الطلب)."""
    text = (reply or "").strip()
    if not text or "❌" in text or "⚠️" in text:
        return SMOKE_FAIL
    if SMOKE_VERIFICATION_PHRASE in text:
        return SMOKE_PASS
    return SMOKE_INCONCLUSIVE


def run_smoke_test(provider: str | None = None) -> dict[str, Any]:
    """نفّذ فحص اتصال لمزوّد واحد وأرجع نتيجة مصنّفة دون كشف أي مفتاح سري.

    الحالات الأربع:
    - ``BLOCKED``: مفتاح المزوّد غير مُعيّن في متغيرات البيئة (لم يُرسل أي طلب).
    - ``FAIL``: فشل الطلب أو أعاد المزوّد رسالة خطأ (``❌`` / ``⚠️``) أو ردًا فارغًا.
    - ``INCONCLUSIVE``: وصل رد من المزوّد لكنه لا يتضمن عبارة التحقق المطلوبة.
    - ``PASS``: وصل رد حقيقي يتضمن عبارة التحقق المطلوبة.
    """
    try:
        cfg = get_provider_config(provider)
    except ValueError as exc:
        return {
            "state": SMOKE_FAIL,
            "provider": (provider or "").strip(),
            "model": "",
            "base_url": "",
            "api_key_env": None,
            "extra_body_env": None,
            "extra_body": {},
            "reply": "",
            "detail": str(exc),
        }

    result: dict[str, Any] = {
        "state": SMOKE_FAIL,
        "provider": cfg["name"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key_env": cfg["api_key_env"],
        "extra_body_env": cfg["extra_body_env"],
        "extra_body": {},
        "reply": "",
        "detail": "",
    }

    if cfg["requires_key"] and not cfg["configured"]:
        result["state"] = SMOKE_BLOCKED
        result["detail"] = (
            f"المفتاح `{cfg['api_key_env']}` غير مُعيّن في متغيرات البيئة "
            f"({cfg['name']} يتطلب مفتاحًا)، لذلك لم يُرسل أي طلب إلى المزوّد."
        )
        return result

    try:
        result["extra_body"] = get_provider_extra_body(cfg)
    except ValueError:
        result["extra_body"] = {}  # رسالة الخطأ ستصل عبر رد المزوّد وتصنَّف FAIL

    reply = redact_secret(
        call_provider_chat(SMOKE_TEST_MESSAGES, provider=cfg["name"]),
        cfg["api_key"],
    )
    result["reply"] = reply
    result["state"] = classify_smoke_reply(reply)
    if result["state"] == SMOKE_FAIL:
        result["detail"] = "فشل الطلب أو أعاد المزوّد رسالة خطأ/ردًا فارغًا."
    elif result["state"] == SMOKE_INCONCLUSIVE:
        result["detail"] = (
            "وصل رد من المزوّد لكنه لا يتضمن عبارة التحقق المطلوبة "
            f"«{SMOKE_VERIFICATION_PHRASE}»."
        )
    else:
        result["detail"] = (
            f"وصل رد حقيقي من المزوّد يتضمن عبارة التحقق "
            f"«{SMOKE_VERIFICATION_PHRASE}»."
        )
    return result


def format_smoke_result(result: dict[str, Any]) -> str:
    """تنسيق نتيجة الفحص الدقيق للعرض في الواجهة (المفاتيح محجوبة دائمًا)."""
    state = str(result.get("state", SMOKE_FAIL))
    icon = _SMOKE_ICONS.get(state, "")
    provider = result.get("provider") or "(غير محدّد)"
    lines = [
        f"### 🧪 الفحص الدقيق لاتصال المزوّد — الحالة: {icon} `{state}`",
        f"- **المزوّد:** `{provider}`",
    ]
    if result.get("model"):
        lines.append(f"- **النموذج:** `{result['model']}`")
    if result.get("base_url"):
        lines.append(f"- **العنوان:** `{result['base_url']}`")
    if result.get("api_key_env"):
        lines.append(f"- **المفتاح:** `{result['api_key_env']}` = `{REDACTED}` (القيمة محجوبة)")
    extra = result.get("extra_body") or {}
    if extra:
        rendered = ", ".join(f"`{key}`" for key in sorted(extra))
        lines.append(f"- **وسائط طلب إضافية:** {rendered} (من `{result.get('extra_body_env')}`)")
    lines.append(f"- **عبارة التحقق المطلوبة:** «{SMOKE_VERIFICATION_PHRASE}»")
    if result.get("detail"):
        lines.append(f"- **التفصيل:** {result['detail']}")
    reply = str(result.get("reply") or "").strip()
    if reply:
        shown = reply if len(reply) <= 400 else f"{reply[:400]}…"
        lines.append(f"- **رد المزوّد:** {shown}")
    return "\n".join(lines)


def smoke_test_provider(provider: str | None = None) -> str:
    """زر «اختبار اتصال المزوّد»: فحص حيّ لمزوّد واحد يُرجع تقريرًا مصنّفًا ومحجوب المفاتيح."""
    return format_smoke_result(run_smoke_test(provider))


_SKILLS_FILE = pathlib.Path("cela_pro_skills.json")


def _load_skills() -> dict[str, Any]:
    with contextlib.suppress(Exception):
        if _SKILLS_FILE.exists():
            data = json.loads(_SKILLS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    return {}


def _save_skills(skills: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        _SKILLS_FILE.write_text(
            json.dumps(skills, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------- محرك المهارات (Skill Engine) ----------
class SkillEngine:
    def __init__(self) -> None:
        self.skills: dict[str, Any] = _load_skills()

    def learn(self, name: str, payload: str) -> str:
        if not name or not name.strip():
            return "⚠️ أدخل اسم المهارة."
        clean_name = name.strip()
        self.skills[clean_name] = payload
        _save_skills(self.skills)
        return f"✅ تم تعلّم المهارة: {clean_name} (الإجمالي: {len(self.skills)})"

    def list_skills(self) -> list[str]:
        if not self.skills:
            return ["(لا توجد مهارات بعد)"]
        return list(self.skills.keys())

    def get(self, name: str) -> Any:
        return self.skills.get(name, "(غير موجودة)")

    def export_json(self) -> str:
        return json.dumps(self.skills, ensure_ascii=False, indent=2)

    def clear(self) -> str:
        self.skills = {}
        _save_skills(self.skills)
        return "🗑️ تم مسح جميع المهارات."


# ---------- OrionAgent ----------
class OrionAgent:
    def __init__(self) -> None:
        self.engine = SkillEngine()
        self.history: list[str] = []

    def run(self, task: str, approved: bool = False) -> str:
        if not task or not task.strip():
            return "⚠️ أدخل مهمة."
        if not approved:
            return (
                "⏸️ في انتظار الموافقة (HITL).\n"
                "فعّل «أوافق» ثم أعد التنفيذ.\n"
                f"المهمة: {task}"
            )
        self.history.append(task)
        skills = self.engine.list_skills()
        return (
            f"[OrionAgent] تنفيذ المهمة: {task}\n"
            f"• المهارات المتاحة: {', '.join(skills)}\n"
            f"• عدد المهارات: {len(self.engine.skills)}\n"
            "• (وضع: محرك محلي — اختر المزوّد من القائمة للمحادثة الذكية)"
        )


ORION = OrionAgent()

# ---------- توفر وحدات Sovereign AI OS (اختياري) ----------
_MODULE_STATUS: dict[str, bool] = {}
for _mod in ("os_skills", "os_memory", "os_know", "os_core"):
    try:
        __import__(_mod)
        _MODULE_STATUS[_mod] = True
    except Exception:
        _MODULE_STATUS[_mod] = False


# ---------- CSS (HEX فقط) ----------
CSS = f"""
.gradio-container {{
    background: {BG} !important;
    color: {TEXT} !important;
    direction: rtl;
    font-family: 'Segoe UI', Tahoma, sans-serif;
}}
.panel {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 16px;
}}
button.primary {{
    background: {ACCENT} !important;
    color: {BG} !important;
    border: none !important;
}}
.status-ok {{ color: {SUCCESS}; }}
.status-info {{ color: {ACCENT}; }}
.status-warn {{ color: {WARN}; }}
.status-err {{ color: {DANGER}; }}
"""

SYSTEM_PROMPT = "أنت CELA.PRO، مساعد ذكي بالعربية لنظام Sovereign AI OS v2026."


def chat_fn(
    message: str,
    history: list[Any] | None = None,
    provider: str | None = None,
) -> str:
    """معالج المحادثة مع توجيه الطلب إلى المزوّد المختار من القائمة."""
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history or []:
        if isinstance(item, dict) and "role" in item and "content" in item:
            messages.append(
                {"role": str(item["role"]), "content": str(item["content"])}
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            if item[0] is not None:
                messages.append({"role": "user", "content": str(item[0])})
            if item[1] is not None:
                messages.append({"role": "assistant", "content": str(item[1])})
    messages.append({"role": "user", "content": str(message)})
    selected_provider = provider or get_default_provider()
    return call_provider_chat(messages, provider=selected_provider)


def get_launch_auth() -> tuple[str, str] | None:
    """إرجاع بيانات مصادقة Gradio عند ضبط GRADIO_USERNAME وGRADIO_PASSWORD."""
    username = os.getenv("GRADIO_USERNAME", "").strip()
    password = os.getenv("GRADIO_PASSWORD", "").strip()
    if username and password:
        return (username, password)
    return None


def get_security_notice() -> str:
    """رسالة تنبيه أمان الرابط العام في حال عدم تفعيل مصادقة Gradio."""
    if get_launch_auth() is not None:
        return (
            "<span class='status-ok'>🔒 حماية الرابط العام مفعّلة "
            "(`GRADIO_USERNAME` / `GRADIO_PASSWORD`).</span>"
        )
    return (
        "<span class='status-warn'>⚠️ تنبيه أمان: لم يتم ضبط `GRADIO_USERNAME` و"
        "`GRADIO_PASSWORD`؛ عند تفعيل رابط Gradio العام (`share=True`) قد يتمكن "
        "آخرون من استهلاك حصص API الخاصة بك.</span>"
    )


def _refresh_provider_ui(provider: str) -> tuple[str, str]:
    """تحديث شريط حالة المزوّد وملخص المزوّدين عند تغيير الاختيار."""
    return format_provider_status(provider), format_all_providers_summary()


# ---------- الواجهة ----------
with gr.Blocks(title="CELA.PRO") as demo:
    gr.Markdown("# 🧠 CELA.PRO — الواجهة العربية لـ Sovereign AI OS v2026")
    security_md = gr.Markdown(get_security_notice())
    provider_status_md = gr.Markdown(format_provider_status(get_default_provider()))

    with gr.Row():
        provider_dropdown = gr.Dropdown(
            choices=PROVIDER_CHOICES,
            value=get_default_provider(),
            label="مزوّد الذكاء الاصطناعي (LLM Provider)",
            info=(
                "اختر المزوّد (Groq / Gemini / OpenRouter / Kimi / Ollama) "
                "دون إرسال المفاتيح للواجهة"
            ),
        )
        refresh_status_btn = gr.Button("تحديث حالة المزوّدين")

    providers_summary_md = gr.Markdown(format_all_providers_summary())
    provider_dropdown.change(
        fn=_refresh_provider_ui,
        inputs=[provider_dropdown],
        outputs=[provider_status_md, providers_summary_md],
    )
    refresh_status_btn.click(
        fn=_refresh_provider_ui,
        inputs=[provider_dropdown],
        outputs=[provider_status_md, providers_summary_md],
    )

    with gr.Row():
        smoke_test_btn = gr.Button("🧪 اختبار اتصال المزوّد", variant="primary")

    smoke_result_md = gr.Markdown(
        "اضغط «🧪 اختبار اتصال المزوّد» لإرسال طلب تحقق واحد إلى المزوّد المختار "
        f"والتأكد من أنه يرد بعبارة «{SMOKE_VERIFICATION_PHRASE}».\n\n"
        "الحالات: `BLOCKED` (المفتاح غير مُعيّن في البيئة) · `FAIL` (فشل الطلب أو رسالة خطأ) · "
        "`INCONCLUSIVE` (وصل رد بلا عبارة التحقق) · `PASS` (وصل رد يتضمن عبارة التحقق). "
        f"قيمة المفتاح تُحجب دائمًا `{REDACTED}`."
    )
    smoke_test_btn.click(
        fn=smoke_test_provider,
        inputs=[provider_dropdown],
        outputs=[smoke_result_md],
    )

    gr.Markdown(
        "### 🔌 وحدات النظام\n"
        + "\n".join(
            f"- {m}: {'✅' if ok else '❌'}" for m, ok in _MODULE_STATUS.items()
        )
    )

    with gr.Row():
        with gr.Column(scale=3):
            gr.ChatInterface(
                fn=chat_fn,
                additional_inputs=[provider_dropdown],
                title="محادثة",
                description=(
                    "تحدث بالعربية أو الإنجليزية باستخدام المزوّد المحدد أعلاه "
                    "(لا يتم التبديل التلقائي بين المزوّدين عند الفشل)."
                ),
            )
        with gr.Column(scale=1):
            gr.Markdown("### 🧩 محرك المهارات (Skill Engine)")
            skill_name = gr.Textbox(label="اسم المهارة", placeholder="مثال: summarize")
            skill_payload = gr.Textbox(label="الحمولة (JSON/نص)", lines=3)
            learn_btn = gr.Button("تعلّم", variant="primary")
            learn_out = gr.Textbox(label="النتيجة", interactive=False)
            learn_btn.click(
                lambda n, p: ORION.engine.learn(n, p),
                inputs=[skill_name, skill_payload],
                outputs=learn_out,
            )

            gr.Markdown("### 🤖 OrionAgent")
            task = gr.Textbox(label="مهمة", placeholder="مثال: لخّص هذا النص")
            approved = gr.Checkbox(label="أوافق (HITL)", value=False)
            run_btn = gr.Button("تنفيذ", variant="primary")
            run_out = gr.Textbox(label="الناتج", lines=6, interactive=False)
            run_btn.click(
                lambda t, a: ORION.run(t, approved=a),
                inputs=[task, approved],
                outputs=run_out,
            )

            gr.Markdown("### 📤 تصدير/مسح")
            export_btn = gr.Button("تصدير JSON")
            export_out = gr.Textbox(label="JSON", lines=6, interactive=False)
            export_btn.click(lambda: ORION.engine.export_json(), outputs=export_out)
            clear_btn = gr.Button("مسح الكل")
            clear_out = gr.Textbox(label="الحالة", interactive=False)
            clear_btn.click(lambda: ORION.engine.clear(), outputs=clear_out)

            gr.Markdown("### 📋 المهارات")
            list_btn = gr.Button("عرض")
            list_out = gr.Textbox(label="القائمة", interactive=False)
            list_btn.click(
                lambda: "\n".join(ORION.engine.list_skills()),
                outputs=list_out,
            )


if __name__ == "__main__":
    _auth = get_launch_auth()
    if _auth is None:
        print(
            "⚠️ تحذير: GRADIO_USERNAME و GRADIO_PASSWORD غير مُعيّنين. "
            "الرابط العام (share=True) سيكون متاحًا بدون كلمة مرور وقد يستهلك حصص API.",
            file=sys.stderr,
        )
    demo.launch(
        share=True,
        server_name="0.0.0.0",
        server_port=7860,
        auth=_auth,
        css=CSS,
    )
