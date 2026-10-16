"""اختبارات التحقق المحلية لتطبيق CELA.PRO ومزوّديه الخمسة وآلية الفحص الدقيق (Smoke Test)."""

from __future__ import annotations

import json
import sys
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
    "GRADIO_USERNAME",
    "GRADIO_PASSWORD",
)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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
    launch_cell = next(source for source in sources if "demo.launch" in source)
    assert sources.index(sync_cell) < sources.index(smoke_cell) < sources.index(launch_cell)
