"""اختبارات التحقق المحلية لتطبيق CELA.PRO ومزوّديه الخمسة وآلية الفحص الدقيق (Smoke Test)."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cela_pro_app  # noqa: E402

_PROVIDER_ENV_KEYS = (
    "CELA_DEFAULT_PROVIDER",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GROQ_BASE_URL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
    "KIMI_API_KEY",
    "KIMI_MODEL",
    "KIMI_BASE_URL",
    "OLLAMA_API_KEY",
    "OLLAMA_MODEL",
    "OLLAMA_BASE_URL",
    "GROQ_REQUEST_EXTRA_JSON",
    "GEMINI_REQUEST_EXTRA_JSON",
    "OPENROUTER_REQUEST_EXTRA_JSON",
    "KIMI_REQUEST_EXTRA_JSON",
    "OLLAMA_REQUEST_EXTRA_JSON",
    "GRADIO_USERNAME",
    "GRADIO_PASSWORD",
    "CELA_PRO_ADMIN_TOKEN",
    "CELA_KNOW_STORE_URL",
    "CELA_SKILL_STORE_URL",
    "GRADIO_TEMP_DIR",
)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # مكتبة الأبحاث والمهارات معزولة لكل اختبار: لا قراءة من مكتبة المطوّر ولا كتابة فيها.
    monkeypatch.setenv("CELA_PRO_DATA_DIR", str(tmp_path / "cela-pro-data"))
    yield
    for knowledge, _papers, skills, _skills in cela_pro_app._RESOURCE_BUNDLES.values():
        for store in (knowledge, skills):
            with contextlib.suppress(Exception):
                store.close()
    cela_pro_app._RESOURCE_BUNDLES.clear()


def _install_scripted_client(
    monkeypatch: pytest.MonkeyPatch,
    script: list[Any],
) -> list[dict[str, Any]]:
    """استبدال `httpx.Client` بعميل وهمي يُعيد عناصر `script` بالترتيب.

    كل عنصر إمّا نص رد المزوّد أو استثناء يُرفع، وتُسجَّل الطلبات في القائمة المُعادة
    للتحقق من العنوان والترويسات والحمولة دون أي اتصال شبكي حقيقي.
    """
    calls: list[dict[str, Any]] = []
    queue = list(script)

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self._content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": self._content}}]}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
        ) -> _FakeResponse:
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            if not queue:
                raise AssertionError("عدد الطلبات أكبر من المسموح به (لا تبديل تلقائي للمزوّد)")
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return _FakeResponse(str(item))

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return calls


def test_provider_choices_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """1) التحقق من قائمة المزوّدين الخمسة والإعدادات الافتراضية وتخصيص متغيرات البيئة."""
    assert cela_pro_app.PROVIDER_CHOICES == [
        "Groq",
        "Gemini",
        "OpenRouter",
        "Kimi",
        "Ollama",
    ]

    expected_defaults = {
        "Groq": ("openai/gpt-oss-20b", "GROQ_API_KEY", True),
        "Gemini": ("gemini-2.5-flash", "GEMINI_API_KEY", True),
        "OpenRouter": ("openrouter/free", "OPENROUTER_API_KEY", True),
        "Kimi": ("moonshot-v1-8k", "KIMI_API_KEY", True),
        "Ollama": ("llama3.2:latest", None, False),
    }

    for name, (default_model, key_env, requires_key) in expected_defaults.items():
        cfg = cela_pro_app.get_provider_config(name)
        assert cfg["name"] == name
        assert cfg["model"] == default_model
        assert cfg["api_key_env"] == key_env
        assert cfg["requires_key"] is requires_key
        assert cfg["configured"] is (not requires_key)

    # تخصيص النموذج والعنوان والتأكد من عدم تسريب المفتاح السري إلى نص الواجهة
    secret_value = "gsk_super_secret_token_do_not_leak"
    monkeypatch.setenv("GROQ_API_KEY", secret_value)
    monkeypatch.setenv("GROQ_MODEL", "custom-groq-model")
    monkeypatch.setenv("GROQ_BASE_URL", "https://custom.groq.local/v1/")

    groq_cfg = cela_pro_app.get_provider_config("groq")
    assert groq_cfg["configured"] is True
    assert groq_cfg["model"] == "custom-groq-model"
    assert groq_cfg["base_url"] == "https://custom.groq.local/v1"

    status_text = cela_pro_app.format_provider_status("Groq")
    summary_text = cela_pro_app.format_all_providers_summary()
    assert "GROQ_API_KEY" in status_text
    assert "custom-groq-model" in status_text
    assert secret_value not in status_text
    assert secret_value not in summary_text


def test_missing_api_key_returns_clear_message_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2) التحقق من رسالة غياب المفتاح وعدم استدعاء الشبكة أو التبديل التلقائي لمزوّد آخر."""
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-available")

    def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("لا يجوز إجراء اتصال شبكي عند غياب مفتاح المزوّد المختار")

    monkeypatch.setattr(httpx, "Client", _forbidden_client)

    reply = cela_pro_app.chat_fn("اختبار", history=[], provider="Groq")
    assert "Groq" in reply
    assert "GROQ_API_KEY" in reply
    assert "وضع تجريبي" in reply


