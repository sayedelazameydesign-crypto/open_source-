# CELA.PRO — تقرير الحالة ودليل الإعداد

**النظام:** Sovereign AI OS v2026 — الواجهة العربية (CELA.PRO)
**الإصدار:** متعدّد مزوّدي الذكاء الاصطناعي (Groq / Gemini / OpenRouter / Kimi / Ollama) + فحص دقيق للاتصال (Smoke Test) + ألوان HEX
**التاريخ:** 2026-10-16

## ما تم إنجازه

1. `cela_pro_app.py` — تطبيق Gradio بالواجهة العربية (RTL)، يضيف قائمة لاختيار مزوّد الذكاء الاصطناعي من داخل الواجهة، مع الحفاظ على محرك المهارات (`SkillEngine`) و`OrionAgent` والمحادثة الحالية وألوان HEX فقط ✅
2. `cela_pro_app.py` — آلية الفحص الدقيق `smoke_test_provider(provider)` مع زر «🧪 اختبار اتصال المزوّد» في الواجهة، وحجب تلقائي لقيمة المفتاح (`[REDACTED]`) ✅
3. `CELA_PRO_COLAB.ipynb` — دفتر Colab محدّث يقرأ المفاتيح وبيانات حماية الرابط العام من Colab Secrets، ويتضمن خلية مزامنة المصدر من `origin/main` وخلية الفحص الدقيق (`Groq` + `Gemini`) قبل خلية تشغيل Gradio ✅
4. `launch_cela_pro.sh` — سكريبت تشغيل يدعم جميع المزوّدين الخمسة وينبّه عند غياب حماية الرابط العام ✅
5. `tests/test_cela_pro_app.py` — تسعة اختبارات تحقق محلية شاملة (المزوّدون، التوجيه، عدم التبديل التلقائي، حالات الفحص الأربع، الحجب، صياغة حالة الإعداد، سلامة دفتر Colab) دون استدعاء APIs حقيقية ✅
6. `CELA_PRO_STATUS.md` — دليل الإعداد وتقرير الحالة هذا ✅

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
- **الإعداد لا يعني الاتصال:** شريط الحالة وملخص المزوّدين يصفان وجود المفتاح في متغيرات البيئة فحسب (`🔑 مُعيّن — غير مُختبَر`)، ولا يُعلنان اتصالًا ناجحًا؛ وجود `GROQ_API_KEY` لا يعني أنه صالح أو أن النموذج والعنوان يعملان. الإعلان الوحيد عن اتصال يعمل هو نتيجة الفحص الدقيق `PASS`.
- **عدم التبديل التلقائي (No Silent Fallback):** عند غياب مفتاح المزوّد المختار أو فشل الاتصال به، يعرض التطبيق رسالة واضحة خاصة بهذا المزوّد ولا ينتقل تلقائيًا إلى مزوّد آخر.
- **تحديث نموذج Groq الافتراضي:** تم اعتماد `openai/gpt-oss-20b` بديلًا عن `llama-3.3-70b-versatile` المتوقف في الخطة المجانية اعتبارًا من 16 أغسطس 2026.

## الفحص الدقيق لاتصال المزوّد (Smoke Test)

زر «🧪 اختبار اتصال المزوّد» في الواجهة — والدالة `smoke_test_provider(provider)` في الكود — يرسل **طلب تحقق واحدًا** إلى المزوّد المختار يطلب فيه الرد بعبارة التحقق، ثم يُصنّف النتيجة إلى أربع حالات منفصلة:

| الحالة | المعنى | سببها |
|---|---|---|
| `BLOCKED` | لم يُرسل أي طلب | مفتاح المزوّد غير مُعيّن في متغيرات البيئة (`*_API_KEY`) |
| `FAIL` | فشل الفحص | تعذّر الطلب، أو أعاد المزوّد رسالة خطأ (`❌` / `⚠️`)، أو وصل رد فارغ |
| `INCONCLUSIVE` | نتيجة غير حاسمة | وصل رد من المزوّد لكنه لا يتضمن عبارة التحقق المطلوبة |
| `PASS` | نجح الفحص | وصل رد حقيقي يتضمن عبارة التحقق «اختبار الاتصال ناجح» |

### دوال الآلية في `cela_pro_app.py`

