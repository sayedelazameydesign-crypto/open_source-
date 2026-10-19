# Celia.sh — Deployment Runbook (A1/A6 → C5 → C6)

> اقرأ هذا قبل أي نشر. المسار الآمن هو: إثبات المزوّد الحقيقي محليًا، ثم النشر على Render مع Postgres. لا تقفز خطوات.

## المتغيرات التي لا تُوضع في Git أبدًا

| المتغير | أين يُخزَّن | وصف |
| --- | --- | --- |
| `GEMINI_API_KEY` | Render env secret / `.env` محلي | مفتاح Gemini الحقيقي |
| `CELA_API_TOKEN` | Render env secret | رمز الـBearer للواجهة العامة |
| `CELA_PRIVATE_API_TOKEN` | Render env secret | رمز منفصل لـ`/cela/v1` |
| `CELA_RUN_STORE_URL` | Render env secret | سلسلة اتصال Postgres (`postgresql://...`) |
| `CELA_PROVIDER_BASE_URL` | Render env (عام) | عنوان مزود OpenAI-compatible (البديل القديم `CELA_MODEL_BASE_URL` مدعوم للتوافق) |
| `CELA_PROVIDER_MODEL` | Render env (عام) | اسم النموذج (مثل `gemini-2.5-flash`، البديل القديم `CELA_MODEL_ID` مدعوم) |
| `CELA_PROVIDER_CREDENTIAL_VAR` | Render env (عام) | اسم متغير البيئة الذي يحمل المفتاح (افتراضي `GEMINI_API_KEY`). يُقبل أيضًا الاسم القديم `CELA_PROVIDER_API_KEY_ENV` للتوافق الخلفي. |
| `CELA_USE_LIVE_PROVIDER` | Render env (عام) | `1` لتفعيل المسار الحقيقي |
| `CELA_ACTIVEPIECES_WEBHOOK_URL` | Render env secret | رابط Activepieces webhook (اختياري) |
| `CELA_ACTIVEPIECES_WEBHOOK_SECRET` | Render env secret | سر الاشتراك في webhook |

## المرحلة أ — اختبار المزوّد محليًا (A1/A6)

```bash
read -rsp "Gemini API key: " GEMINI_API_KEY && printf '\n'
export GEMINI_API_KEY

export CELA_PROVIDER_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/"
export CELA_PROVIDER_MODEL="${CELA_PROVIDER_MODEL:-gemini-2.5-flash}"
export CELA_PROVIDER_CREDENTIAL_VAR=GEMINI_API_KEY
export CELA_USE_LIVE_PROVIDER=1
export CELA_API_TOKEN=local-test-token
export CELA_PRIVATE_API_TOKEN=local-test-private-token

python scripts/test_gemini_provider.py
```

شروط النجاح (السكربت يطبعها كلها `PASS`):

1. طلب صادر فعلي إلى Gemini — يسجّل السكربت حالة كل طلب HTTP (200) وعنوانه والنموذج المرسل، دون طباعة المفتاح في السجلات أو الرد.
2. تنفيذ أداة حقيقي (`python_execute`) مع نتيجة ناجحة.
3. `execution.kind == "provider_adapter"` في نهاية المهمة (لا قبلها فقط)، مع `fallback_triggered=False` وتطابق هوية المزوّد مع الإعداد.
4. عند إجبار فشل (401) يتحول التنفيذ إلى `scripted` مع `fallback_triggered=True` وسبب محجوب يذكر 401.
5. عند غياب المفتاح تفشل **نقاط الدخول الفعلية** مغلقةً: `create_app()` يرفع `RuntimeError` قبل بدء الخدمة، و`python -m os_server.cli run --live` يخرج برمز 2، دون تشغيل نسخة scripted صامتة.
6. **`tool_calls` اثنان في دور واحد** يمرّان إلى طلب متابعة حقيقي: يُحقن الدور الأول
   صناعيًا (لا يمكن مطالبة مزوّد بإصدار نداءين بالضبط، وهذا مذكور صراحةً في السطر
   المُسجَّل)، ثم تُنفَّذ الأداتان فعلًا، ويُرسَل طلب المتابعة **الحقيقي** حاملًا
   `assistant.tool_calls` ومعرّفي النداءين ونتيجتيهما. الدليل ليس الـ200 وحده:
   يُفحص **شكل جسم الطلب** نفسه (النموذج، `messages`، `tools` وفيها
   `python_execute`، ومحتوى النتيجتين)، فطلبٌ ناجح بجسم ناقص لا يجتاز.