def test_request_routing_for_cloud_and_ollama_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3) التحقق من توجيه الطلبات إلى عنوان ونموذج المزوّد المختار بما في ذلك Ollama بدون مفتاح."""
    calls: list[dict[str, Any]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "رد ناجح من المزوّد"}}]}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
        ) -> _FakeResponse:
            calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    out_cloud = cela_pro_app.chat_fn(
        "مرحبًا",
        history=[["سؤال سابق", "جواب سابق"]],
        provider="OpenRouter",
    )
    assert out_cloud == "رد ناجح من المزوّد"
    assert len(calls) == 1
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["headers"].get("Authorization") == "Bearer sk-or-test-key"
    assert calls[0]["json"]["model"] == "openrouter/free"
    assert calls[0]["json"]["messages"][-1] == {"role": "user", "content": "مرحبًا"}

    # Ollama لا يحتاج مفتاحًا محليًا
    out_ollama = cela_pro_app.chat_fn("سؤال محلي", history=[], provider="Ollama")
    assert out_ollama == "رد ناجح من المزوّد"
    assert len(calls) == 2
    assert calls[1]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in calls[1]["headers"]
    assert calls[1]["json"]["model"] == "llama3.2:latest"


def test_connection_failure_no_fallback_and_orion_skills_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """4) التحقق من عدم التبديل التلقائي عند الفشل ومصادقة Gradio وOrionAgent."""
    attempts: list[str] = []

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FailingClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, **kwargs: Any) -> Any:
            attempts.append(url)
            raise httpx.ConnectError("تعذر الاتصال بالخادم")

    monkeypatch.setattr(httpx, "Client", _FailingClient)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-backup")

    err_reply = cela_pro_app.chat_fn("اختبر الاتصال", history=[], provider="Gemini")
    assert "❌ فشل الاتصال بمزوّد Gemini" in err_reply
    assert "gemini-2.5-flash" in err_reply
    assert len(attempts) == 1

    # التحقق من إعدادات حماية الرابط العام في Gradio
    assert cela_pro_app.get_launch_auth() is None
    assert "تنبيه أمان" in cela_pro_app.get_security_notice()
    monkeypatch.setenv("GRADIO_USERNAME", "cela_admin")
    monkeypatch.setenv("GRADIO_PASSWORD", "cela_pass")
    assert cela_pro_app.get_launch_auth() == ("cela_admin", "cela_pass")

    # التحقق من بقاء محرك المهارات وOrionAgent مع HITL
    monkeypatch.setattr(cela_pro_app, "_SKILLS_FILE", tmp_path / "skills.json")
    agent = cela_pro_app.OrionAgent()
    assert "في انتظار الموافقة" in agent.run("لخّص التقرير", approved=False)
    learn_msg = agent.engine.learn("summarize", "تلخيص النصوص العربية")
    assert "تم تعلّم المهارة: summarize" in learn_msg
    run_msg = agent.run("لخّص التقرير", approved=True)
    assert "[OrionAgent] تنفيذ المهمة: لخّص التقرير" in run_msg
    assert "summarize" in run_msg


def test_smoke_test_blocked_when_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """5) BLOCKED: غياب المفتاح يمنع إرسال أي طلب، وOllama (بلا مفتاح) لا يُحجب أبدًا."""

    def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("لا يجوز إرسال طلب شبكي عندما يكون مفتاح المزوّد غير مُعيّن")

    monkeypatch.setattr(httpx, "Client", _forbidden_client)

    blocked = cela_pro_app.run_smoke_test("Gemini")
    assert blocked["state"] == cela_pro_app.SMOKE_BLOCKED
    assert blocked["reply"] == ""
    assert "GEMINI_API_KEY" in blocked["detail"]

    report = cela_pro_app.smoke_test_provider("Gemini")
    assert "`BLOCKED`" in report
    assert "GEMINI_API_KEY" in report
    assert "لم يُرسل أي طلب" in report

    # Ollama لا يتطلب مفتاحًا محليًا، لذلك لا يمكن أن تكون حالته BLOCKED
    calls = _install_scripted_client(monkeypatch, [""])
    ollama = cela_pro_app.run_smoke_test("Ollama")
    assert ollama["state"] == cela_pro_app.SMOKE_FAIL
    assert ollama["state"] != cela_pro_app.SMOKE_BLOCKED
    assert len(calls) == 1
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"


def test_smoke_test_classifies_pass_inconclusive_and_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6) PASS / INCONCLUSIVE / FAIL: تصنيف رد المزوّد وفق عبارة التحقق المطلوبة."""
    secret = "gsk_smoke_test_secret_value"
    monkeypatch.setenv("GROQ_API_KEY", secret)
    calls = _install_scripted_client(
        monkeypatch,
        [
            f"العبارة المطلوبة هي: {cela_pro_app.SMOKE_VERIFICATION_PHRASE}",
            "أهلًا بك! كيف أستطيع مساعدتك اليوم؟",
            httpx.ConnectError("تعذر الاتصال بالخادم"),
        ],
    )

    passed = cela_pro_app.run_smoke_test("Groq")
    assert passed["state"] == cela_pro_app.SMOKE_PASS
    assert cela_pro_app.SMOKE_VERIFICATION_PHRASE in passed["reply"]
    assert "عبارة التحقق" in passed["detail"]
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0]["headers"].get("Authorization") == f"Bearer {secret}"
    assert calls[0]["json"]["model"] == "openai/gpt-oss-20b"
    assert calls[0]["json"]["messages"] == cela_pro_app.SMOKE_TEST_MESSAGES

    inconclusive = cela_pro_app.run_smoke_test("Groq")
    assert inconclusive["state"] == cela_pro_app.SMOKE_INCONCLUSIVE
    assert "لا يتضمن عبارة التحقق" in inconclusive["detail"]
    assert "`INCONCLUSIVE`" in cela_pro_app.format_smoke_result(inconclusive)

    failed_report = cela_pro_app.smoke_test_provider("Groq")
    assert "`FAIL`" in failed_report
    assert "فشل الطلب" in failed_report
    assert "تعذر الاتصال بالخادم" in failed_report

    # ثلاثة طلبات لمزوّد واحد فقط: لا تبديل تلقائي إلى مزوّد آخر عند الفشل.
    # تُقارَن مضيفات الطلبات مطابقةً تامةً (لا فحصًا جزئياً للنص) حتى لا يمرّ
    # عنوان مثل https://api.groq.com.example/ كمزوّد صحيح.
    assert len(calls) == 3
    assert all(httpx.URL(call["url"]).host == "api.groq.com" for call in calls)
    assert cela_pro_app.SMOKE_STATES == ("BLOCKED", "FAIL", "INCONCLUSIVE", "PASS")


