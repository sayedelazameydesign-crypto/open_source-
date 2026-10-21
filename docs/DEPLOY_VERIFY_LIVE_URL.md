# بوابة التحقق من النشر — عنوان حقيقي بلا مزوّد محدد

**التاريخ:** 2026-10-21 · **الفرع:** `arena/bfa772ba-open-source` · **الحالة:** البوابة مُنفَّذة ومُختبَرة محليًا؛ **لم تُشغَّل بعد على عنوان عام حقيقي**.

## ١) القرار

`Deploy verification (manual)` كان أحمر لأن خطوته الأولى تحلّ عنوان الخدمة من
Render API، وحسابنا مرفوض من Render ([`NO_CARD_DEPLOY_VERIFIED.md`](NO_CARD_DEPLOY_VERIFIED.md)).
أي إصلاح داخل Render كان مستحيلًا، وأي إبقاء للبوابة كما هي يعني CI أحمر بلا مالك.

القرار: **إخراج Render من مسار التحقق بالكامل.** البوابة الآن تتحقق من *عنوان*
لا من *منصة*؛ والمزوّد اختيار تشغيلي (ClawCloud Run، Railway، Fly.io، VPS، أو
Render لمن يستطيع) لا شرط تقني.

## ٢) ما تغيّر — بالملفات

| الملف | التغيير |
|---|---|
| `.github/workflows/deploy-verify.yml` | حُذف استدعاء Render API و`RENDER_API_KEY`. العنوان من `vars.CELA_DEPLOY_URL` وقائمة السماح من `vars.CELA_DEPLOY_ALLOWED_HOSTS`. لا مدخل عنوان في `workflow_dispatch` |
| `scripts/authorize_deploy_target.py` | جديد: يرفض قبل أي طلب غياب المتغيرات، `http://`، بيانات الاعتماد، منفذًا غير 443، مسارًا، IP حرفيًا، أو مضيفًا خارج قائمة السماح؛ ويُخرج `API_URL` المُطبَّع إلى `$GITHUB_ENV` (قائمة السماح تبقى متغير المستودع نفسه) |
| `packages/os_server/os_server/deploy_probe.py` | شرطان اختياريان جديدان: `/cela/v1/whoami` (خاص مقابل عام)، و`/platform/info` (`store.durable=true`). لا يتغيّر عدد الفحوص الافتراضي (4) عند عدم طلبهما |
| `scripts/verify_deployment.py` | `--private-token-env`, `--require-private-token`, `--require-durable-store`، وقائمة سماح من `CELA_DEPLOY_ALLOWED_HOSTS` |
| `packages/os_server/os_server/automation.py` | `CELA_DEPLOY_REQUIRE_PERSISTENT_STORE=1` يفرض Postgres/`distributed://` على أي مزوّد، بدل الاعتماد على `RENDER=true` |
| `Makefile` | `make verify-deploy` يرفض العمل بلا `ALLOW_HOSTS`؛ لا نطاق مزوّد مفترض |
| `.github/workflows/postgres-persistence.yml` | جديد: C6 يدوي، كتابة مؤكَّدة، فصل إلزامي عن قاعدة الإنتاج |
| `tests/test_deploy_verify_workflow.py` | أُعيدت كتابته: يثبت غياب أي أثر لـRender، وأن العنوان/القائمة من متغيرات المستودع، وأن البوابة تفحص السطح الخاص والمخزن |
| `tests/test_authorize_deploy_target.py` · `tests/test_postgres_persistence_workflow.py` | جديد: حراسة قواعد الوجهة وقيود الكتابة في C6 |

## ٣) الإعداد لمرة واحدة في GitHub

في `Settings → Secrets and variables → Actions`:

| النوع | الاسم | مثال | ملاحظة |
|---|---|---|---|
| Variable | `CELA_DEPLOY_URL` | `https://celia.clawcloudrun.com` | HTTPS، بلا مسار/استعلام/منفذ |
| Variable | `CELA_DEPLOY_ALLOWED_HOSTS` | `*.clawcloudrun.com` | أنماط مفصولة بفواصل؛ `*.` تطابق النطاقات الفرعية لا الجذر |
| Secret | `CELA_API_TOKEN` | — | الرمز العام |
| Secret | `CELA_PRIVATE_API_TOKEN` | — | يجب أن يختلف عن العام |
| Secret | `CELA_RUN_STORE_URL` | `postgresql://…` | مطلوب: البوابة تفرض مخزنًا دائمًا |

