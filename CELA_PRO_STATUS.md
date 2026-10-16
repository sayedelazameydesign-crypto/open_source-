# CELA.PRO — تقرير الحالة ودليل الإعداد

**النظام:** Sovereign AI OS v2026 — الواجهة العربية (CELA.PRO)
**الإصدار:** متعدّد مزوّدي الذكاء الاصطناعي (Groq / Gemini / OpenRouter / Kimi / Ollama) + ألوان HEX
**التاريخ:** 2026-10-16

## ما تم إنجازه

1. `cela_pro_app.py` — تطبيق Gradio بالواجهة العربية (RTL)، يضيف قائمة لاختيار مزوّد الذكاء الاصطناعي من داخل الواجهة، مع الحفاظ على محرك المهارات (`SkillEngine`) و`OrionAgent` والمحادثة الحالية وألوان HEX فقط ✅
2. `CELA_PRO_COLAB.ipynb` — دفتر Colab محدّث يقرأ المفاتيح وبيانات حماية الرابط العام من Colab Secrets ✅
3. `launch_cela_pro.sh` — سكريبت تشغيل يدعم جميع المزوّدين الخمسة وينبّه عند غياب حماية الرابط العام ✅
4. `tests/test_cela_pro_app.py` — أربعة اختبارات تحقق محلية شاملة دون استدعاء APIs حقيقية ✅
5. `CELA_PRO_STATUS.md` — دليل الإعداد وتقرير الحالة هذا ✅

## قائمة مزوّدي الذكاء الاصطناعي (LLM Providers)

تتضمن القائمة في الواجهة خمسة خيارات:

| المزوّد | النموذج الافتراضي | متغير المفتاح | متغير النموذج | متغير العنوان (Base URL) | العنوان الافتراضي |
|---|---|---|---|---|---|
| **Groq** | `openai/gpt-oss-20b` | `GROQ_API_KEY` | `GROQ_MODEL` | `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` |
| **Gemini** | `gemini-2.5-flash` | `GEMINI_API_KEY` | `GEMINI_MODEL` | `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| **OpenRouter** | `openrouter/free` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |
| **Kimi** | `moonshot-v1-8k` | `KIMI_API_KEY` | `KIMI_MODEL` | `KIMI_BASE_URL` | `https://api.moonshot.cn/v1` |
| **Ollama** | `llama3.2:latest` | لا يحتاج مفتاحًا محليًا (`OLLAMA_API_KEY` اختياري) | `OLLAMA_MODEL` | `OLLAMA_BASE_URL` | `http://localhost:11434/v1` |

### قواعد الأمان والتوجيه
- **عدم كشف المفاتيح:** اختيار المزوّد يعرض حالة الإعداد فقط (`مُعيّن` / `غير مُعيّن`) ولا يرسل قيم المفاتيح السرية إلى الواجهة.
- **عدم التبديل التلقائي (No Silent Fallback):** عند غياب مفتاح المزوّد المختار أو فشل الاتصال به، يعرض التطبيق رسالة واضحة خاصة بهذا المزوّد ولا ينتقل تلقائيًا إلى مزوّد آخر.
- **تحديث نموذج Groq الافتراضي:** تم اعتماد `openai/gpt-oss-20b` بديلًا عن `llama-3.3-70b-versatile` المتوقف في الخطة المجانية اعتبارًا من 16 أغسطس 2026.

## حماية الرابط العام في Gradio

لأن التطبيق يعمل برابط عام (`share=True`)، يُنصح بتعيين متغيري المصادقة لحماية حصص API الخاصة بك:

| المتغير | الغرض |
|---|---|
| `GRADIO_USERNAME` | اسم المستخدم لحماية واجهة Gradio العامة |
| `GRADIO_PASSWORD` | كلمة المرور لحماية واجهة Gradio العامة |
| `CELA_DEFAULT_PROVIDER` | المزوّد الافتراضي عند بدء التشغيل (الافتراضي: `Groq`) |

في حال عدم تعيين `GRADIO_USERNAME` و`GRADIO_PASSWORD`، يُصدر التطبيق تحذيرًا واضحًا بأن مستخدمين آخرين قد يستهلكون حصص API عبر الرابط العام.

## خطوات التشغيل المحلي

```bash
# مثال: تشغيل باستخدام Groq مع تفعيل حماية Gradio
export GROQ_API_KEY="gsk_..."
export GRADIO_USERNAME="admin"
export GRADIO_PASSWORD="strong-password"

bash launch_cela_pro.sh
# أو
python cela_pro_app.py
```

## خطوات التشغيل على Google Colab

1. افتح `CELA_PRO_COLAB.ipynb` في Google Colab.
2. أضف الأسرار المطلوبة في قسم **Secrets (🔑)** بالأسماء المطابقة (`GROQ_API_KEY`، `GEMINI_API_KEY`، `OPENROUTER_API_KEY`، `KIMI_API_KEY`، بالإضافة إلى `GRADIO_USERNAME` و`GRADIO_PASSWORD`).
3. شغّل الخلايا بالترتيب (يستنسخ الدفتر المستودع تلقائيًا أو يطلب رفع `cela_pro_app.py` إن لم يكن موجودًا).

## نقاط الانتباه

- الخطط المجانية وحدود الطلبات قابلة للتغيير (على سبيل المثال: خطة OpenRouter المجانية تفرض حدًا يوميًا للطلبات، وواجهة Gemini المتوافقة مع OpenAI تخضع لقيود وسياسة بيانات المستوى المجاني).
- ألوان الواجهة بصيغة HEX فقط (`#0b0f19` …) لتجنّب `SyntaxError`.