def test_smoke_test_and_chat_redact_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """7) حجب قيمة المفتاح ([REDACTED]) في رسائل الخطأ ومخرجات الفحص وردود المحادثة."""
    secret = "gsk_super_secret_do_not_leak_9999"
    monkeypatch.setenv("GROQ_API_KEY", secret)
    leaky_error = f"401 Unauthorized — Authorization: Bearer {secret} rejected"
    calls = _install_scripted_client(
        monkeypatch,
        [
            httpx.ConnectError(leaky_error),
            f"المفتاح المستلم هو {secret} — {cela_pro_app.SMOKE_VERIFICATION_PHRASE}",
        ],
    )

    report = cela_pro_app.smoke_test_provider("Groq")
    assert "`FAIL`" in report
    assert "401 Unauthorized" in report
    assert cela_pro_app.REDACTED in report
    assert secret not in report

    echoed = cela_pro_app.chat_fn("تحقق من الاتصال", history=[], provider="Groq")
    assert cela_pro_app.SMOKE_VERIFICATION_PHRASE in echoed
    assert cela_pro_app.REDACTED in echoed
    assert secret not in echoed

    # الحجب يقع على العرض فقط: الطلب الحقيقي ما يزال يحمل المفتاح في الترويسة
    assert len(calls) == 2
    assert calls[0]["headers"].get("Authorization") == f"Bearer {secret}"
    assert calls[1]["headers"].get("Authorization") == f"Bearer {secret}"


