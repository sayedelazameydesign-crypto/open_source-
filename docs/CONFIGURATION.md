# مرجع الإعداد — كل متغيّرات البيئة

هذا المستند هو **المرجع الكامل** لمتغيّرات البيئة التي تقرأها الشيفرة. `.env.example`
يبقى **قالبًا** لما يحتاجه المشغّل عادةً (المزوّدون، الرموز، النشر)؛ أمّا هنا فكل متغيّر
مستخدم في `packages/os_*` و`packages/web` و`scripts/`، مع افتراضيه وأين يُقرأ.

> **قاعدة الفشل المغلق:** كل ما يوسّع الصلاحيات (`*_ENABLE=1`، `ALLOW_*`، `USE_LIVE_PROVIDER=1`)
> معطّل افتراضيًا. غياب المتغيّر يعني **الأضيق**، لا الأوسع. المتغيّرات السرّية لا تحمل قيمة
> افتراضية أبدًا، ووجودها في البيئة يعني أنها **مُعيّنة** لا أنها **صالحة** (الفحص وحده يقول ذلك).

القسم `[*]` يعني: موجود في `.env.example`. والباقي لا يُنشَر في القالب عمدًا (إمّا داخلي،
أو لا يُضبط إلا في صورة/مهمة CI معيّنة).

---