> `CELA_RUN_STORE_URL` هنا هو **مرجع الإنتاج** أيضًا لاختبار C6؛ لا يُستخدم كهدف كتابة أبدًا.

## ٤) متغيرات الخدمة نفسها (أي مزوّد حاويات)

| المتغير | القيمة | لماذا |
|---|---|---|
| `CELA_API_TOKEN` | سرّ عشوائي طويل | سطح المنصة |
| `CELA_PRIVATE_API_TOKEN` | سرّ **مختلف** | `/cela/v1` |
| `CELA_RUN_STORE_URL` | `postgresql://…` (أو `distributed://` مع `CELA_POSTGRES_URL` و`CELA_REDIS_URL`) | بقاء التشغيلات بعد إعادة النشر |
| `CELA_USE_LIVE_PROVIDER` | `0` | المزوّد الحي مغلق بقرار حتى يكتمل adapter (المرحلة 1) |
| `CELA_CODE_EXEC_MODE` | `disabled` | تنفيذ الكود معطّل في الحاوية |
| `PORT` | يضبطه المزوّد | Dockerfile/الخادم يقرأه |

**أمثلة نطاقات لقائمة السماح** (إعداد مقترح، لا ادّعاء نشر):

| المزوّد | شكل العنوان | نمط قائمة السماح |
|---|---|---|
| ClawCloud Run | `https://<app>.<region>.clawcloudrun.com` | `*.clawcloudrun.com` |
| Railway | `https://<service>-production.up.railway.app` | `*.up.railway.app` |
| Fly.io | `https://<app>.fly.dev` | `*.fly.dev` |
| VPS + نطاقك | `https://api.<domain>` | `api.<domain>` أو `*.<domain>` |
| Render (اختياري) | `https://<service>.onrender.com` | `*.onrender.com` |

> لا تضع نطاق قاعدة البيانات في القائمة، ولا `*.supabase.co` — البوابة تفحص خدمة HTTP لا قاعدة بيانات.

## ٥) تشغيل البوابة

```bash
export CELA_API_TOKEN=...
export CELA_PRIVATE_API_TOKEN=...
python scripts/verify_deployment.py \
  --api https://<service>.<provider-domain> \
  --allow-host '*.your-provider.app' \
  --private-token-env CELA_PRIVATE_API_TOKEN --require-private-token \
  --require-durable-store \
  --expect-kind scripted
```

أو من Actions: **Deploy verification (live URL) → Run workflow**، واختر
`expect_kind` فقط. الخطوات بالترتيب:

1. `scripts/authorize_deploy_target.py` — قرار الوجهة قبل أي طلب.
2. `scripts/deploy_doctor.py --strict` مع
   `CELA_DEPLOY_REQUIRE_PERSISTENT_STORE=1` و`CELA_CODE_EXEC_MODE=disabled`
   (قيم حرفية، لا أسرار)، و`CELA_BACKEND_URL` يُمرَّر عبر الـshell لا عبر
   تعبير `env:` لأن قيم `$GITHUB_ENV` لا تدخل سياق تعابير الخطوة التالية.
3. `scripts/verify_deployment.py` — ستة فحوص مسمّاة.

## ٦) ما يثبته كل فحص

| الفحص | يثبت | لا يثبت |
|---|---|---|
| `health answers {"status":"ok"} without a token` | العملية تعمل و`/health` عام | أن التطبيق سليم وظيفيًا |
| `runtime-info requires the platform token` | السطح العام محمي | أن الرمز صالح (الفحص التالي يثبته) |
| `runtime-info reports execution.kind == …` | هوية التنفيذ مطابقة للمعلَن (`scripted` قبل المرحلة 1) | أن المزوّد الحي يعمل |
| `whoami` خاص/عام | الرمزان مختلفان وكل سطح يحمي نفسه | أن كل مسارات `/cela/v1` مُختبرة |
| `platform-info reports a durable run store` | الخدمة **أُعدّت** بمخزن دائم | أن الكتابة تنجو من إعادة اتصال (هذا C6) |
| `no response echoes the API token` | لا تسريب للرمز في أجسام الردود | عدم وجود تسريب في السجلات (خارج نطاق الفحص) |