def test_provider_status_reports_configuration_not_a_verified_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8) «مُعيّن» لا تعني «متصل»: حالة الإعداد لا تُعلن اتصالًا لم يُتحقَّق منه."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_configured_but_unverified")

    status = cela_pro_app.format_provider_status("Groq")
    assert "GROQ_API_KEY" in status
    assert "مُعيّن" in status
    assert "متصل" not in status
    assert "غير مُتحقَّق" in status
    assert "اختبار اتصال المزوّد" in status

    summary = cela_pro_app.format_all_providers_summary()
    groq_line = next(line for line in summary.splitlines() if "**Groq**" in line)
    assert "مُعيّن" in groq_line
    assert "غير مُختبَر" in groq_line
    assert "✅" not in groq_line

    # غياب المفتاح يبقى تحذيرًا صريحًا كما كان، وOllama المحلي لا يُعلن اتصالًا هو الآخر
    assert "غير مُعيّن" in cela_pro_app.format_provider_status("Kimi")
    assert "KIMI_API_KEY" in cela_pro_app.format_provider_status("Kimi")
    assert "غير مُتحقَّق" in cela_pro_app.format_provider_status("Ollama")


def test_colab_notebook_syncs_source_before_smoke_and_launch() -> None:
    """9) دفتر Colab: مزامنة المصدر من origin/main، والفحص قبل التشغيل، وخلايا صالحة."""
    notebook_path = _REPO_ROOT / "CELA_PRO_COLAB.ipynb"
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    sources = [
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"
    ]

    for index, source in enumerate(sources):
        if source.lstrip().startswith(("!", "%")):
            continue  # أوامر IPython السحرية ليست Python صالحًا بمفردها
        compile(source, f"<cell {index}>", "exec")

    sync_cell = next(source for source in sources if "--ff-only" in source)
    assert "fetch" in sync_cell
    assert "cela_pro_app.local.bak.py" in sync_cell  # لا استبدال صامتًا لنسخة محلية مختلفة
    assert "rev-parse" in sync_cell  # يُعلن التزام المصدر المستخدم بدل إخفائه

    smoke_cell = next(source for source in sources if "run_smoke_test" in source)
    launch_cell = next(source for source in sources if "launch_cela_pro(" in source)
    assert sources.index(sync_cell) < sources.index(smoke_cell) < sources.index(launch_cell)
    # التشغيل يمرّ عبر المساعد المشترك مع CLI حتى لا تُتجاوز المصادقة أو حد حجم الرفع.
    assert not any("demo.launch(" in source for source in sources)

    # كل متغير بيئة يقرؤه التطبيق يجب أن يكون قابلًا للتحميل من Colab Secrets،
    # وإلا تعذّر توجيه مزوّد إلى عنوان مخصّص (مثل نقطة Modal الوسيطة لـ Kimi).
    secrets_cell = next(source for source in sources if "SECRET_KEYS" in source)
    required_env = {
        str(spec[key])
        for spec in cela_pro_app.PROVIDER_SPECS.values()
        for key in ("api_key_env", "optional_key_env", "model_env", "base_url_env")
        if spec.get(key)
    }
    required_env |= {"GRADIO_USERNAME", "GRADIO_PASSWORD", "CELA_DEFAULT_PROVIDER"}
    required_env |= {
        "CELA_PRO_ADMIN_TOKEN",
        "CELA_PRO_DATA_DIR",
        "CELA_KNOW_STORE_URL",
        "CELA_SKILL_STORE_URL",
    }
    missing = sorted(env for env in required_env if f'"{env}"' not in secrets_cell)
    assert not missing, f"متغيرات بيئة ناقصة في خلية أسرار Colab: {missing}"
    assert "TOKEN_ID.TOKEN_SECRET" in secrets_cell  # توثيق صيغة مفتاح وسيط Modal