7. **المزوّد نفسه يُصدر نداءين في دور واحد** (بلا أي حقن): تُسجَّل الطلبات الحية
   وتُفتَّش عن دور `assistant` واحد يحمل `tool_calls` اثنين على الأقل، تعود
   نتيجتاهما بـ`tool_call_id` مطابقين بالترتيب، ويُجاب طلب المتابعة الحامل
   لهما بـ200. وحدة الاختبار دون اتصال تتحقق من منطق هذا الفحص فقط؛
   **الدليل الحي وحده هو تشغيل Actions بالمفتاح الحقيقي**، والنقل الوهمي
   (mock) لا يُحتسب دليلًا حيًا.

يطبع السكربت في ترويسته **الـSHA الكامل** (40 حرفًا، من `GITHUB_SHA` في Actions أو
`git rev-parse HEAD` محليًا) داخل `live-acceptance.log` نفسه، ويطبعه Workflow أيضًا
في خطوة الـPreflight. عند التحقق قارن سطر `commit sha:` في السجل المرفوع بقيمة
`git rev-parse HEAD` وبـcommit التشغيل في Actions؛ سطر `PASS` بلا SHA مطابق
لا يُنسب إلى شجرة بعينها، ولا يُقبل دليلًا. وينتهي السجل الناجح بسطر الانتقال
الصحيح: `Proceed to C5 (Render deploy), then C6 (PostgreSQL persistence)` —
أي أن المرحلة التالية هي **C5 (نشر Render)** ثم **C6 (استمرارية PostgreSQL)**،
وبقاء A1/A6 خضراء لا يُغلق أيًا منهما.

ثم اختبر تشغيل الخادم محليًا **بمفتاح حقيقي** (خطوة يدوية سريعة):

```bash
export CELA_RUN_STORE_URL="sqlite:///./cela_runs.db"
python -m os_server.cli serve --host 127.0.0.1 --port 8080
# في طرفية ثانية:
curl -s -H "Authorization: Bearer $CELA_API_TOKEN" http://127.0.0.1:8080/mission/status
# تأكد من أن payload يحتوي على execution.kind == "provider_adapter"
```

### تشغيل A1/A6 من GitHub Actions (بلا تمرير المفتاح إلى أي طرفية)

GitHub لا يعرض قيمة أي سر بعد حفظه — لا للمستخدم ولا للوكلاء — والطريقة الوحيدة
لاستخدامه هنا هي تشغيل Workflow. لهذا يوجد `.github/workflows/live-acceptance.yml`:

- يقرأ `GEMINI_API_KEY` من أسرار المستودع فقط، ويضعه في بيئة الخطوة (لا في argv ولا في URL).
- الوجهة ثابتة داخل الملف (`generativelanguage.googleapis.com`)، وليست مدخلًا يمكن تغييره عند التشغيل.
- يتخطى الخطوة الحية إن لم يكن `ALLOW_LIVE_EXTERNAL_CALLS` هو النص `true`، فلا يستهلك حصة.
- يحاول رفع `live-acceptance.log` كـArtifact دائمًا، حتى عند فشل أحد المعايير؛ لا يوجد سجل حي إذا تخطّى التشغيل أو فشل الفحص الأولي.

`workflow_dispatch` لا يظهر في الواجهة قبل وجود الملف على الفرع الافتراضي، لذلك يعمل أيضًا
على `push` يغيّر الملف نفسه على فرع غير `main` (أول التزام يُضيفه). وبعد الدمج يكفي: Actions → Live acceptance → Run workflow.
لتغيير النموذج دون تعديل الملف، أضف متغير مستودع `CELA_LIVE_MODEL` بالقيمة المطلوبة.
لا يُعد نجاح Workflow مع تخطي الخطوة الحية دليلًا على اجتياز A1/A6؛ تبقى A1/A6 وC5 وC6 مفتوحة حتى وصول أدلتها الفعلية.

### تشغيل C6 من GitHub Actions (بلا تمرير أي DSN إلى طرفية)

نفس قاعدة A1/A6: قيمة الـDSN لا تظهر لأحد بعد حفظها سرًّا، فإثبات C6 الحي
يكون بتشغيل `.github/workflows/postgres-persistence.yml`:

1. أنشئ **مشروع Supabase ثانيًا مخصصًا للاختبار** (Free، بلا بطاقة) — قاعدة
   الإنتاج الحالية لا تُكتب أبدًا بصفتها هدفًا، والسكربت يرفض أي هدف يحلّ إلى
   نفس القاعدة (نفس مشروع Supabase عبر الـpooler والمباشر = قاعدة واحدة).
2. في **Settings → Secrets and variables → Actions** أضف ثلاثة أسرار (الأسماء
   فقط تُذكر هنا؛ القيم لا تُكتب في محادثة ولا في كود):
   * `CELA_TEST_POSTGRES_DSN` — رابط **Session pooler** (منفذ `5432`، مع
     `?sslmode=require`) لقاعدة الاختبار. هو هدف الكتابة الوحيد.
   * `CELA_RUN_STORE_URL` — رابط قاعدة الإنتاج، **مرجع مقارنة فقط**: يُقارن
     ولا يُتصل به ولا يُكتب إليه أبدًا. غيابه لا يوقف التشغيل لكنه يجعل
     النتيجة موسومة `SEPARATION IS UNVERIFIED`، فلا تُغلق بها C6.
   * `CELA_ALLOW_C6_WRITE` = النص `true` حرفيًا — مفتاح تسليح الكتابة (نفس
     نمط `ALLOW_LIVE_EXTERNAL_CALLS` في A1/A6). وجود الـDSN وحده لا يُسلّح
     كتابة: بدونه يبقى الـPreflight أحمر ولا تُلمس أي قاعدة، فلا تتحول C6
     خضراء إلا بعد كتابة وتحقق وحذف فعليين.
3. شغّل: Actions → **Postgres persistence (C6, real PostgreSQL)** → Run workflow.
   الدليل هو تعليق بعنوان `C6`: ‏`C6 at <sha>` حيث `<sha>` يساوي `headSha`
   لتشغيلك أنت (رأس الفرع لحظة إعادة التشغيل، لا رقمًا منقولًا من خطة)،
   وسطر `[PASS] PostgreSQL run/state/events/artifact/ticket persist…`،
   وArtifact باسم `postgres-persistence-log`. السكربت يكتب ثم **يحذف صفوفه**
   افتراضيًا، ويطبع `commit sha:` من `GITHUB_SHA` في ترويسته.

> أسرار GitHub لا تصل إلى Render تلقائيًا: ما يُحفظ في Actions يخدم
> Workflows فقط، وما تحتاجه الخدمة الحية (`CELA_API_TOKEN`,
> `CELA_PRIVATE_API_TOKEN`, `CELA_RUN_STORE_URL`, `GEMINI_API_KEY`) يُدخل
> مرة أخرى في لوحة Render نفسها بصفته Environment secrets.

### تصحيحات على خطط «النشر المجاني» المتداولة لهذا المستودع

* **`render.yaml` موجود أصلًا** في الجذر وبصيغة هذا المشروع تحديدًا: بناء
  `pip install -e ".[server,distributed]"` من `pyproject.toml` (لا يوجد
  `requirements.txt` ولا نقطة دخول `app:app`/`gunicorn`). أي قالب عام يقترح
  غير ذلك سيبني خدمة لا تُقلع.
* **أمر الإقلاع عاملٌ واحد مقصودًا:** `uvicorn os_server.app:create_app --factory`
  بلا `--workers`. على ذاكرة الخطة المجانية المحدودة، كل عامل إضافي نسخة
  كاملة من العملية؛ عاملان أو أكثر مخاطرة ذاكرة لا مكسب.
* **`DATABASE_URL` لا يقرأه التطبيق.** المتغير المقروء هو `CELA_RUN_STORE_URL`
  فقط. قاعدة Render المُدارة تُستخدم بكتابة قيمتها **داخل** `CELA_RUN_STORE_URL`
  في اللوحة، وقاعدة Supabase/Neon الخارجية تُستخدم برابط Session pooler
  بالصيغة نفسها — لا يوجد `fromDatabase` في الـBlueprint الحالي لأن القاعدة
  خارجية عمدًا (قاعدة Render المجانية قصيرة العمر؛ تحقق من مهلتها في اللوحة
  قبل اختيارها قاعدة دائمة).
* **إبقاء الخدمة مستيقظة بـping خارجي ليس بلا كلفة:** انظر تنبيه النوم في
  [`DEPLOY_FREE_TIER_RUNBOOK.md`](DEPLOY_FREE_TIER_RUNBOOK.md) §٢ — يستهلك
  ساعات التشغيل المجانية باستمرار.