## ٧) C6 — التحقق من Postgres الحقيقي

`.github/workflows/postgres-persistence.yml`:

* يدوي فقط (`workflow_dispatch`)، وقراءة فقط على المستودع.
* لا يشتغل إلا باختيار `confirm_write: yes` صريح.
* يحتاج `CELA_TEST_POSTGRES_DSN` (قاعدة اختبار مخصّصة) و`CELA_RUN_STORE_URL`
  (مرجع الإنتاج للمقارنة فقط). غياب أي منهما خطأ مسمّى قبل أي اتصال.
* لا يمرّر `--no-production-reference` أبدًا: تشغيل بلا دليل فصل لا يُسجَّل C6.

**سلوك الحرّاس مُتحقَّق منه محليًا بلا قاعدة بيانات** (لأن الرفض يسبق الاتصال):

```text
$ CELA_TEST_POSTGRES_DSN='postgresql://u:p@db.example.test:5432/app' \
  CELA_RUN_STORE_URL='postgresql://other:pw@db.example.test:5432/app' \
  python scripts/test_postgres_persistence.py --yes
error: refusing to write: this DSN resolves to the same database as the production
reference (db.example.test:5432/app). Use a dedicated test database.
# exit 2

$ CELA_TEST_POSTGRES_DSN='postgresql://u:p@db.example.test:5432/cela_test' \
  python scripts/test_postgres_persistence.py --yes
error: refusing to write: no production reference to compare against, so this guard
cannot prove the target is a different database. ...
# exit 2
```

> **C6 ما زال مفتوحًا:** لم يُشغَّل السكربت ضد Postgres حيّ بعد. قاعدة
> `open-source-test-db` النشطة في Supabase (منذ 2026-10-19) هي الهدف الطبيعي،
> لكن تمرير `CELA_TEST_POSTGRES_DSN` لم يحدث من هذه الجلسة.

## ٨) دليل محلي على أن البوابة تعمل فعليًا

شُغِّل التطبيق الحقيقي على حلقة محلية بمخزن SQLite ورمزين مختلفين، ثم استُدعيت
نفس البوابة بالمعايير الكاملة:

```text
Celia.sh — deployment verification
  service : http://127.0.0.1:35445
  commit  : 86cc03ad5669a184e3c9b31ed66726ee62ed7302
  expect  : execution.kind == 'scripted'
  token   : loaded from CELA_API_TOKEN (never printed)
  private : loaded from CELA_PRIVATE_API_TOKEN (never printed)

[PASS] health answers {"status":"ok"} without a token
       HTTP 200 with {"status": "ok"}
[PASS] runtime-info requires the platform token
       HTTP 401 without a token
[PASS] runtime-info reports execution.kind == "scripted"
       execution.kind=scripted; model_name=demo-react; goal_verification=not_verified
[PASS] /cela/v1/whoami accepts the private token and refuses the public one
       HTTP 401 for the public token; HTTP 200 and surface=private for the private token
[PASS] platform-info reports a durable run store
       store.durable=true; backend=SQLiteRunStore; scope=disk
[PASS] no response echoes the API token
       the API token appears in no response body

6/6 criteria passed.
```

فحص إضافي لمخرجات الأمر نفسه: لا يظهر أي من الرمزين في `stdout` أو `stderr`
(`TOKENS_LEAKED: False`).

**حدود هذا الدليل:** يثبت أن البوابة تنفّذ ستة فحوص صحيحة ضد عملية حقيقية، وأن
الرمزين لا يتسربان. **لا يثبت** نشرًا عامًا، ولا TLS/DNS فعليًا، ولا Postgres
(`scope=disk` هو SQLite محلي؛ البوابة في CI ترفض SQLite عبر شرط الـDoctor).