def test_provider_extra_body_is_optional_and_cannot_hijack_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10) وسائط الطلب الإضافية: اختيارية، تُدمج في الحمولة، ولا تغيّر النموذج أو الرسائل."""
    calls = _install_scripted_client(
        monkeypatch,
        ["رد أول", "رد ثانٍ", cela_pro_app.SMOKE_VERIFICATION_PHRASE],
    )
    monkeypatch.setenv("KIMI_API_KEY", "wk-token-id.token-secret")

    # بدون المتغير: الحمولة تبقى كما كانت تمامًا (توافق خلفي)
    cela_pro_app.chat_fn("مرحبًا", history=[], provider="Kimi")
    assert set(calls[0]["json"]) == {"model", "messages", "temperature"}

    # مع المتغير: تُدمج الوسائط، لكن النموذج والرسائل يبقى مصدرهما التطبيق
    monkeypatch.setenv(
        "KIMI_REQUEST_EXTRA_JSON",
        '{"reasoning_effort": "none", "top_p": 0.9, "model": "hijacked", '
        '"messages": [{"role": "user", "content": "hijacked"}]}',
    )
    cela_pro_app.chat_fn("مرحبًا", history=[], provider="Kimi")
    payload = calls[1]["json"]
    assert payload["reasoning_effort"] == "none"
    assert payload["top_p"] == 0.9
    assert payload["model"] == "moonshot-v1-8k"
    assert payload["messages"][-1] == {"role": "user", "content": "مرحبًا"}

    # الفحص الدقيق يعرض الوسائط المرسلة ويظل PASS
    report = cela_pro_app.smoke_test_provider("Kimi")
    assert "`PASS`" in report
    assert "وسائط طلب إضافية" in report
    assert "KIMI_REQUEST_EXTRA_JSON" in report

    # قيمة غير صالحة: رسالة واضحة دون إرسال أي طلب
    monkeypatch.setenv("KIMI_REQUEST_EXTRA_JSON", "{هذا ليس JSON}")
    broken = cela_pro_app.chat_fn("مرحبًا", history=[], provider="Kimi")
    assert broken.startswith("❌")
    assert "KIMI_REQUEST_EXTRA_JSON" in broken
    assert len(calls) == 3

    # قائمة بدل كائن: مرفوضة أيضًا
    monkeypatch.setenv("KIMI_REQUEST_EXTRA_JSON", '["reasoning_effort"]')
    not_object = cela_pro_app.chat_fn("مرحبًا", history=[], provider="Kimi")
    assert not_object.startswith("❌")
    assert "كائن JSON" in not_object
    assert len(calls) == 3


_SKILL_MD_TEXT = """---
name: summarize-research
description: Summarize a research paper and cite the source.
license: MIT
---

