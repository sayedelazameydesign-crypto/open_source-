# دليل النشر المجاني — Render + Supabase + Cloudflare Pages

> **الهدف:** نشر `Celia.sh — Mission OS` بتكلفة **صفر دولار وبلا بطاقة ائتمان**، مع بقاء `CELA_API_TOKEN` و`CELA_PRIVATE_API_TOKEN` على الخادم فقط.
> **المكوّنات الثلاثة** موصوفة هنا بالترتيب الذي تُنشأ به: قاعدة البيانات، ثم الخادم، ثم الواجهة.
>
> ```
> المتصفح ──► Cloudflare Pages (out/ ثابتة + Pages Function BFF)
>                 │  Authorization: Bearer <token>  (من بيئة الـFunction فقط)
>                 ▼
>            Render (FastAPI / os_server)  ──►  Supabase Postgres (تشغيلات + أرشيف + وسوم)
> ```

---

## 0) قبل أن تبدأ — ما تحتاجه

| المتطلب | لماذا |
|---|---|
| حساب GitHub + المستودع | المصدر الوحيد لبناء الخدمتين |
| حساب Supabase (Free) | تخزين دائم؛ قرص Render المجاني **مؤقت** ويُمسح عند كل نوم |
| حساب Render (Free) | تشغيل `os_server` (Python حقيقي: subprocess + SQLite WAL + ملفات) |
| حساب Cloudflare (Free) | استضافة الواجهة + وكيل الحافة |
| رمزان **مختلفان** بطول 32+ حرفًا | فصل السطح العام عن `/cela/v1` الخاص |

لتوليد رمزين:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> ⛔ **القاعدة الحاكمة:** لا يُوضع أي رمز في Git، ولا في متغير يبدأ بـ `NEXT_PUBLIC_`.
> `scripts/deploy_doctor.py` يعتبر `NEXT_PUBLIC_*TOKEN*` خطأً مانعًا (`browser_exposed_secret_var`).

---

## 1) Supabase Postgres (5 دقائق)

1. **New project** → اختر منطقة قريبة من خادم Render، واحفظ كلمة مرور قاعدة البيانات.
2. **Settings → Database → Connection string** واختر **Session pooler** (port `5432`) لا **Direct connection**:
   * الاتصال المباشر في Supabase صار **IPv6 فقط**، وRender Free لا يملك خروج IPv6 مستقرًا.
   * **Transaction pooler** (port `6543`) يعطّل prepared statements، و`psycopg` يستخدمها تلقائيًا بعد خمسة تنفيذات؛ لذلك Session pooler هو الخيار الآمن.
3. أضف `?sslmode=require` إلى نهاية الرابط إن لم يكن موجودًا.
4. القيمة النهائية تُصبح `CELA_RUN_STORE_URL`:

```
postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

> الجداول تُنشأ تلقائيًا (`run_meta`, `run_states`, `run_events`, `run_artifacts`, `ws_tickets`).
> مشاريع Supabase المجانية **تتوقف مؤقتًا بعد سبعة أيام بلا نشاط**؛ نشاط التطبيق يعيدها تلقائيًا عند أول اتصال (قد يتأخر أول طلب).

---

## 2) Render — خادم `os_server` (10 دقائق)

1. **New → Blueprint** واختر المستودع؛ سيقرأ `render.yaml` مباشرة.
2. أدخل الأسرار المطلوبة (`sync: false`):

| المتغير | القيمة |
|---|---|
| `CELA_API_TOKEN` | رمز عام (للواجهة والسطح العام) |
| `CELA_PRIVATE_API_TOKEN` | رمز **مختلف** لـ `/cela/v1` |
| `CELA_RUN_STORE_URL` | رابط Session pooler من الخطوة 1 |
| `GEMINI_API_KEY` | مطلوب **فقط** بعد تفعيل الوضع الحي (الخطوة 2.6)؛ اتركه فارغًا في المعاينة |
| `CELA_ACTIVEPIECES_*` | اختياري (أتمتة) |

3. **لماذا `pip install -e ".[server,distributed]"` في `render.yaml`؟**
   `CELA_RUN_STORE_URL` يشير إلى Postgres، وحزمة `psycopg` موجودة فقط في extra `distributed`.
   بدونها **يقلع الخادم بنجاح ويجيب `/health` بـ 200**، ثم يفشل أول تشغيل فعلي بـ 500.
   لهذا يفحص `deploy_doctor` وجود الحزمة ويُفشل النشر إن غابت (`postgres_driver_missing`).

4. **التحقق قبل النشر (محليًا، بنفس متغيرات Render):**

```bash
CELA_API_TOKEN="…" CELA_PRIVATE_API_TOKEN="…" \
CELA_RUN_STORE_URL="postgresql://…" RENDER=true \
python scripts/deploy_doctor.py --strict
```

يجب أن يعود `"ready": true` مع `"store_backend_scheme": "postgresql"` وبلا تحذير `ephemeral_sqlite_on_render`.

5. **التحقق بعد النشر:**

```bash
API=https://<service>.onrender.com
curl -sS $API/health                                   # {"status":"ok"}
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_API_TOKEN" $API/runs        # 200
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_API_TOKEN" $API/cela/v1/whoami  # 401
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" $API/cela/v1/whoami  # 200
```

### 2.6 الوضع التجريبي أولًا، ثم الوضع الحي

`render.yaml` يُنشئ الخدمة في **وضع المعاينة**: `CELA_USE_LIVE_PROVIDER=0`.
لذلك يقلع الخادم بلا أي مفتاح، ويُعلن عن نفسه بصدق: `/runtime-info` يعيد
`execution.kind = "scripted"`، وواجهة `ExecutionDisclosure` تعرض «تنفيذ تجريبي»
بدل الإيحاء بأن نموذجًا حيًّا أجاب.

1. أكمل خطوات §3–§4 أولًا وتأكد أن المهمة التجريبية تُحفظ وتُستعاد بعد إعادة تشغيل Render.
2. أضف `GEMINI_API_KEY` كسرًا في Render (Dashboard → Environment)، ثم غيّر
   `CELA_USE_LIVE_PROVIDER` إلى `1` وأعد النشر.
3. شغّل اختبار القبول A1/A6 بمفتاح حقيقي (انظر `docs/DEPLOYMENT_RUNBOOK.md` §المرحلة أ)،
   ثم تحقق من الخدمة المنشورة نفسها: `/runtime-info` يجب أن يعيد
   `provider_adapter` مع `fallback_triggered=False` في مسار النجاح.

> **فشل مغلق مقصود:** إذا بقي العلم على `1` بلا مفتاح صالح، يرفض الخادم الإقلاع،
> ويعيد `scripts/deploy_doctor.py` الخطأ `live_provider_unconfigured` **قبل** النشر.
> لا تترك العلم مفعّلًا على أمل أن يعمل لاحقًا.

> **اسم النموذج ليس مضمونًا:** القيمة الافتراضية `gemini-2.5-flash` مجرد إعداد؛
> أهلية المفتاح وحصته يقررهما المزوّد. إن رفض المزوّد النموذج، غيّر
> `CELA_PROVIDER_MODEL`، وأعد تشغيل A1/A6، وسجّل التغيير بدل افتراض النجاح.

> **نوم Render المجاني:** بعد 15 دقيقة خمول ينام الخادم ويستغرق ~50 ثانية للاستيقاظ.
> لا تعطّل هذا في الخادم: وكيل الحافة يترجمه إلى `503 backend_starting` مع `Retry-After: 5`، والواجهة تعيد المحاولة،
> وبث SSE يستأنف بـ `?after_seq=N` بدل إعادة الأحداث. إن أردت تقليل النوم، أرسل طلب `/health` كل 10 دقائق من
> خدمة ping خارجية (اختياري، ويكون ضمن حدود الخطة المجانية).

---

## 3) Cloudflare Pages — الواجهة + وكيل الحافة (10 دقائق)

### 3.1 إعداد المشروع

| الإعداد | القيمة |
|---|---|
| Framework preset | None / Next.js (Static HTML Export) |
| **Root directory** | `packages/web` |
| **Build command** | `npm run build:pages` |
| **Build output directory** | `out` |
| Functions directory | `functions` (يكتشفها Cloudflare تلقائيًا، وهي مضمّنة في المستودع) |

`npm run build:pages` يضبط `CELA_STATIC_EXPORT=1` فيُصدَّر الموقع ثابتًا في `out/`:

* الصفحات: `/`, `/status`, `/launcher`, `/documents`, `/404`.
* **لا** يُضمَّن مسار Next الخادمي `app/api/backend/[...path]/route.ts` في التصدير الثابت (التصدير لا يدعم Route Handler ديناميكيًا)؛
  بل يتولى `functions/api/backend/[[path]].ts` دور الوكيل على الحافة بنفس عقد الأمان.

### 3.2 متغيرات البيئة

Cloudflare → Pages → المشروع → **Settings → Variables and Secrets** (Production **و** Preview):

| المتغير | النوع | القيمة |
|---|---|---|
| `CELA_BACKEND_URL` | Secret | `https://<service>.onrender.com` (**https إلزامي**) |
| `CELA_API_TOKEN` | Secret | الرمز العام نفسه |
| `CELA_PRIVATE_API_TOKEN` | Secret | الرمز الخاص نفسه |
| `CELA_BACKEND_TIMEOUT_MS` | Plain (اختياري) | `25000` افتراضيًا، والحد الأقصى `60000` |

