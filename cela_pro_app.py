"""CELA.PRO — الواجهة العربية لنظام Sovereign AI OS v2026.

تطبيق Gradio (RTL) مع:
- محادثة عبر Kimi API (KIMI_API_KEY)
- محرك مهارات (Skill Engine) + OrionAgent (stubs قابلة للتوسيع)
- ألوان HEX فقط (لا rgb()) لتجنّب SyntaxError في Python
"""

import os
import json
import httpx
import gradio as gr

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

# ---------- إعدادات Kimi ----------
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.getenv("KIMI_MODEL", "moonshot-v1-8k")


def kimi_chat(messages):
    """استدعاء Kimi (OpenAI-compatible). يعود بوضع تجريبي إن لم يوجد مفتاح."""
    if not KIMI_API_KEY:
        return (
            "⚠️ KIMI_API_KEY غير مُعيّن — وضع تجريبي.\n"
            "أضف المفتاح: export KIMI_API_KEY=sk-..."
        )
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{KIMI_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {KIMI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": KIMI_MODEL,
                    "messages": messages,
                    "temperature": 0.7,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - واجهة مستخدم
        return f"❌ خطأ Kimi: {exc}"


import pathlib as _pathlib

_SKILLS_FILE = _pathlib.Path("cela_pro_skills.json")


def _load_skills():
    try:
        if _SKILLS_FILE.exists():
            return json.loads(_SKILLS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_skills(skills):
    try:
        _SKILLS_FILE.write_text(
            json.dumps(skills, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# ---------- محرك المهارات (Skill Engine) ----------
class SkillEngine:
    def __init__(self):
        self.skills = _load_skills()

    def learn(self, name, payload):
        if not name.strip():
            return "⚠️ أدخل اسم المهارة."
        self.skills[name.strip()] = payload
        _save_skills(self.skills)
        return f"✅ تم تعلّم المهارة: {name.strip()} (الإجمالي: {len(self.skills)})"

    def list_skills(self):
        if not self.skills:
            return ["(لا توجد مهارات بعد)"]
        return list(self.skills.keys())

    def get(self, name):
        return self.skills.get(name, "(غير موجودة)")

    def export_json(self):
        return json.dumps(self.skills, ensure_ascii=False, indent=2)

    def clear(self):
        self.skills = {}
        _save_skills(self.skills)
        return "🗑️ تم مسح جميع المهارات."


# ---------- OrionAgent ----------
class OrionAgent:
    def __init__(self):
        self.engine = SkillEngine()
        self.history = []

    def run(self, task, approved=False):
        if not task.strip():
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
            f"• (وضع: محرك محلي — اربط Kimi للذكاء الكامل)"
        )


ORION = OrionAgent()

# ---------- توفر وحدات Sovereign AI OS (اختياري) ----------
_MODULE_STATUS = {}
for _mod in ("os_skills", "os_memory", "os_know", "os_core"):
    try:
        __import__(_mod)
        _MODULE_STATUS[_mod] = True
    except Exception:  # noqa: BLE001
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
.status-warn {{ color: {WARN}; }}
.status-err {{ color: {DANGER}; }}
"""


def chat_fn(message, history):
    """معالج المحادثة."""
    messages = [{"role": "system", "content": "أنت CELA.PRO، مساعد ذكي بالعربية لنظام Sovereign AI OS v2026."}]
    for pair in (history or []):
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            messages.append({"role": "user", "content": str(pair[0])})
            messages.append({"role": "assistant", "content": str(pair[1])})
    messages.append({"role": "user", "content": message})
    return kimi_chat(messages)


# ---------- الواجهة ----------
with gr.Blocks(title="CELA.PRO") as demo:
    gr.Markdown("# 🧠 CELA.PRO — الواجهة العربية لـ Sovereign AI OS v2026")
    gr.Markdown(
        f"<span class='status-ok'>●</span> Kimi: "
        f"{'متصل' if KIMI_API_KEY else 'غير متصل (وضع تجريبي)'}  |  "
        f"Model: {KIMI_MODEL}"
    )
    gr.Markdown(
        "### 🔌 وحدات النظام\n" + "\n".join(
            f"- {m}: {'✅' if ok else '❌'}" for m, ok in _MODULE_STATUS.items()
        )
    )

    with gr.Row():
        with gr.Column(scale=3):
            gr.ChatInterface(
                fn=chat_fn,
                title="محادثة",
                description="تحدث بالعربية أو الإنجليزية.",
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
            run_btn.click(lambda t, a: ORION.run(t, approved=a), inputs=[task, approved], outputs=run_out)

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
            list_btn.click(lambda: "\n".join(ORION.engine.list_skills()), outputs=list_out)


if __name__ == "__main__":
    demo.launch(share=True, server_name="0.0.0.0", server_port=7860, css=CSS)