Read the abstract, identify the question, method, and limitations, then cite the paper.
"""


def _library_files(tmp_path: Path) -> list[Path]:
    data_dir = tmp_path / "cela-pro-data"
    return sorted(data_dir.rglob("*")) if data_dir.exists() else []


def test_launch_auth_prefers_gradio_credentials_then_the_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11) المصادقة: GRADIO_* أولًا ثم operator + CELA_PRO_ADMIN_TOKEN، وتشغيل موحّد بحد رفع."""
    assert cela_pro_app.get_launch_auth() is None
    assert "CELA_PRO_ADMIN_TOKEN" in cela_pro_app.get_security_notice()

    monkeypatch.setenv("CELA_PRO_ADMIN_TOKEN", "  operator-secret  ")
    assert cela_pro_app.get_launch_auth() == ("operator", "operator-secret")
    notice = cela_pro_app.get_security_notice()
    assert "🔒" in notice and "CELA_PRO_ADMIN_TOKEN" in notice
    assert "operator-secret" not in notice  # الرمز نفسه لا يظهر في الواجهة

    monkeypatch.setenv("GRADIO_USERNAME", "cela_admin")
    monkeypatch.setenv("GRADIO_PASSWORD", "cela_pass")
    assert cela_pro_app.get_launch_auth() == ("cela_admin", "cela_pass")
    assert "GRADIO_USERNAME" in cela_pro_app.get_security_notice()

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cela_pro_app.demo, "launch", lambda **kwargs: captured.update(kwargs))
    cela_pro_app.launch_cela_pro(share=False)
    assert captured["auth"] == ("cela_admin", "cela_pass")
    assert captured["max_file_size"] == cela_pro_app.MAX_UPLOAD_SIZE == "50mb"
    assert captured["css"] == cela_pro_app.CSS
    assert captured["share"] is False


def test_library_writes_need_the_operator_token_and_feed_chat_as_untrusted_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """12) مكتبة الأبحاث: الكتابة برمز المشغّل فقط، والمؤلفون بـ «؛»، والاسترجاع في المحادثة."""
    paper = (
        "Robust Retrieval",
        "أ. باحث؛ ب. عالم",
        2024,
        "10.1234/robust.retrieval",
        "",
        "CC-BY-4.0",
        "abstract",
        "Semantic retrieval with cited answers improves research assistants.",
        None,
    )
    status, _listing = cela_pro_app._add_research_paper(*paper, "anything")
    assert "CELA_PRO_ADMIN_TOKEN" in status  # الإضافة معطّلة بلا رمز
    monkeypatch.setenv("CELA_PRO_ADMIN_TOKEN", "operator-secret")
    status, _listing = cela_pro_app._add_research_paper(*paper, "wrong-token")
    assert "رمز المشغّل غير صحيح" in status
    assert _library_files(tmp_path) == []  # الرفض لا يلمس المكتبة

    status, listing = cela_pro_app._add_research_paper(*paper, "operator-secret")
    assert status.startswith("✅"), status
    assert "Robust Retrieval" in listing
    assert "أ. باحث; ب. عالم" in listing  # مؤلفان لا مؤلف واحد طويل
    assert "DOI 10.1234/robust.retrieval" in listing

    found = cela_pro_app._search_research_papers("semantic retrieval cited answers")
    assert "Robust Retrieval" in found
    assert "https://doi.org/10.1234/robust.retrieval" in found

    monkeypatch.setenv("GROQ_API_KEY", "gsk_library_test")
    calls = _install_scripted_client(monkeypatch, ["رد مع استشهاد"])
    reply = cela_pro_app.chat_fn("semantic retrieval cited answers", history=[], provider="Groq")
    assert reply == "رد مع استشهاد"
    messages = calls[0]["json"]["messages"]
    assert "غير موثوقة" in messages[0]["content"]
    user_turn = messages[-1]["content"]
    assert user_turn.startswith("semantic retrieval cited answers")
    assert "<retrieved-corpus" in user_turn
    assert "untrusted-web" in user_turn and "10.1234/robust.retrieval" in user_turn