| الدالة | الدور |
|---|---|
| `run_smoke_test(provider)` | تنفيذ الفحص وإرجاع نتيجة مصنّفة (`state` / `reply` / `detail` / بيانات المزوّد) |
| `classify_smoke_reply(reply)` | تصنيف نص الرد إلى `FAIL` / `INCONCLUSIVE` / `PASS` |
| `format_smoke_result(result)` | تنسيق التقرير المعروض في الواجهة |
| `smoke_test_provider(provider)` | الدالة المرتبطة بالزر (تنفيذ + تنسيق) |
| `redact_secret(text, secret)` | حجب قيمة المفتاح بـ `[REDACTED]` في رسائل الخطأ وردود المزوّد ومخرجات الفحص |

عبارة التحقق ثابتة في `SMOKE_VERIFICATION_PHRASE` («اختبار الاتصال ناجح») ورسائل الطلب في `SMOKE_TEST_MESSAGES`.

### ضمانات الأمان

- **الحجب التلقائي:** لا تظهر قيمة أي مفتاح API في تقرير الفحص ولا في رسائل أخطاء `call_provider_chat` ولا في ردود المحادثة؛ تُستبدل دائمًا بـ `[REDACTED]`، بينما يبقى المفتاح داخل ترويسة `Authorization` في الطلب الفعلي فقط.
- **لا تبديل تلقائي:** الفحص يخاطب المزوّد المختار وحده ولا ينتقل إلى مزوّد آخر عند الفشل أو غياب المفتاح.
- **بلا أسرار في الواجهة:** التقرير يعرض اسم متغير المفتاح (`GROQ_API_KEY` مثلًا) وحالته، لا قيمته.

### الفحص داخل دفتر Colab

خلية الفحص الدقيق موضوعة في `CELA_PRO_COLAB.ipynb` **قبل** خلية تشغيل Gradio، وتفحص `Groq` و`Gemini` معًا ثم تطبع ملخصًا مثل:

```text
ملخص الفحص الدقيق: {'Groq': 'PASS', 'Gemini': 'BLOCKED'}
```

## وصفة: ربط `Kimi` بنقطة وسيطة متوافقة مع OpenAI (مثل Modal)

أي نقطة وسيطة تتحدّث بروتوكول OpenAI Chat Completions تعمل مع مزوّد `Kimi` عبر متغيرات البيئة وحدها، دون تعديل في الكود:

| المتغير | القيمة |
|---|---|
| `KIMI_BASE_URL` | عنوان النقطة منتهيًا بـ `/v1` (التطبيق يضيف `/chat/completions`)، مثل `https://<workspace>--ep-<endpoint>.<region>.modal.direct/v1` |
| `KIMI_MODEL` | اسم النموذج كما تعلنه النقطة، مثل `moonshotai/Kimi-K3` |
| `KIMI_API_KEY` | الصيغة المركّبة التي يقبلها الوسيط: `TOKEN_ID.TOKEN_SECRET` |
| `KIMI_REQUEST_EXTRA_JSON` | اختياري: كائن JSON يُدمج في جسم الطلب لوسائط الوسيط أو النموذج، مثل `{"reasoning_effort": "none"}` |
| `CELA_DEFAULT_PROVIDER` | `Kimi` |

> كل مزوّد يقرأ المتغيرات الأربعة نفسها باسمه (`GROQ_*` / `GEMINI_*` / `OPENROUTER_*` / `KIMI_*` / `OLLAMA_*`)، ومتغير `*_REQUEST_EXTRA_JSON` اختياري تمامًا: إن تُرك فارغًا تبقى حمولة الطلب كما هي (`model` + `messages` + `temperature`). ولا يمكن للحقول الإضافية استبدال `model` أو `messages` — فهما يأتيان دائمًا من إعداد المزوّد ومن المحادثة. والقيمة غير الصالحة تُنتج رسالة `❌` واضحة **دون إرسال أي طلب**. قالب فارغ لكل المتغيرات متاح في `.env.example`.

محليًا:

```bash
# ضع هذه الأسطر في ملف .env (مُدرج في .gitignore) أو صدّرها في الجلسة — لا ترفعها إلى المستودع
export KIMI_BASE_URL="https://<workspace>--ep-<endpoint>.<region>.modal.direct/v1"
export KIMI_MODEL="moonshotai/Kimi-K3"
export KIMI_API_KEY="<TOKEN_ID>.<TOKEN_SECRET>"
export KIMI_REQUEST_EXTRA_JSON='{"reasoning_effort": "none"}'   # اختياري
export CELA_DEFAULT_PROVIDER="Kimi"
bash launch_cela_pro.sh
```