## ١) الهوية والمصادقة والخدمة

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_API_TOKEN` `[*]` | — (إلزامي للتشغيل) | رمز سطح المنصة `/runs`, `/ws`, `/platform/info`, `/ledger`. غيابه ⇒ كل نقطة محمية تُعيد `503` (فشل مغلق)، لا `200`. |
| `OS_API_TOKEN` | — | الاسم القديم، يُقرأ للتوافق الخلفي فقط. |
| `CELA_PRIVATE_API_TOKEN` `[*]` | — | رمز `/cela/v1/*` المستقل. **يُرفض التشغيل** إن ساوى رمز المنصة (`private_api_token must differ`). |
| `CELA_CODE_EXEC_MODE` | `local` | `local` \| `production` \| `disabled`. الصور و`render.yaml` تثبّته على `disabled`: أي طلب تنفيذ يُرفض بـ`DisabledBackend` بدل تشغيل كود غير موثوق على مضيف مشترك. |
| `CELA_CODE_EXEC_BACKEND` | — | مع `MODE=production`: `e2b` \| `firecracker`. كلاهما **فشل مغلق**: SDK/نقطة نهاية غائبة أو خطأ مزوّد ⇒ `DENIED`، ولا ارتداد إلى العزل المحلي. |
| `CELA_FIRECRACKER_ENDPOINT` | — | عنوان مدير الـmicroVM لخلفية `firecracker`. |
| `CELA_FIRECRACKER_API_TOKEN` | — | **اسم** المتغيّر الحامل للرمز (لا الرمز نفسه) لخلفية `firecracker`. |
| `CELA_SANDBOXED` | — | يضبطه المشغّل داخل الطفل (`1`)؛ محوّلات Reach ترفض العمل إن رأته، فلا تستعير قدرة المضيف. |
| `CELA_PORT` | `8080` | المنفذ المعلن في `/platform/info`؛ `PORT` يتقدّم عليه إن وُجد (منصّات تنشر `PORT`). |
| `CELA_APP_NAME` | `celia-mission-os` | اسم التطبيق المعروض في `/platform/info` (حتى 64 محرفًا). |
| `CELA_REGION` | — | منطقة النشر المعروضة (حتى 64 محرفًا). |
| `CELA_PUBLIC_URL` | — | العنوان العام؛ وجوده فقط يُبلَّغ عنه كـ`public_url_configured`. لا يُستخدم في أي توجيه. |
| `CELA_PUBLIC_DOCS` | `1` | `0` يحذف `/docs` و`/redoc` و`/openapi.json` (تُعيد 404). الافتراضي **مُبقى مفعّلًا** لأن دليل النشر يستخدم `/docs ⇒ 200` كإشارة فحص بعد النشر؛ أطفئه على نشر عام لتوقّف تسليم خريطة الـAPI كاملة لمجهول. |
| `CELA_API_URL` | — | عنوان الخدمة المنشورة لـ`scripts/verify_deployment.py` و`os_server.deploy_probe` (بديل `--api`؛ `CELA_BACKEND_URL` يليه). |
| `CELA_LEDGER_PATH` `[*]` | `./.cela/ledger.jsonl` | مسار سجل النتائج (JSONL). `off`/`none`/`""` يعطّل الكتابة صراحةً. |

## ٢) مخازن الحالة والاستمرارية

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_DEPLOY_REQUIRE_PERSISTENT_STORE` | `0` | يجعل بوابة النشر تشترط مخزنًا دائمًا (`postgresql://`/`postgres://` أو `distributed://` مع `CELA_POSTGRES_URL` و`CELA_REDIS_URL`) على **أي** مزوّد، لا على Render وحدها. `0` لا يُعفي من قاعدة Render: مع `RENDER=true` يبقى الاشتراط فعّالًا. الغرض: منصّة بحاوية/قرص مؤقّت تفقد SQLite عند كل إعادة نشر، فيُرفض النشر بدل فقد التشغيلات. |
| `CELA_RUN_STORE_URL` `[*]` | مخزن داخل الذاكرة | `sqlite:///…` أو `postgresql://…` (يحتاج `psycopg` من extra `distributed`). بدونه كل تشغيل يضيع عند إعادة التشغيل. |
| `CELA_POSTGRES_URL` | — | بديل/مرادف لـDSN عند بناء مخزن موزّع في الاختبارات والسكربتات. |
| `CELA_REDIS_URL` | `redis://localhost:6379/0` | ناقل الأحداث للمخزن الموزّع (extra `distributed`). |
| `CELA_MEMORY_STORE_URL` `[*]` | معطّل | تفعيل `os_memory` **بالعنوان فقط**؛ غيابه = الطبقة غير موجودة، لا "تعمل بلا تخزين". يدعم `sqlite:///…`. |
| `CELA_MEMORY_NAMESPACE` | الافتراضي الداخلي | عزل مفاتيح الذاكرة بين نشرات/مستأجرين على نفس المخزن. |
| `CELA_KNOW_STORE_URL` `[*]` | معطّل | مخزن المعرفة (`os_know`)؛ غيابه = لا فهرسة ولا استرجاع. |
| `CELA_SKILL_STORE_URL` `[*]` | معطّل | مخزن مكتبة المهارات القابلة للمراجعة. |
| `CELA_SUBAGENTS_DIR` | معطّل | **وجوده هو المفتاح**: مجلد تعريفات الوكلاء الفرعيين (`agents/sub_agents`). بدونه لا وكيل فرعي يعمل. |
| `CELA_SUBAGENTS_ROOT` | `.` | جذر المسارات النسبية المستخدمة داخل تعريفات الوكلاء. |
| `CELA_MISSION_ROOT` | `.` | جذر أدوات المهمة (`mission_tools`) في السجل التجريبي — كل وصول يمرّ بـ`CELA_MISSION_ALLOW_HOSTS`/`DENY`. |
| `CELA_WAAPI_OUTBOX_PATH` | المسار الداخلي | مسار صندوق الصادر الدائم لـWaAPI (رسائل لا تُفقد عند إعادة التشغيل). |

## ٣) الشبكة والخروج

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_MISSION_ALLOW_HOSTS` `[*]` | قائمة فارغة ⇒ رفض | قائمة سماح صريحة لأدوات المهمة. لا تتجاوز أرضية SSRF أبدًا. |
| `CELA_MISSION_DENY_HOSTS` `[*]` | — | رفض يُقيَّم **قبل** السماح، ليقتطع نطاقًا فرعيًا من نمط سماح واسع. |
| `CELA_REACH_ENABLE` | `0` | تشغيل طبقة الوصول للمحتوى (`os_reach`) وقنواتها. |
| `CELA_REACH_CONFIG` | `~/.config/cela/reach.toml` | ملف إعداد القنوات. |
| `CELA_REACH_ALLOW_HOSTS` `[*]` | فارغة ⇒ رفض | سماح صريح لمضيفي Reach. |
| `CELA_REACH_DENY_HOSTS` `[*]` | — | رفض يسبق السماح. |
| `CELA_REACH_AUDIT_LOG` | — | مسار تدقيق تنفيذ أوامر CLI. |
| `CELA_REACH_CLI_ENABLE` | `0` | تفعيل قنوات CLI (تنفيذ أوامر) — الأخطر في المجموعة، معطّل افتراضيًا. |
| `CELA_REACH_CLI_TIMEOUT` | حد داخلي بالثواني | سقف زمن الأمر الواحد. |
| `CELA_REACH_CLI_ALLOW_AUTH` | `0` | هل تُسمح أوامر تحتاج مصادقة (توكن/مفتاح)؟ `0` = تُرفض. |
| `CELA_REACH_CLI_DOCTOR` | — | تجاوز مسار تشخيص القناة في `reach doctor`. |
| `CELA_REACH_USER` | `$USER` ثم `$USERNAME` | هوية تُمرَّر لبعض القنوات؛ تُقرأ من الإعداد أولًا. |
| `CELA_LAUNCHER_ENABLE` | `0` | صفحة/أدوات `/launcher` (قراءة فقط: كتالوج وإصدارات، بلا تنزيل أو تثبيت). |
| `CELA_LAUNCHER_FEED_URL` | موجز المشروع | موجز الكتالوج البديل (انظر `os_server.launcher_api`). |
| `CELA_LAUNCHER_ALLOW_HOSTS` `[*]` | فارغة ⇒ رفض | سماح للمضيفين. |
| `CELA_LAUNCHER_DENY_HOSTS` `[*]` | — | رفض يسبق السماح. |
| `CELA_MCP_ENABLE` | `0` | وصل خوادم MCP كأدوات. تشغيله **لا يكفي** وحده: يجب أن يسمّي ملف الإعداد خادمًا. |
| `CELA_MCP_CONFIG` | مسار المستودع | ملف إعداد MCP. ملف تالف ⇒ ملاحظة في السجل، لا انهيار. |
| `CELA_MCP_ALLOW_HOST_EXEC` | `0` | يسمح بتشغيل خوادم MCP محليّة (stdio) كعمليات على المضيف — أوسع صلاحية في المستودع. |

## ٤) النماذج

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_USE_LIVE_PROVIDER` `[*]` | `0` | `0` = عرض سكربتي بلا مفتاح. `1` مع مزوّد ناقص ⇒ **فشل عند الإقلاع** بدل تقديم ردود سكربتية تحت وسم "مباشر". |
| `CELA_DEFAULT_PROVIDER` | `Groq` | مزوّد `cela_pro_app.py` الافتراضي (لا تبديل تلقائي عند الفشل). |
| `CELA_MODEL_ID`, `CELA_MODEL_BASE_URL` | — | النموذج/العنوان لمزوّد OpenAI-متوافق يقرأه الخادم (أسماء قديمة للتوافق). |
| `CELA_PROVIDER_MODEL`, `CELA_PROVIDER_BASE_URL` | — | المرادفان الحاليان. |
| `CELA_PROVIDER_API_KEY_ENV` | — | **اسم** المتغيّر الحامل للمفتاح (لا المفتاح)، يُقرأ عند الطلب. |
| `CELA_PROVIDER_CREDENTIAL_VAR` | — | المرادف الحالي لـ`CELA_PROVIDER_API_KEY_ENV`. |
| `CELA_LIVE_MODEL` | — | تجاوز معرّف النموذج في التشغيل الحي (اختبارات/تقييمات). |
| `CELA_TOKEN_BUDGET` | سقف داخلي | ميزانية التوكنز للتشغيل؛ عند النفاد يخفض الموجّه الطبقة ثم يوقف التشغيل بسبب `budget`. |
| `CELA_TOKEN_ESTIMATOR` | `chars/4[legacy]` | مقدّر التوكنز: التقدير التقريبي أو tokenizer فعلي عند توفره. |
| `GROQ_*`, `GEMINI_*`, `OPENROUTER_*`, `KIMI_*`, `OLLAMA_*` `[*]` | افتراضيات الكود | لكل مزوّد: `_API_KEY`, `_MODEL`, `_BASE_URL`, `_REQUEST_EXTRA_JSON` (كائن JSON يُدمج في جسم الطلب لمعاملات المزوّد/الوسيط). `cela_pro_app.py` يبني هذه الأسماء ديناميكيًا، لذا لا يجدها فحص ثابت. |

## ٥) التقييمات والقياس

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_EVAL_MODEL_URL` | — | نقطة نهاية نموذج حقيقي لتشغيل `os_evals` الحيّ. |
| `CELA_EVAL_MODEL_ID` | فارغ | معرّف النموذج؛ **إلزامي** مع `--model-url`. |
| `CELA_EVAL_API_KEY_ENV` | `CELA_EVAL_API_KEY` | **اسم** المتغيّر الحامل لمفتاح التقييم. |
| `CELA_EVAL_RECALL_FLOOR` | حد داخلي | أرضية `recall@k` التي تُفشل بوابة الاسترجاع. |
| `CELA_EVAL_MRR_FLOOR` | حد داخلي | أرضية `MRR` لنفس البوابة. |
| `CELA_OTEL_ENDPOINT` | — | مجمّع OTLP؛ غيابه = التصدير يُنتج OTLP/JSON صالحًا محليًا بلا شبكة. |
| `CELA_OTEL_SERVICE` | `celia-mission-os` | اسم الخدمة في بيانات OTLP. |

## ٦) التكاملات

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_INDIGO_ENABLE` | `0` | تفعيل تكامل Indigo، مستقل تمامًا عن إقلاع النواة. |
| `CELA_INDIGO_BASE_URL` | عنوان الخدمة | عنوان Indigo. |
| `CELA_INDIGO_CREDENTIAL_VAR` | — | **اسم** متغيّر بيانات الاعتماد (لا القيمة). |
| `CELA_ACTIVEPIECES_WEBHOOK_URL` `[*]` | — | هدف الإشعار الصادر. |
| `CELA_ACTIVEPIECES_WEBHOOK_SECRET` `[*]` | — | سر توقيع الويب هوك. |
| `CELA_ACTIVEPIECES_DAILY_LIMIT` / `_MONTHLY_LIMIT` `[*]` | `100` / `1000` | سقوف إرسال صريحة قبل أي استدعاء. |
| `CELA_WAAPI_ENABLE` `[*]` | `0` | جسر WaAPI؛ لا يُفعَّل قبل التحقق من الاعتماد والويب هوك. |
| `WAAPI_*` `[*]` | — | اعتماد المزوّد وسر مسار الويب هوك وترويسة إضافية اختيارية. |

## ٧) التضمين (embeddings)

نفس عقد `CELA_EMBED_*` في `.env.example` — الفشل المغلق هو القاعدة:

- `CELA_EMBED_BACKEND` — `off` (افتراضي) \| `hash` \| `feature-hash` \| `model` \| `provider`.
- `CELA_EMBED_ALLOW_PROVIDER=0` — الموافقة الصريحة الوحيدة على إرسال نص المدوّنة خارج الجهاز.
- `CELA_EMBED_ALLOW_HOSTS` — قائمة `host[:port]`، HTTPS فقط، بلا رموز نمطية ولا عناوين IP، محصورة بـ`tool="embeddings"`.
- `CELA_EMBED_ALLOW_DOWNLOAD=0` — `model` فقط: `1` يسمح بأول تنزيل أوزان؛ `0` = أخطاء عند غياب الذاكرة المخبئية فقط.

## ٨) الواجهة (packages/web) — رموز لا تصل للمتصفح

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_BACKEND_URL` `[*]` | — | عنوان الـbackend المقروء في خادم Next/Pages Function فقط. `http://` مرفوض خارج loopback. |
| `CELA_BACKEND_TIMEOUT_MS` | 25000 (حدود 100–60000) | مهلة الوكيل؛ انتهاؤها يعطي `503 backend_starting` قابلًا لإعادة المحاولة. |
| `CELA_WEB_ACCESS_SECRET` `[*]` | — | بوابة وصول المشغّل. غيابه مع `NODE_ENV=production` **أو غير معروف** ⇒ `503 access_gate_unconfigured` (فشل مغلق)، لا وكيل مفتوح. |
| `CELA_WEB_GATE_REQUIRED` `[*]` | — | `1` يفرض الفشل المغلق، `0` يفتح صراحةً لبيئة محلية/اختبار. |
| `CELA_STATIC_EXPORT` | — | `1` يبني تصديرًا ثابتًا لـCloudflare Pages (يستبعد وكيل Next، وتتولى الحافة الدور). |
| `NODE_ENV` | — | `development`/`test` = مفتوح؛ أي شيء آخر (أو غياب) = إنتاج ⇒ فشل مغلق. |

## ٩) هوية الصورة (تُحقن وقت البناء)

| المتغيّر | الافتراضي | المعنى |
|---|---|---|
| `CELA_IMAGE_REF`, `CELA_IMAGE_TAG`, `CELA_IMAGE_DIGEST` | فارغة | مرجع/وسم/بصمة الصورة المعروضة في `/registry`. فارغة ⇒ الصفحة تقول "غير مضبوط" بصدق بدل تخمين. |
| `CELA_BUILD_SHA`, `CELA_BUILD_TIME` | فارغة | بصمة البناء ووقته، من `docker-image.yml`. |

## ١٠) متغيّرات خاصة بالاختبارات فقط

`CELA_E2E_TEST_MODE`, `CELA_TEST_POSTGRES_DSN`, `CELA_*_TEST_KEY`, `CELA_FC_MISSING_XYZ`,
`OS_SANDBOX_TEST_SECRET` — لا تُضبط في نشر حقيقي. أسماء تحمل `*_MISSING_*`/`_STUB_` مقصودة
لاختبار مسارات "المفتاح غائب"، وأي مطابقة نمطية لها في الشيفرة ليست إعدادًا فعليًا.

---

## كيف يتحقق هذا المستند من نفسه؟

`tests/test_docs_claims.py` يمنع أن تكذب الوثائق عن الشيفرة. الفحص الآلي المستخدم في مراجعة
2026-10-21 هو استخراج كل `CELA_*`/`OS_*` من `packages/`, `scripts/`, `tests/`, `packages/web/`
ومقارنتها بهذا الملف و`.env.example`: لا يبقى متغيّر مستخدم بلا توثيق.