## ٩) أخطاء شائعة ورسائلها

| الرسالة | السبب | الإصلاح |
|---|---|---|
| `CELA_DEPLOY_URL repository variable is empty or unset.` | متغير المستودع غير مضبوط | أضفه في إعدادات Actions |
| `the deploy URL must use https://` | عنوان `http://` | استخدم HTTPS عامًا؛ `--allow-loopback` للاختبار المحلي فقط |
| `host '…' is not covered by the allowlist` | النطاق خارج الأنماط | صحّح النمط؛ `*.example.app` لا يطابق `example.app` |
| `CELA_TEST_POSTGRES_DSN secret is empty or unset` | سر C6 غائب | أضف سلسلة قاعدة الاختبار |
| `persistent_run_store_required` | `CELA_RUN_STORE_URL` ذاكرة/SQLite | اضبط Postgres |
| `store.durable is not true` | الخدمة تعمل بمخزن الذاكرة | اضبط `CELA_RUN_STORE_URL` في بيئة الخدمة وأعد النشر |

## ١٠) ما بقي خارج هذه البوابة

1. **النشر نفسه:** لا خدمة عامة بعد. البوابة جاهزة، لكنها تحتاج عنوانًا حقيقيًا.
2. **C6:** يحتاج `CELA_TEST_POSTGRES_DSN` وتشغيلًا واحدًا ناجحًا.
3. **المزوّد الحي:** `CELA_USE_LIVE_PROVIDER=1` يبقى ممنوعًا حتى يكتمل adapter
   المزوّد ومراجعة مضيفه (المرحلة 1).
4. **`render.yaml`:** يبقى Blueprint اختياريًا؛ لم يعد أي فحص يعتمد عليه.

---

## ١١) إعداد مرجعي لكل مزوّد (اختر واحدًا)

الصورة تُبنى في CI وتُنشر على GHCR عند الدفع إلى `main`، واسمها مُشتق من اسم
المستودع بعد تنظيف الشرطة الأخيرة (لأن مراجع Docker لا تنتهي بشرطة):
`ghcr.io/sayedelazameydesign-crypto/open_source`، بوسوم `:latest` و`:edge`
و`:sha-<commit>`. الصورة تفتح `0.0.0.0` وتحترم `$PORT` (الافتراضي 8080)،
و`HEALTHCHECK` فيها يضرب `/health`.

**المتغيرات الخمسة المشتركة بين كل المزوّدين** (القيم نفسها لا تتغيّر):

```text
CELA_API_TOKEN=<سلسلة عشوائية طويلة>
CELA_PRIVATE_API_TOKEN=<سلسلة مختلفة تمامًا>
CELA_RUN_STORE_URL=postgresql://...        # أو distributed:// + CELA_POSTGRES_URL + CELA_REDIS_URL
CELA_USE_LIVE_PROVIDER=0
CELA_CODE_EXEC_MODE=disabled
```

> لا تضف `GEMINI_API_KEY` الآن. وجوده مع `CELA_USE_LIVE_PROVIDER=0` لا يضر،
> لكن إضافته تُغري بتفعيل العلم قبل اكتمال adapter المرحلة 1، والخادم يرفض
> الإقلاع إذا فُعّل العلم بلا مفتاح صالح.

### ClawCloud Run (المسار المختار في `NO_CARD_DEPLOY_VERIFIED.md`)

1. أنشئ تطبيقًا من **Container Image** بالمصدر `ghcr.io/sayedelazameydesign-crypto/open_source:latest`.
   إن كانت الحزمة خاصة، أضف بيانات سحب من GitHub Packages.
2. منفذ الحاوية: `8080` (أو اترك المزوّد يقرأ `$PORT`).
3. فحص الصحة: مسار `/health`، متوقع `200`.
4. أضف المتغيرات الخمسة أعلاه.
5. انشر، ثم انسخ النطاق العام — سيكون مثل `https://<app>.<region>.clawcloudrun.com`.

### Railway

