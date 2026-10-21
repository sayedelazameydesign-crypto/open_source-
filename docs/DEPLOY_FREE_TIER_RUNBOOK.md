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
CELA_RUN_STORE_URL="postgresql://…" RENDER=true CELA_CODE_EXEC_MODE=disabled \
python scripts/deploy_doctor.py --strict
```

يجب أن يعود `"ready": true` مع `"store_backend_scheme": "postgresql"`. غياب `CELA_RUN_STORE_URL` أو ضبطه على SQLite/ذاكرة خطأ مانع (`persistent_run_store_required`)، لا تحذيرًا؛ Postgres لازم لبقاء التشغيلات بعد restart على Render Free. يفحص `deploy_doctor` متغير البيئة و`render.yaml` الهدف معًا (يحتاج PyYAML من extra `dev`؛ `make install` يثبّته). يفشل هذا الفحص مغلقًا ما لم يكن `CELA_CODE_EXEC_MODE=disabled` صريحًا في كليهما. **لكنه preflight يدوي، وليس بعدُ بوابة يفرضها orchestrator مركزي على كل deploy**؛ المستودع لا يحتوي حاليًا deploy adapter يستدعيه لكل provider. لذلك لا تنفّذ نشرًا يدويًا من لوحة Render أو عبر أي provider حتى تُربط هذه البوابة مركزيًا؛ لا تتجاوز الرفض ولا تختَر provider بديلًا.

5. **التحقق بعد النشر:**

```bash
API=https://<service>.onrender.com
curl -sS $API/health                                   # {"status":"ok"}
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_API_TOKEN" $API/runs        # 200
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_API_TOKEN" $API/cela/v1/whoami  # 401
curl -sS -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" $API/cela/v1/whoami  # 200
```

ولفحص **هوية التنفيذ** — أن `/health` يردّ `{"status":"ok"}` وأن `/runtime-info`
يردّ `execution.kind` المتوقَّع — استعمل السكربت المعمول بدل الـ`curl` اليدوي:

```bash
export CELA_API_TOKEN=...            # الرمز العام؛ لا يُطبع أبدًا
make verify-deploy API="$API" EXPECT_KIND=scripted
# Celia.sh — deployment verification
#   service : https://<service>.onrender.com
#   commit  : <40-char sha>
#   expect  : execution.kind == 'scripted'
#   token   : loaded from CELA_API_TOKEN (never printed)
# [PASS] health answers {"status":"ok"} without a token
# [PASS] runtime-info requires the platform token
# [PASS] runtime-info reports execution.kind == "scripted"
# [PASS] no response echoes the API token
# 4/4 criteria passed.
```

الـprobe **لا يخمّن**: فحوصه الأربعة مسمّاة، ورمز الخروج `0` يعني أن كل الفحوص
نجحت، و`1` يعني أن فشل واحدًا على الأقل (فيُطبع أيّها بالتفصيل)، و`2` يعني أن
الفحص لم يعمل أصلًا (لا رمز، أو عنوان غير صالح). يستخدم HTTPS/443 فقط، ويطابق
المضيف مع `*.onrender.com` قبل DNS أو إرسال أي طلب، ويثبّت كل عناوين DNS العامة،
ولا يتبع redirects ولا يثق بمتغيرات proxy. يبدأ بـ`/health` بلا رمز؛ ولا يفحص
Postgres عبر HTTP. الرمز لا يُطبع ولا يُخزَّن في التقرير. لمزوّد API آخر، حدّد
`ALLOW_HOSTS` صراحةً بعد مراجعة adapter؛ لا تستخدم نطاقات قاعدة البيانات كخدمة HTTP.
أضف `--json` عند استدعاء `scripts/verify_deployment.py` مباشرةً لآلة تقرأ التقرير.

### 2.6 الوضع التجريبي أولًا، ثم الوضع الحي

`render.yaml` يُنشئ الخدمة في **وضع المعاينة**: `CELA_USE_LIVE_PROVIDER=0`،
ويفرض `CELA_CODE_EXEC_MODE=disabled` في إعداد الخدمة نفسه؛ لا يُعتمد على
`CELA_SANDBOXED=1` كحدّ للشبكة أو على Dockerfile لأن Blueprint يستخدم Python runtime.
لذلك يقلع الخادم بلا أي مفتاح، ويُعلن عن نفسه بصدق: `/runtime-info` يعيد
`execution.kind = "scripted"`، وواجهة `ExecutionDisclosure` تعرض «تنفيذ تجريبي»
بدل الإيحاء بأن نموذجًا حيًّا أجاب.

1. أكمل خطوات §3–§4 أولًا وتأكد أن المهمة التجريبية تُحفظ وتُستعاد بعد إعادة تشغيل Render.
2. **لا تفعّل الوضع الحي في هذه المرحلة.** عميل `OpenAICompatModel` الحالي لا يستخدم بعد
   `PinnedHTTPTransport` ولا سياسة مضيف خاصة بالمزوّد؛ يجب أولًا إكمال adapter المزوّد
   ومراجعة مضيف Gemini رسميًا واختبار DNS/TLS/الـredirects. أبقِ
   `CELA_USE_LIVE_PROVIDER=0` حتى إغلاق هذا العمل؛ وجود المفتاح وحده ليس إذنًا لإرسال
   prompts أو الأسرار إلى المضيف.
3. بعد إغلاق adapter فقط، أضف `GEMINI_API_KEY` كسرّ في Render (Dashboard → Environment)،
   ثم غيّر `CELA_USE_LIVE_PROVIDER` إلى `1` وأعد النشر.
4. شغّل اختبار القبول A1/A6 بمفتاح حقيقي (انظر `docs/DEPLOYMENT_RUNBOOK.md` §المرحلة أ)،
   ثم تحقق من الخدمة المنشورة نفسها: `/runtime-info` يجب أن يعيد
   `provider_adapter` مع `fallback_triggered=False` في مسار النجاح. أي:

   ```bash
   make verify-deploy API="$API" EXPECT_KIND=provider_adapter
   ```

   الفحص الافتراضي يتوقّع `scripted` (وضع المعاينة)، فإن استُدعي على خدمة حيّة
   دون تصحيح التوقّع أظهر ذلك صراحةً بدل أن يمرّ صامتًا.

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

### 2.7 التحقق بعد النشر من GitHub Actions (workflow يدوي)

الفحصان أعلاه يدويّان، أي أن الرمز يمرّ في طرفية المشغّل. لقضائهما بدون أي
`export` محلي يوجد `.github/workflows/deploy-verify.yml` — **يدوي فقط**
(`workflow_dispatch`) و`permissions: contents: read`، فلا يغيّر شيئًا في المستودع:

1. يحلّ عنوان الخدمة من Render API بالاسم `celia-mission-os-api` باستخدام
   `RENDER_API_KEY`. العنوان **ليس مدخلًا**: لا يستطيع من يضغط *Run workflow* أن
   يوجّه `CELA_API_TOKEN` إلى مضيف يختاره هو؛ هذه البنية عليها حراسة في
   `tests/test_deploy_verify_workflow.py` كما في `eval-real-model.yml`.
2. `python scripts/deploy_doctor.py --strict` على بيئة الخطوة بالأسرار المُمرَّرة.
3. `make verify-deploy API=<العنوان المحلول> EXPECT_KIND=<التوقّع>`، أي نفس
   الفحوص الأربعة المذكورة في §2 بند 5.

الأسرار المطلوبة في المستودع: `RENDER_API_KEY` و`CELA_API_TOKEN` و
`CELA_PRIVATE_API_TOKEN` و`CELA_RUN_STORE_URL`. غياب أي منها يُطبع كخطأ مسمّى
(`::error title=Preflight::… secret is empty or unset.`) قبل أي طلب فعلي، فلا
يُقرأ الفشل الخلطي على أنه خلل في الخدمة.

خطوتان تُفشلان التشغيل لسبب محلي بالـrunner، وتُقرأان خطأً على أنهما فشل نشر —
فلا تُصلحان بتخفيف الفحص:

* **لا تحذف `distributed` من خطوة التثبيت.** `CELA_RUN_STORE_URL` يشير إلى
  Postgres، و`deploy_doctor` يرفض مخزن Postgres بلا `psycopg`
  (`postgres_driver_missing`) لأن الخادم يتوقف عند أول تشغيل لا عند الإقلاع.
  باقتصار التثبيت على `.[dev,server]` تعود `ready: false` فيتوقف الـworkflow قبل
  أن يلمس الخدمة أصلًا؛ لذلك يثبّت `.[dev,server,distributed]` مطابقةً لبناء
  الخدمة في `render.yaml` (`.[server,distributed]`).
* **`expect_kind` خيار مقفل** (`scripted` / `provider_adapter` / `any`) بقيمة
  افتراضية `scripted`: وضع المعاينة. اختيار `provider_adapter` دليل صادق فقط بعد
  إكمال §2.6 فعليًا؛ والقيمة تصل إلى الـshell عبر متغير بيئة وتُعاد مقابلتها قبل
  الاستخدام، لا عبر استخراج داخل سطر الأمر.

حدّان يُقرآن قبل تفسير النتيجة: خطوة `Doctor (strict)` تفحص **بيئة الـrunner** بما
يُمرَّر إليها من أسرار، لا بيئة الخدمة على Render نفسها — الفحص الذي يلمس الخدمة
هو خطوة `Verify deployment`. و`workflow_dispatch` لا يظهر في الواجهة قبل وجود
الملف على الفرع الافتراضي (`main`)؛ وبعده يمكن تشغيله من أي فرع يحتوي الملف،
ومن أي فرع يُشغَّل منه تُقرأ نسخة الـworkflow نفسها.

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
| `CELA_WEB_ACCESS_SECRET` | Secret (موصى به بشدة) | سر وصول المشغّل: يُشترط إثبات صالح على كل `/api/backend/*` — كوكي `HttpOnly` موقّعة ومنتهية الصلاحية عبر `POST /api/access` (لا تحمل السر نفسه) أو السر نفسه في ترويسة `X-Celia-Access` للأتمتة. |
| `CELA_WEB_GATE_REQUIRED` | Plain (موصى به) | ضعه `1` صراحةً حتى يكون اشتراط السر موثّقًا في إعدادات النشر. البيئة الإنتاجية أو غير المعروفة تفشل **مغلقة** أيضًا إذا نُسي `CELA_WEB_ACCESS_SECRET` بدل فتح الوكيل بصمت. (`0` = فتح مقصود صريح.) |

> هذه المتغيرات تُقرأ في **وقت التشغيل** داخل Pages Function. متغيرات *البناء* لا تحتاجها.
> `deploy_doctor` يحذّرك صراحةً أنه لا يرى أسرار وقت التشغيل من بيئة البناء (`cloudflare_backend_url_unset`).
> بوابة الوصول (`CELA_WEB_ACCESS_SECRET`) هي التوصية المطبقة لمراجعة المصادقة (Finding 1):
> الوكيل يصادق الخادم على الخلفية، والبوابة تضيف طبقة إثبات وصول بسيطة للمشغّل الواحد أمام
> الإنترنت العام — لكنها ليست هوية أو صلاحيات لكل مستخدم، وتوكنات Backend تبقى مطلوبة عند
> الاتصال المباشر بخدمة Render. في Next.js (مثل Vercel) يفشل الوكيل مغلقًا تلقائيًا عند
> `NODE_ENV=production` إذا غاب السر (`503 access_gate_unconfigured`) ما لم يُضبط
> `CELA_WEB_GATE_REQUIRED=0` عمدًا. التدوير: تغيير السر يُبطل كل الإثباتات السارية فورًا.

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

### 3.5 Vercel — Next.js (بديل لـ Cloudflare Pages)

> **مهم:** Next.js على Vercel يعمل بـ `NODE_ENV=production` دائمًا، فغياب
> `CELA_WEB_ACCESS_SECRET` يعني **فشلاً مغلقاً**: كل `/api/backend/*` يعيد
> `503 access_gate_unconfigured` والواجهة المنشورة لا تصل إلى الـbackend إطلاقاً.
> آمن لكنه مكسور — ولا شيء في المستودع كان ينبّهك قبل هذا القسم.

#### 3.5.1 إعداد المشروع

| الإعداد | القيمة |
|---|---|
| **Framework preset** | Next.js |
| **Root directory** | `packages/web` |
| **Build command** | `npm run build` (افتراضي Vercel) |
| **Output** | `.next` (افتراضي) — لا `out/` |

لا يُستخدم `npm run build:pages` هنا؛ Vercel يشغّل خادم Next.js كاملاً مع
`app/api/backend/[...path]/route.ts` كوكيل خادمي. عقد الأمان مطابق لـ
Pages Function (نفس `cela/` الخاص، نفس حقن التوكن، نفس `after_seq`).

#### 3.5.2 متغيرات البيئة

Vercel → المشروع → **Settings → Environment Variables** (Production **و** Preview):

| المتغير | النوع | القيمة |
|---|---|---|
| `CELA_BACKEND_URL` | Secret | `https://<service>.onrender.com` (**https إلزامي** — الوكيل يرفض `http://` خارج loopback) |
| `CELA_API_TOKEN` | Secret | الرمز العام نفسه |
| `CELA_PRIVATE_API_TOKEN` | Secret | الرمز الخاص نفسه |
| `CELA_BACKEND_TIMEOUT_MS` | Plain (اختياري) | `25000` افتراضيًا، والحد الأقصى `60000` |
| `CELA_WEB_ACCESS_SECRET` | Secret | **إلزامي على Vercel** — سر وصول المشغّل. غيابه = `503` على كل وكيل (انظر أعلاه). |
| `CELA_WEB_GATE_REQUIRED` | Plain | `1` صراحةً لتوثيق اشتراط البوابة. (`0` = فتح مقصود فقط.) |

> **مهم — التدقيق 2026-10-19:** المشروع مربوط بـ GitHub، لذا دفع `main` ينشر تلقائياً. غير أن متغيرات Vercel لا تُطبّق بأثر رجعي — أي تعديل في **Dashboard → Environment Variables** (أو عبر `--env`) يتطلب **نشرًا جديدًا**. التدقيق وجد `CELA_WEB_ACCESS_SECRET` مسجلاً في **Production** فقط وغائبًا في **Preview**؛ النتيجة: `Preview` الحالي (commit `9e7c207` بحالة `READY`) لا يملك سر التشغيل والبوابة تفشل مغلقة. أضف السر في **Preview وProduction معاً** ثم أعد النشر للتحقق (`GET /api/access`).

> **حاجز قبل أي اختبار حي — التدقيق 2026-10-19 09:09Z:** روابط النشر الحالية
> (Preview لـ`d52cfb7` ورابط `main`) تُعيد **صفحة دخول Vercel** (`Protected
> Deployment – Vercel` → `vercel.com/login?next=%2Fsso-api…`) لأي طلب بلا جلسة،
> أي أن **حماية النشر من Vercel (Vercel Authentication)** مفعّلة على المشروع
> وتقطع الطلب **قبل** وصوله إلى Next.js. النتيجة: `GET /api/access` لا يُرى
> إطلاقاً، والاختبارات الأربعة في §3.5.4 مستحيلة من الإنترنت العام حتى تُضبط
> الحماية. المسار: Vercel → `open-source` → **Settings → Deployment Protection**،
> فإمّا إيقاف **Vercel Authentication** (بوابة `CELA_WEB_ACCESS_SECRET` هي الحامية
> المقصودة للواجهة) أو إبقاء الحماية وقبول أن كل اختبار حي يحتاج جلسة Vercel
> (يدوياً في المتصفح، أو `curl` مع ترويسة `x-vercel-protection-bypass` بعد توليد
> Protection Bypass secret). التفاصيل والأوامر في
> `docs/verification/vercel-state-2026-10-19.md`.

##### مسار النشر الفعلي: موصّل Vercel مع GitHub — وCLI/Actions بديل موثَّق

**تصحيح (تدقيق 2026-10-19):** النشر عندك **عبر موصّل Vercel مع GitHub**، لا عبر
Actions. الدليل بالأمر لا بالذاكرة:

```text
$ ls .github/workflows
ci.yml  codeql.yml  eval-real-model.yml  launcher-ci.yml  web-ci.yml   # لا workflow ينفّذ vercel deploy
$ gh api repos/…/commits/9adbdb72…/status
{"state":"success","statuses":[{"context":"Vercel","state":"success",
 "target_url":"https://vercel.com/celia-fashions-projects/open-source/A3cEbh3XkmZsEFNSFeqCF8NArRys"}]}
```

أي أن متغيرات وقت التشغيل مصدرها **Dashboard → Settings → Environment Variables**
(Production **و** Preview)، وكل تعديل فيها يتطلب **نشرًا جديدًا** (انظر التنبيه
في §3.5.2). الخطوات أدناه تنطبق فقط إن اخترت النشر يدوياً عبر CLI أو أضفت
workflow لاحقاً؛ وفي الحالتين لا تضع السر في نصّ الـworkflow ولا في `NEXT_PUBLIC_*`:

1. أضف الأسرار في **GitHub → Settings → Secrets and variables → Actions**:
   `CELA_BACKEND_URL`, `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`,
   `CELA_WEB_ACCESS_SECRET`, `CELA_WEB_GATE_REQUIRED=1`, `VERCEL_TOKEN`,
   `VERCEL_PROJECT_ID`, `VERCEL_ORG_ID` (أو `VERCEL_TEAM_ID`).
2. مرّرها كـ **runtime env** عند النشر (الأسرار تبقى في بيئة Actions، لا تُكتب في YAML).
   **تنبيه:** `--build-env` يوفّر المتغير لمرحلة البناء فقط، بينما مسارات
   Next.js تقرأ `process.env.CELA_WEB_ACCESS_SECRET` **وقت التشغيل** عند كل طلب.
   لذلك استخدم `--env` (وليس `--build-env`) لأسرار الخادم، وإلا يبقى النشر بلا سر وقت التشغيل ويفشل مغلقاً بـ `503`:

```yaml
# داخل workflow — لا تكتب قيمة سر في النص
- run: npx vercel deploy --prod --env CELA_BACKEND_URL=$CELA_BACKEND_URL --env CELA_API_TOKEN=$CELA_API_TOKEN --env CELA_PRIVATE_API_TOKEN=$CELA_PRIVATE_API_TOKEN --env CELA_WEB_ACCESS_SECRET=$CELA_WEB_ACCESS_SECRET --env CELA_WEB_GATE_REQUIRED=$CELA_WEB_GATE_REQUIRED
  env:
    CELA_BACKEND_URL: ${{ secrets.CELA_BACKEND_URL }}
    CELA_API_TOKEN: ${{ secrets.CELA_API_TOKEN }}
    CELA_PRIVATE_API_TOKEN: ${{ secrets.CELA_PRIVATE_API_TOKEN }}
    CELA_WEB_ACCESS_SECRET: ${{ secrets.CELA_WEB_ACCESS_SECRET }}
    CELA_WEB_GATE_REQUIRED: "1"
    VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
    VERCEL_PROJECT_ID: ${{ secrets.VERCEL_PROJECT_ID }}
    VERCEL_ORG_ID: ${{ secrets.VERCEL_ORG_ID }}
```

> لا تُمكّن workflow لمزامنة المفاتيح إلى Vercel إن كان موجوداً — اكتبه
> يدوياً كـ `--env` (وقت التشغيل) فقط، واجعل `CELA_WEB_ACCESS_SECRET` سرّ Actions
> يُمرَّر إلى `vercel deploy --env` (لا في `NEXT_PUBLIC_*` ولا في سجلّ الـbuild).
> المرجع: `vercel.com/docs/deployments/environments#environment-variables` و `vercel.com/docs/cli` (`--env` للتشغيل، `--build-env` للبناء فقط).

#### 3.5.3 الفحص قبل النشر (يحاكي بيئة بناء Vercel)

```bash
VERCEL=1 CELA_BACKEND_URL=https://<service>.onrender.com \
  CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... \
  CELA_WEB_ACCESS_SECRET=... CELA_WEB_GATE_REQUIRED=1 \
  python scripts/deploy_doctor.py --strict
# المتوقع: "ready": true + "next_bff_proxy": true + بلا تحذير vercel_*
# بلا سر → تحذير vercel_web_access_secret_unset (خطأ تحت --strict) + vercel_gate_required_unset
# http:// → خطأ insecure_backend_url (الوكيل يرفضه فعلاً)
```

#### 3.5.4 التحقق بعد النشر

```bash
SITE=https://<project>.vercel.app

# 0) اختبر أن الطلب يصل إلى التطبيق أصلاً ولا تصطاده حماية نشر Vercel.
#    إن ظهر `vercel.com/login?next=%2Fsso-api` أو عنوان `Protected Deployment`
#    فالنتيجة ليست من تطبيقك — عالج §3.5.2 (Deployment Protection) أولاً.
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $SITE/api/access
#    المتوقع 200 وبلا redirect. أي 30x إلى vercel.com/login = حماية Vercel.

curl -sS $SITE/api/access                          # {"mode":"enforced","required":true,"authorized":false}
curl -sS -X POST -H "Content-Type: application/json" -d '{"secret":"..."}' $SITE/api/access  # يضع الكوكي
curl -sS $SITE/api/backend/health                 # {"status":"ok"} عبر الكوكي أو X-Celia-Access
curl -sS -H "X-Celia-Access: $CELA_WEB_ACCESS_SECRET" $SITE/api/backend/cela/v1/whoami  # 200 (الرمز الخاص يُحقن على الحافة)
grep -c "CELA_API_TOKEN\|local-dev-token" <(curl -sS $SITE/) || true  # 0 — لا رمز في HTML
```

> تدوير السر يُبطل كل الكوكيز فوراً.

---

## 4) الفحص النهائي قبل الإعلان

| # | الفحص | المتوقع |
|---|---|---|
| 1 | `python scripts/deploy_doctor.py --strict` ببيئة Render | `ready: true` بلا أخطاء |
| 2 | `CF_PAGES=1 CELA_BACKEND_URL=https://… python scripts/deploy_doctor.py` | `ready: true` و`cloudflare_bff_proxy: true` |
| 2b | `VERCEL=1 CELA_BACKEND_URL=https://… CELA_WEB_ACCESS_SECRET=… CELA_WEB_GATE_REQUIRED=1 python scripts/deploy_doctor.py --strict` | `ready: true` و`next_bff_proxy: true` وبلا تحذير `vercel_*` |
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
| `503 access_gate_unconfigured` على كل `/api/backend/*` على Vercel | `CELA_WEB_ACCESS_SECRET` غائب و`NODE_ENV=production` يفشل مغلقاً | أضف `CELA_WEB_ACCESS_SECRET` + `CELA_WEB_GATE_REQUIRED=1` في Vercel (Production وPreview) **كمتغيرات تشغيل (Dashboard أو `vercel deploy --env`)** — لا `--build-env` — ثم أعد النشر |
| `401 access_required` بلا كوكي على Vercel | البوابة `enforced` بلا إثبات | افتح `/api/access` وأدخل السر، أو استخدم `X-Celia-Access` للأتمتة |
| `/api/*` يعيد صفحة `Protected Deployment – Vercel` / `302` إلى `vercel.com/login?next=%2Fsso-api` | **Vercel Authentication** (Deployment Protection) مفعّلة فتقطع الطلب قبل Next.js — ليست `503 access_gate_unconfigured` ولا `401 access_required` | Settings → Deployment Protection: أوقفها (بوابة السر هي الحماية المقصودة)، أو ولّد Protection Bypass secret ومرّر `x-vercel-protection-bypass` في `curl` |

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
| `packages/web/functions/api/backend/[[path]].ts` | وكيل الحافة (BFF) — Cloudflare Pages |
| `packages/web/app/api/backend/[...path]/route.ts` | وكيل الخادم (BFF) — Next.js / Vercel |
| `packages/web/next.config.ts` | وضع التصدير الثابت عبر `CELA_STATIC_EXPORT=1` |
| `scripts/deploy_doctor.py` | فحص الجاهزية قبل النشر |
| `.github/workflows/deploy-verify.yml` | الفحص بعد النشر يدويًا من Actions (§2.7) — يحلّ العنوان من Render API ثم `deploy_doctor` + `verify_deployment` |
| `tests/test_deploy_config.py` + `npm run test:deploy` | حراسة العقد الأمني والكونفيغوراسيون |
| `docs/AUTOMATION_AND_DEPLOYMENT_PLAN.md` | الخطة الكاملة ومصفوفة الامتثال |