## ما لا يعنيه هذا الملف — C5 وC6 على وجه التحديد

مرحلتان مختلفتان، ولا تُغني إحداهما عن الأخرى:

| | C5 — خدمة الويب على Render | C6 — استمرارية PostgreSQL |
|---|---|---|
| ما يُثبته | أن العملية تُبنى وتعمل من `render.yaml`/إعداد يدوي، وأن `/health` و`/runtime-info` يردّان بهوية تنفيذ صحيحة | أن الحالة والأحداث والـArtifacts وتذاكر WebSocket تبقى بعد إعادة الاتصال/إعادة التشغيل، وتُقرأ عبر اتصال جديد |
| ما يحتاجه | حساب Render + متغيّرات البيئة (بلا بطاقة) | قاعدة اختبار **منفصلة** + `CELA_RUN_STORE_URL` لقاعدة التطبيق كمرجع مقارنة فقط |
| ما لا يُثبته | لا يُثبت أي استمرارية: بلا `CELA_RUN_STORE_URL` الخدمة على Render **بلا حالة** وتفقد كل تشغيل عند إعادة التشغيل | لا يُثبت أن النشر نفسه ناجح؛ اختباره يجري محليًا ضد قاعدة الاختبار |

حدود الحالة الحالية — تُقرأ قبل أي كلمة عن «جاهز للإنتاج»:

* **C5 لم يُنفَّذ:** لا نشر على Render، ولا `render.yaml` مُطبَّق على خدمة حقيقية.
* **C6 لم يُنفَّذ:** لا قاعدة اختبار حقيقية، ولا تشغيل لـ`scripts/test_postgres_persistence.py`
  ضد Postgres حيّ. ما يغطّيه CI هو مزوّدو DBAPI مزيَّفون على SQLite فقط (انظر [`EVIDENCE.md`](EVIDENCE.md) §٣).
* لذلك هذا المستودع **ليس «جاهزًا للإنتاج»**، وأي وصف يقول غير ذلك يخالف هذا الملف.

## المرحلة C5 — النشر على Render

1. اربط المستودع بـ Render واختر خطة Free.
2. استخدم `render.yaml` الموجود في جذر المشروع (Blueprint) أو اضبط نفس المتغيرات يدويًا.
3. **لا تضف** `GEMINI_API_KEY` في القيم العامة (non-secret). أضفه كـ Secret.
4. `render.yaml` يضبط `CELA_USE_LIVE_PROVIDER=0` (وضع معاينة) و`CELA_PROVIDER_*` مسبقًا، لذلك ينشر Blueprint بنجاح **بلا أي مفتاح**. فعّل الوضع الحي بخطوتين: أضف `GEMINI_API_KEY` كسر (Secret)، ثم غيّر العلم إلى `1` وأعد النشر. الخادم **يفشل سريعًا** (fail fast) إذا رأى العلم مفعّلًا بلا مزوّد صالح — وهذه الميزة تمنع نشرًا يوحي بأنه مباشر وهو في الحقيقة تجريبي. قبل النشر يلتقط `scripts/deploy_doctor.py` الحالة نفسها بالخطأ `live_provider_unconfigured`.
5. اسم النموذج ليس مضمونًا: `CELA_PROVIDER_MODEL` مجرد إعداد، وأهلية المفتاح تحددها Google. إن رفض المزوّد النموذج الافتراضي (`gemini-2.5-flash`)، غيّره وأعد تشغيل A1/A6 وسجّل التغيير بدل افتراض النجاح.
6. بعد اكتمال النشر، اضرب `/health` بدون رمز وتحقق من `{"status":"ok"}` (هذا المسار بسيط عمدًا لفحص موازن الأحمال). استخدم `/runtime-info` بترويسة `Authorization: Bearer $CELA_API_TOKEN` للتحقق من هوية التنفيذ الفعلية:
   ```json
   {"execution":{"kind":"provider_adapter","model_name":"gemini-gemini-2.5-flash","goal_verification":"not_verified"}}
   ```
   لو ظهر `scripted` بعد تمكين العلم، فهناك خطأ إعداد (عادةً اسم المتغير أو المفتاح). ستجد الرسالة الواضحة في سجلات بدء التشغيل — ولن تبدأ الخدمة أصلًا في حالة الإعداد الناقص.

## المرحلة C6 — ربط PostgreSQL والتحقق من استمرارية البيانات