> هذه المتغيرات تُقرأ في **وقت التشغيل** داخل Pages Function. متغيرات *البناء* لا تحتاجها.
> `deploy_doctor` يحذّرك صراحةً أنه لا يرى أسرار وقت التشغيل من بيئة البناء (`cloudflare_backend_url_unset`).

### 3.3 النشر اليدوي (بديل عن ربط Git)

```bash
cd packages/web
npm ci
npm run build:pages
npx wrangler pages deploy out --project-name celia-mission-os
```

### 3.4 التحقق بعد النشر

```bash
SITE=https://<project>.pages.dev
curl -sS $SITE/api/backend/health                 # {"status":"ok"}
curl -sS -o /dev/null -w "%{http_code}\n" $SITE/api/backend/cela/v1/whoami   # 200 (الرمز الخاص يُحقن على الحافة)
curl -sS -o /dev/null -w "%{http_code}\n" $SITE/status
grep -c "CELA_API_TOKEN\|local-dev-token" <(curl -sS $SITE/) || true        # 0 — لا رمز في HTML
```

---

## 4) الفحص النهائي قبل الإعلان

| # | الفحص | المتوقع |
|---|---|---|
| 1 | `python scripts/deploy_doctor.py --strict` ببيئة Render | `ready: true` بلا أخطاء |
| 2 | `CF_PAGES=1 CELA_BACKEND_URL=https://… python scripts/deploy_doctor.py` | `ready: true` و`cloudflare_bff_proxy: true` |
| 3 | `GET /health` على Render | `{"status":"ok"}` |
| 4 | `POST /api/backend/runs` من موقع Pages | `202` مع `run_id` |
| 5 | `GET /api/backend/runs/{id}/events?after_seq=0` | `text/event-stream` وأحداث متتابعة |
| 6 | إعادة تشغيل خدمة Render ثم فتح الواجهة | تعرض «الخادم يستيقظ» وتعيد المحاولة بدل خطأ خام |
| 7 | تشغيل ثم إعادة تشغيل Render | التشغيل السابق يبقى في `/ledger` (Postgres لا القرص المؤقت) |
| 8 | `CELA_TEST_POSTGRES_DSN=<قاعدة اختبار منفصلة> CELA_RUN_STORE_URL=<قاعدة التطبيق> python scripts/test_postgres_persistence.py --yes` | PASS، ثم حذف صفوف الاختبار (C6). مرجع الإنتاج للمقارنة فقط (أو `--production-dsn`)، وغيابه يوقف السكربت. مشروع Supabase ثانٍ مقبول حتى لو شارك مضيف الـpooler |

---

## 5) استكشاف الأعطال