وعلى Colab أضف `KIMI_API_KEY` و`KIMI_BASE_URL` و`KIMI_MODEL` (و`KIMI_REQUEST_EXTRA_JSON` إن لزم) في قسم **Secrets (🔑)**؛ خلية الأسرار في الدفتر تقرأ كل متغيرات المزوّدين (`*_API_KEY` و`*_MODEL` و`*_BASE_URL` و`*_REQUEST_EXTRA_JSON`) وتطبع **أسماء** ما حُمّل فقط دون القيم.

### قراءة نتيجة الفحص ضد نقطة وسيطة

| النتيجة | المعنى عمليًا |
|---|---|
| `PASS` | النقطة قبلت المفتاح وردّت بعبارة «اختبار الاتصال ناجح» |
| `FAIL` | رفض الوسيط للمفتاح (`401`)، أو تعذّر الوصول إلى العنوان، أو ردّ فارغ |
| `INCONCLUSIVE` | وصل رد حقيقي بصياغة أخرى — سلوك متوقّع من نماذج الاستدلال/التفكير؛ اضبط `KIMI_REQUEST_EXTRA_JSON='{"reasoning_effort": "none"}'` ثم أعد الفحص |
| `BLOCKED` | `KIMI_API_KEY` غير مُعيّن أصلًا، ولم يُرسل أي طلب |

> ⚠️ لا تلصق قيمة `TOKEN_SECRET` في تعليق على issue أو في مخرجات اختبار أو في الدفتر؛ التطبيق يحجبها `[REDACTED]` في كل ما يعرضه، لكنه لا يستطيع حجبها مما تكتبه أنت يدويًا. وإن تسرّبت يومًا، أنشئ توكن جديدًا من إعدادات مساحة العمل وألغِ القديم.

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

بعد فتح الواجهة اختر المزوّد من القائمة ثم اضغط «🧪 اختبار اتصال المزوّد» للتأكد من وصول رد حقيقي قبل بدء المحادثة. ويمكن استدعاء الفحص من Python مباشرة:

```python
import cela_pro_app

print(cela_pro_app.smoke_test_provider("Groq"))        # تقرير منسّق (المفتاح محجوب)
cela_pro_app.run_smoke_test("Groq")["state"]           # PASS / INCONCLUSIVE / FAIL / BLOCKED
```

## خطوات التشغيل على Google Colab

1. افتح `CELA_PRO_COLAB.ipynb` في Google Colab.
2. أضف الأسرار المطلوبة في قسم **Secrets (🔑)** بالأسماء المطابقة (`GROQ_API_KEY`، `GEMINI_API_KEY`، `OPENROUTER_API_KEY`، `KIMI_API_KEY`، بالإضافة إلى `GRADIO_USERNAME` و`GRADIO_PASSWORD`).
3. شغّل الخلايا بالترتيب. **خلية مزامنة المصدر** تنفّذ `git fetch` ثم `git pull --ff-only origin main` (أو استنساخًا عند أول تشغيل)، وتُحدّث `cela_pro_app.py` من المستودع، وتطبع **التزام المصدر** المستخدم وبصمة الملف. فإن كان لديك ملف محلي مختلف تُحفظ نسخة منه في `cela_pro_app.local.bak.py` قبل الاستبدال، وإن تعذّر التحديث يظهر تحذير صريح بدل استخدام مصدر قديم بصمت.
4. راجع مخرجات **خلية الفحص الدقيق** قبل تشغيل الواجهة: الحالة `PASS` تعني أن المزوّد رد بعبارة «اختبار الاتصال ناجح»، و`BLOCKED` تعني أن المفتاح ناقص في Colab Secrets، و`INCONCLUSIVE` تعني أن الرد وصل لكنه لا يتضمن عبارة التحقق.

## نقاط الانتباه

- الخطط المجانية وحدود الطلبات قابلة للتغيير (على سبيل المثال: خطة OpenRouter المجانية تفرض حدًا يوميًا للطلبات، وواجهة Gemini المتوافقة مع OpenAI تخضع لقيود وسياسة بيانات المستوى المجاني).
- ألوان الواجهة بصيغة HEX فقط (`#0b0f19` …) لتجنّب `SyntaxError`.
- الفحص الدقيق يستهلك طلبًا واحدًا من حصة المزوّد في كل ضغطة، وبعض النماذج قد تُضيف مقدمة إلى الرد؛ لهذا تُصنَّف هذه الحالة `INCONCLUSIVE` (رد وصل بلا عبارة التحقق) بدلًا من `FAIL`.