def test_uploaded_files_are_confined_to_the_gradio_upload_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """13) الملف المرفوع يُقبل من مجلد رفع Gradio وحده: لا مسار اعتباطي ولا رابط رمزي ولا بادئة."""
    uploads = tmp_path / "uploads"
    (uploads / "abc123").mkdir(parents=True)
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(uploads))
    monkeypatch.setenv("CELA_PRO_ADMIN_TOKEN", "operator-secret")

    inside = uploads / "abc123" / "study.md"
    inside.write_text("# Study\n\nLocal evidence about cited retrieval.", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("# Secret\n\nnot an upload", encoding="utf-8")
    sibling = tmp_path / "uploads-evil" / "study.md"  # بادئة مشابهة لمجلد الرفع
    sibling.parent.mkdir()
    sibling.write_text("# Evil\n\nprefix trick", encoding="utf-8")
    link = uploads / "link.md"
    link.symlink_to(outside)

    assert cela_pro_app._uploaded_file_path(str(inside)) == os.path.realpath(inside)
    for refused in (outside, sibling, link, uploads / ".." / "secret.md", uploads):
        with pytest.raises(ValueError, match="مسار ملف غير مسموح"):
            cela_pro_app._uploaded_file_path(str(refused))

    no_text = ("", "", None, "", "", "unknown", "full-text", "")
    status, _ = cela_pro_app._add_research_paper(*no_text, str(outside), "operator-secret")
    assert "مسار ملف غير مسموح" in status
    status, listing = cela_pro_app._add_research_paper(*no_text, str(inside), "operator-secret")
    assert status.startswith("✅"), status
    assert "• study —" in listing  # العنوان يُشتق من اسم الملف المرفوع

    skill_upload = uploads / "def456" / "SKILL.md"
    skill_upload.parent.mkdir()
    skill_upload.write_text(_SKILL_MD_TEXT, encoding="utf-8")
    status, _ = cela_pro_app._import_skill_resource("", str(outside), "operator-secret")
    assert "مسار ملف غير مسموح" in status
    status, skills = cela_pro_app._import_skill_resource("", str(skill_upload), "operator-secret")
    assert "candidate" in status, status
    assert "summarize-research — candidate — import — MIT" in skills


def test_reading_an_empty_library_creates_no_store_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """14) فتح الصفحة أو البحث أو المحادثة لا يُنشئ ملفات مكتبة؛ أول كتابة وحدها تفعل."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_library_test")
    calls = _install_scripted_client(monkeypatch, ["رد"])
    assert "لا توجد أوراق" in cela_pro_app._paper_library_text()
    assert "لا توجد مهارات" in cela_pro_app._skill_library_text()
    assert "لا توجد مقاطع" in cela_pro_app._search_research_papers("anything")
    assert cela_pro_app.chat_fn("مرحبًا", history=[], provider="Groq") == "رد"
    assert calls[0]["json"]["messages"][-1] == {"role": "user", "content": "مرحبًا"}
    assert _library_files(tmp_path) == []

    monkeypatch.setenv("CELA_PRO_ADMIN_TOKEN", "operator-secret")
    unlicensed = _SKILL_MD_TEXT.replace("license: MIT\n", "")
    status, _ = cela_pro_app._import_skill_resource(unlicensed, None, "operator-secret")
    assert "missing-license" in status
    status, skills = cela_pro_app._import_skill_resource(_SKILL_MD_TEXT, None, "operator-secret")
    assert "candidate" in status and "summarize-research" in skills
    names = {path.name for path in _library_files(tmp_path)}
    assert {"knowledge.sqlite3", "skills.sqlite3"} <= names