| العرض | السبب المرجّح | الحل |
|---|---|---|
| `503 backend_starting` | Render نائم (Free) | انتظر 5–60 ثانية؛ الواجهة تعيد المحاولة تلقائيًا |
| `502 backend_unreachable` | `CELA_BACKEND_URL` خاطئ أو الخدمة متوقفة | تحقّق من الرابط و`/health` |
| `503 token_not_configured` | سر ناقص في بيئة Pages | أضف `CELA_API_TOKEN` (أو الخاص) في Settings |
| `503 backend_not_configured` مع «must use https» | الرابط `http://` | استخدم `https://` (يُسمح بـ `http://127.0.0.1` للتطوير فقط) |
| `503 backend_not_configured` مع «must not embed credentials» | `https://user:pass@host` | انقل الاعتماد إلى Secrets |
| `413 payload_too_large` | جسم الطلب > 1 MiB | الوكيل يقبل JSON فقط؛ استخدم مسار رفع مخصصًا |
| أول تشغيل `500` مع «psycopg is not installed» في السجل | بناء Render بلا extra `distributed` | أعد النشر بـ `pip install -e ".[server,distributed]"` |
| الخدمة ترفض الإقلاع: `no valid LLM provider` | `CELA_USE_LIVE_PROVIDER=1` بلا مفتاح/عنوان صالح | إمّا أضف `GEMINI_API_KEY` و`CELA_PROVIDER_*`، أو أعد العلم إلى `0` للمعاينة |
| ضياع التشغيلات بعد نوم Render | `CELA_RUN_STORE_URL` على sqlite/ذاكرة | استخدم رابط Supabase (Session pooler) |
| `401 invalid or missing private API token` على `/cela/v1/*` | استخدام الرمز العام | استخدم `CELA_PRIVATE_API_TOKEN` |
| الصفحة تعمل و`/api/backend/*` يعطي 404 على Pages | مجلد الإخراج أو دالة الحافة غير منشورة | تأكد من `out` ووجود `functions/` في جذر المشروع |
| صفحة بيضاء بعد النشر | `CELA_STATIC_EXPORT` غير مفعّل فبُنيت نسخة Node | استخدم `npm run build:pages` |

---

## 6) الثوابت الأمنية (لا تُخالف عند أي تعديل)

1. **الرموز على الخادم فقط.** لا `NEXT_PUBLIC_*` يحمل اسم سر؛ لا رمز في HTML أو JS أو `localStorage`.
2. **`/api/backend/cela/*` بالرمز الخاص فقط**، وبقية المسارات بالعام — في وكيلي Next والحافة معًا.
3. **لا `http://` خارج loopback** — الرموز لا تعبر الشبكة مكشوفة (يفرضه الوكيل والطبيب معًا).
4. **لا إعادة توجيه خارج أصل الخادم** — منعًا لإعادة إرفاق `Authorization` بمضيف آخر.
5. **`?after_seq=N` يمر حرفيًا** — إعادة الاتصال تستأنف ولا تُعيد الأحداث.
6. **`Cache-Control: no-store, no-transform`** على كل استجابة، و`X-Accel-Buffering: no` لبث SSE.
7. **لا أسرار في السجلات**: أي خطأ يعيد رمزًا (`code`) ورسالة عامة؛ الأخطاء تُسجَّل بالاسم فقط.

---

## 7) ملفات ذات صلة

| الملف | الدور |
|---|---|
| `render.yaml` | مخطط Render (خطة مجانية، فحص صحة `/health`، أسرار `sync: false`) |
| `packages/web/functions/api/backend/[[path]].ts` | وكيل الحافة (BFF) |
| `packages/web/next.config.ts` | وضع التصدير الثابت عبر `CELA_STATIC_EXPORT=1` |
| `scripts/deploy_doctor.py` | فحص الجاهزية قبل النشر |
| `tests/test_deploy_config.py` + `npm run test:deploy` | حراسة العقد الأمني والكونفيغوراسيون |
| `docs/AUTOMATION_AND_DEPLOYMENT_PLAN.md` | الخطة الكاملة ومصفوفة الامتثال |
