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

ثم اختبر تشغيل الخادم محليًا **بمفتاح حقيقي** (خطوة يدوية سريعة):

```bash
export CELA_RUN_STORE_URL="sqlite:///./cela_runs.db"
python -m os_server.cli serve --host 127.0.0.1 --port 8080
# في طرفية ثانية:
curl -s -H "Authorization: Bearer $CELA_API_TOKEN" http://127.0.0.1:8080/mission/status
# تأكد من أن payload يحتوي على execution.kind == "provider_adapter"
```

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
   python scripts/test_postgres_persistence.py --yes
   ```
   - السكربت لا يقرأ `CELA_RUN_STORE_URL` إطلاقًا، ويرفض أي DSN يطابق نفس المضيف/المنفذ/القاعدة، ويطلب `--yes` قبل أي كتابة.
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