1. **New Project → Deploy from Docker Image**، والصورة نفسها.
2. أضف المتغيرات الخمسة في تبويب **Variables**.
3. **Settings → Networking → Generate Domain** للحصول على
   `https://<service>-production.up.railway.app` (قد يختلف الشكل؛ انسخه كما هو).
4. Railway يمرّر `PORT` تلقائيًا؛ لا تثبّته يدويًا إلا إذا طلب منك.

### Fly.io

```bash
fly launch --image ghcr.io/sayedelazameydesign-crypto/open_source:latest --no-deploy
fly secrets set CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... \
  CELA_RUN_STORE_URL='postgresql://...' CELA_USE_LIVE_PROVIDER=0 CELA_CODE_EXEC_MODE=disabled
fly deploy
```

Fly يفرض بطاقة على الحساب؛ إن لم تكن متاحة فهو ليس خيارًا الآن.

### VPS (Docker موجود مسبقًا)

```bash
cat > /etc/celia.env <<'ENV'
CELA_API_TOKEN=...
CELA_PRIVATE_API_TOKEN=...
CELA_RUN_STORE_URL=postgresql://...
CELA_USE_LIVE_PROVIDER=0
CELA_CODE_EXEC_MODE=disabled
ENV
chmod 600 /etc/celia.env

docker run -d --restart=unless-stopped --name celia \
  --env-file /etc/celia.env -p 127.0.0.1:8080:8080 \
  ghcr.io/sayedelazameydesign-crypto/open_source:latest
```

ثم ضع reverse proxy (Caddy/Nginx) بشهادة TLS أمام `127.0.0.1:8080`؛ البوابة
ترفض `http://` وعناوين IP، فلا تفحص الخدمة عبر منفذ مكشوف بلا شهادة.

### بعد أي مزوّد

اضبط متغيرَي المستودع ثم شغّل البوابة:

```text
CELA_DEPLOY_URL=https://<النطاق الذي نسخته>
CELA_DEPLOY_ALLOWED_HOSTS=<نمط يغطي ذلك النطاق، مثل *.clawcloudrun.com>
```

## ١٢) خطة سبعة أيام (واقعية، بلا وعود نشر ليست بيدنا)

| اليوم | العمل | معيار الإنجاز | العائق |
|---|---|---|---|
| 1 | اختيار المزوّد وإنشاء التطبيق من صورة GHCR، وضبط المتغيرات الخمسة | الحاوية تعمل و`/health` يرد `200` | يحتاج حسابًا لدى المزوّد |
| 2 | ضبط `CELA_DEPLOY_URL` و`CELA_DEPLOY_ALLOWED_HOSTS` وتشغيل **Deploy verification (live URL)** | 6/6 PASS مع `execution.kind=scripted` | يحتاج الأسرار الثلاثة في المستودع |
| 3 | إنشاء قاعدة اختبار مخصّصة (أو استخدام `open-source-test-db`) وضبط `CELA_TEST_POSTGRES_DSN` | سكربت C6 يصل ويُنشئ الجداول | يحتاج سلسلة اتصال |
| 4 | تشغيل **Postgres persistence (C6)** بـ`confirm_write: yes` | كل الفحوص ناجحة و`exit 0`، ثم تسجيل المخرج في `EVIDENCE.md` | لا كتابة إلى قاعدة الإنتاج |
| 5 | تثبيت النشر: إعادة تشغيل الخدمة والتأكد أن التشغيلات السابقة ما زالت مقروءة | `/runs` يعيد تشغيلًا قبل إعادة التشغيل | — |
| 6 | مراجعة `docs/ROADMAP.md` وتحديث C5/C6 والحالة | لا بند مفتوح بلا مالك | — |
| 7 | مراجعة المرحلة 1: كتابة نطاق adapter المزوّد وقياس التوكنز | قرار مكتوب: ما يُنفَّذ أولًا ولماذا | لا يُفعّل العلم في هذه المرحلة |

**ما لا تدّعيه هذه الخطة:** لا تاريخ لإصدار `1.0`؛ البوابتان في `ROADMAP.md`
شرطان قد لا يُستوفيا. واليوم 7 تسليم *نطاق* المرحلة 1 لا تنفيذها.