1. أنشئ قاعدة Supabase/Render Postgres مجانية وانسخ سلسلة الاتصال.
2. عيّن `CELA_RUN_STORE_URL=postgresql://user:pass@host:5432/postgres` كسر في Render.
   - في حال Postgres المدار من Render يمكنك استخدام `$DATABASE_URL`.
3. محليًا، استخدم **قاعدة اختبار مخصصة** (وليست قاعدة الإنتاج) وشغّل اختبار الفصل الحقيقي للاتصال:
   ```bash
   export CELA_TEST_POSTGRES_DSN="postgresql://user:pass@host:5432/cela_test"
   export CELA_RUN_STORE_URL="postgresql://.../postgres"   # مرجع الإنتاج للمقارنة فقط
   python scripts/test_postgres_persistence.py --yes
   ```
   - `CELA_RUN_STORE_URL` **ليس هدف الكتابة أبدًا**؛ هو مرجع المقارنة فقط. إن لم يكن معرّفًا في طرفية الاختبار، مرّره صراحةً بـ`--production-dsn "postgresql://…"`. وغياب المرجع لا يُفسَّر كدليل على الانفصال: السكربت يرفض الكتابة بدونه، ولا يسمح بالمتابعة إلا بعلم صريح `--no-production-reference` يُعلن عندها أن العزل **غير مُتحقَّق**.
   - يُرفض أي DSN يطابق **نفس القاعدة**: مرجع مشروع Supabase يُقرأ من المضيف `db.<ref>.supabase.co` (الاتصال المباشر) أو من اسم المستخدم `postgres.<ref>` (Session pooler)؛ فمشروعان يتشاركان مضيف الـpooler يبقى كل منهما قاعدة مستقلة، ونفس المشروع عبر pooler ومباشر يُعد قاعدة واحدة. وعند تعذّر إثبات المرجع، يكفي تطابق المضيف/المنفذ/القاعدة للحكم بالرفض **حتى لو اختلف اسم المستخدم**.
   - `--yes` مطلوب قبل أي كتابة.
   - يطبع تحذيرًا واضحًا بأنه **يكتب ثم يحذف** سجلاته؛ استخدم `--keep` فقط إذا أردت فحص السجلات بعد النجاح.
   - ينفّذ مهمة تكتب ملفًا معروف البصمة عبر `python_execute`، يغلق الاتصال، يفتح اتصالًا جديدًا (محاكاة إعادة تشغيل)، ثم يؤكد استعادة:
     - الحالة النهائية + meta
     - جميع الأحداث بترتيب `seq` الصحيح
     - الملف الناتج ومحتواه وبصمة SHA-256 المطابقة (تنزيل عبر الاتصال الجديد)
     - تذكرة WebSocket التي أُنشئت **قبل** إعادة الاتصال: مخزّنة كتجزئة SHA-256، تُستهلك من الاتصال الجديد مرة واحدة فقط، وإعادة استهلاكها مرفوضة
   - عند الفشل يعيد رمز خروج غير صفري برسالة منقّحة (بدون traceback)، ويحذف سجلاته افتراضيًا.
4. من الواجهة، نفّذ مهمة حقيقية، ثم أعد تشغيل الخدمة من لوحة Render، ثم اقرأ `/runs/{id}` وتأكد أن الحالة `completed` والأحداث قابلة للإعادة عبر SSE.

## التشخيص السريع

| العَرَض | السبب الأرجح |
| --- | --- |
| الخدمة ترفض البدء برسالة `no valid LLM provider` | `CELA_USE_LIVE_PROVIDER=1` بدون `GEMINI_API_KEY` صالح أو بدون `CELA_PROVIDER_BASE_URL`. شغّل `python scripts/deploy_doctor.py --strict` ليكشفها قبل النشر (`live_provider_unconfigured`)، أو أعد العلم إلى `0` للمعاينة. |
| `/health` يُبلغ `scripted` مع تمكين العلم | المفتاح موجود لكنه مرفوض (انظر السجلات لأي `fallback_reason`). |
| أول طلب 500 على Postgres | لم يتم تثبيت `[distributed]` (يؤمّنه `render.yaml` تلقائيًا)، أو DSN خاطئ. |
| أسماء أحداث تسلسلية `seq` غير متصلة بعد إعادة التشغيل | لم يتم ربط `CELA_RUN_STORE_URL` فالخادم يعمل على مخزن مؤقت في الذاكرة. |
