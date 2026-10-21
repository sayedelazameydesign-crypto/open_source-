# الأدلة — ما يمكن التحقق منه، وكيف

هذا الملف هو **المكان الوحيد** الذي يحمل عدد اختبارات المشروع. الرقم أدناه
مُولَّد من المجموعة نفسها بأمر، ويفحصه CI؛ لا يُحرَّر يدويًا، ولا يُنسخ إلى
`README.md`. سبب وجوده مباشر: رقم مكتوب بخط اليد في README (1069) بقي ثابتًا
بينما كبرت المجموعة، وصار أول دليل يقرأه الزائر هو أول دليل يخذله.

<!-- generated:test-count -->
**1528 اختبارًا في المجموعة** — 1525 مُختارًا · 3 مُستبعَدًا (deselected) · `pytest --collect-only -q`
<!-- /generated:test-count -->

> **الرقمان ليسا تناقضًا.** العدد أعلاه حصيلة `pytest --collect-only -q`، أي
> المُختار **زائد** المُستبعَد. الثلاثة موسومة `network`: اختبارا eval حيان
> واختبار Chromium حيّ يحمّل صفحة Python.org ويثبت حجب الموارد من الأصول الخارجية.
> سطر `pytest -q` يطبع `N passed, 3 deselected` ويقيس المُختار وحده، فهو أقل
> من العدد أعلاه بثلاثة بالضبط. المقارنة الصحيحة هي مع `--collect-only`، لا مع سطر النجاح.

كيف يُفحص:

```bash
make evidence          # يعيد توليد الكتلة أعلاه من المجموعة
make evidence-check    # يفشل إن كانت الكتلة لا تطابق ما تجمعه المجموعة فعلًا
```

`--check` يُشغَّل في CI على Python 3.11، فلا يمرّ تغيير يضيف اختبارات (أو
يحذفها) دون تحديث الرقم. `pytest -q` يشغّل المجموعة المحلية دون شبكة ويستبعد
الاختبارات الثلاثة المعلّمة `network`؛ مهمة `live-browser` في CI تشغّل اختبار
Chromium الحيّ صراحةً، لكن اختباري Reach الشبكيين لا تشغّلهما `ci.yml` حاليًا.
يثبّت CI extra `papers` حتى يُنفَّذ اختبار PDF الفعلي، و`ruff check .` يفحص الأسلوب.

## ١) الأوامر التي تُنتج كل ادعاء

| الادعاء | الأمر | ما يعنيه بالضبط |
|---|---|---|
| عدد الاختبارات | `pytest --collect-only -q` | ما **تُجمعه** المجموعة، لا ما نجح في تشغيل بعينه |
| نجاح المجموعة | `pytest -q` | يشغّل الاختبارات المحلية؛ يستبعد 3 اختبارات موسومة `network` افتراضيًا |
| نظافة الأسلوب | `ruff check .` | كل قواعد `pyproject.toml` |
| واجهة الويب | `npm run test:components` / `test:hooks` / `test:deploy` | مكوّنات React · خطّافات · دالة الحافة (بلا شبكة) |
| بناء الويب (شكلان) | `npm run build` · `npm run build:pages` | بناء Next الخادمي · تصدير ثابت لـ Cloudflare Pages |
| جاهزية النشر | `python scripts/deploy_doctor.py --strict` | أسرار وعناوين؛ Render يشترط مخزن Postgres دائمًا و`CELA_CODE_EXEC_MODE=disabled` في البيئة وBlueprint الهدف؛ هذا preflight ليس orchestrator مركزيًا |
| artifacts | `render.yaml` واختبارات GHCR | Blueprint واحد في الجذر لـRender؛ بقية container providers تستخدم صورة GHCR ذات مدخل `deploy/g1a` |
| Provider API حيّ | **غير مفعّل** | `OpenAICompatModel` لم يُربط بعد بالنقل المثبّت أو allowlist خاصة بالمزوّد؛ لا تفعّل `CELA_USE_LIVE_PROVIDER=1` حتى مراجعة host Gemini وإكمال adapter واختباراته |
| هوية خدمة منشورة | `python scripts/verify_deployment.py --api https://<service>.onrender.com` | HTTPS/443 · allowlist افتراضية `*.onrender.com` · DNS pinning · `/health` أولًا بلا رمز · بلا redirects أو proxy env · DB خارج فحص HTTP |
| مستندات PDF | `pip install -e ".[dev,server,papers]"` ثم `pytest tests/test_resource_collection.py` | extra `papers` يثبت `pypdf`؛ CI و`make install` يثبتانه لتشغيل التغطية الحقيقية |
| Chromium حيّ | `pytest -m "browser and network" tests/test_browser_live.py` | الاختبار يشترط محتوى من Python.org ورصد طلبات CDN عابرة للأصل وفشلها؛ لا يُعدّ نجاحًا حتى يمر job الـCI |

## ٢) لقطة تحقّق — 2026-10-17 عند `3498ce3`

هذه **لقطة مؤرَّخة**، لا رقم حيّ. الرقم الحيّ الوحيد هو الكتلة المُولَّدة أعلاه.

| الفحص | النتيجة |
|---|---|
| `pytest -q` | بلا فشل (العدد أعلاه؛ اختبار المخزن الموزّع يتخطّى حين يكون `psycopg` مثبَّتًا) |
| `ruff check .` | All checks passed |
| `npm run test:components` / `:hooks` / `:deploy` | 22 / 11 / 15 — كلها ناجحة |
| `npm run build` | ٦ مسارات، منها `ƒ /api/backend/[...path]` |
| `npm run build:pages` | تصدير ثابت: ٥ صفحات، بلا مسار `/api` |
| دمج نشر الخطة المجانية | PR #45 ← `554f3b9` |

## ٢-ب) لقطة تحقّق ثانية — 2026-10-19 عند `4fac0a8`

أُضيفت لالتقاط حالة ما بعد إصلاح تنقيح أخطاء المزوّد. لا Postgres ولا Render هنا
أيضًا؛ العمود الأخير يقول ما **لم** يُنفَّذ صراحةً.

| الفحص | النتيجة |
|---|---|
| `make check` | `ruff` نظيف · `evidence-check` مطابق · `1265 passed, 2 deselected` |
| `pytest --collect-only -q` | `1267` اختبارًا مجمَّعًا (١٢٦٥ مُختارًا + ٢ مُستبعَدين) |
| اختبارات تنقيح جسم خطأ المزوّد | ١٧ اسم حقل (`api_key`, `secret_key`, `private_key`, `access_key`, `client_secret`, `private_token`, …) × شكلَي الجسم (JSON ونص) = ٣٤ حالة مُعامَلة، كلها تُخفي القيمة؛ + اختبار يُثبت إخفاء المفتاح المُرسَل في الترويسات حين يعيده الجسم |
| A1/A6 الحي (Gemini) | نجح حيًّا: run 39258093213 عند `fe43a7f` — **7/7 معايير PASS**، ومنها المعيار ٦: `tool_calls_in_one_turn=2`، `tool_results=2`، `ids_round_trip=True`، `followup_http=200`، والمزوّد أعاد الإجابتين (55 و1024). التفاصيل تُقرأ من تعليقات التشغيل (Checks API) لا من الـArtifact |
| C5 (Render) | **لم يُنفَّذ** |
| C6 (PostgreSQL) | **لم يُنفَّذ** — لا قاعدة اختبار حيّة |

## ٢-ج) إصلاح سياسة النشر وفحص الخدمة — 2026-10-20

النتائج المحلية أدناه تخص الـworking tree الحالي، ولا تدّعي تنفيذ Render. صف Chromium يوثّق job منفصلًا في GitHub Actions على commit `9721494` بعد إصلاحات CI وVercel. فُعّل `pypdf` من extra `papers` قبل تشغيل المجموعة.

| الفحص | النتيجة |
|---|---|
| `make check` | نجح بعد إضافة حواجز تشغيل الـproviders: `ruff check .` · `evidence-check` · `vendored-verify` (198 ملفًا) · `1525 passed, 3 deselected` |
| `ruff format --check .` | **يفشل على 34 ملفًا موجودًا مسبقًا خارج تغييرات هذه الجولة**؛ ملفات Python المعدّلة تمرّ بفحص التنسيق المستهدف. لم أُعد تنسيق ملفات غير مرتبطة لتجنب diff جانبي واسع |
| `git diff --check` و`python scripts/update_evidence.py --check` | نجحا؛ الكتلة المولّدة تسجل 1528 مجموعة / 1525 مختارًا / 3 مستبعدة |
| `pytest -m network` | **محاولة محلية فاشلة: 3/3**؛ Chromium تعذّر لعدم تثبيت Playwright، وGitHub فشل TLS بسبب CA محلي، وexample.org أنهى TLS بـEOF. لا ادعاء بأن اختبار CDN نجح محليًا |
| PDF / `papers` | extra يثبت `pypdf 6.21.1`؛ الاختبار الحقيقي دخل المجموعة بدل تخطيه |
| Probe / نشر مستهدف | 439 اختبارًا ناجحًا؛ تشمل TLS/SNI، hostname verification، proxy env، health-first/no-token، تنقيح token في body/exception/config، redirects scheme/port، fallback بين IPv6 وIPv4، allowlist Render، وبوابتي code-exec وPostgres |
| Chromium حيّ | نجح job `live-browser` في CI run `39523895556` على commit `9721494`; شغّل Python.org مع قياس حجب CDN |
| حواجز اختبارات الـprovider | 11 اختبارًا مستهدفًا نجحت؛ أزيلت مشغلات `push` من workflows الحية، وصارت dispatch اليدوية شرطًا، كما أصبح فحص Indigo يعمل offline افتراضيًا ما لم يكن `ALLOW_LIVE_EXTERNAL_CALLS=true` صراحةً |
| Render / Postgres حيّان | **لم يُنفَّذا**؛ لا توجد خدمة/قاعدة تجربة مرتبطة بهذه الجولة |
| بوابة نشر مركزية | **غير موجودة بعد**؛ `deploy_doctor --strict` يفحص Render Blueprint والبيئة لكنه preflight يدوي لا يفرضه orchestrator لكل provider. هذا مانع لأي deploy، ولا يُتجاوز باختيار provider آخر |

## ٢-د) نتائج CI وVercel للـPR #83 — 2026-10-20

هذه النتائج تخص head `9721494` (بعد إصلاح تعارض `main` وحواجز الـprovider وتوافق أداة الويب). الـPR ما يزال مفتوحًا؛ لا تعني النتائج نشر Render أو تجاوز بوابة النشر المركزية.

| الفحص | النتيجة |
|---|---|
| GitHub CI `39523895556` | **نجح** على Python 3.11 و3.12 و3.13؛ `live-browser` نجح أيضًا |
| CodeQL `39523895517` | **نجح** لكل اللغات المهيّأة |
| Web CI `39523895677` | **نجح**: `npm ci`، lint، typecheck، اختبارات الويب، `next build` و`build:pages` |
| Docker PR smoke `39523895602` | **نجح** البناء وفحص `/health` وبوابة المصادقة؛ خطوة النشر إلى GHCR متخطاة في حدث PR |
| Vercel | **نجح** بعد تثبيت TypeScript `~5.9.3` و`eslint-plugin-react-hooks` `~5.2.0`. كان إعداد `main` السابق يستخدم TypeScript 7 خارج peer range لـ`typescript-eslint` ومع plugin hooks أحدث يرفض أنماط effects الحالية |
| تشغيل providers غير المقصود | على commit `81ea197` أدّت مشغلات `push` القديمة إلى تشغيل فحصي Gemini وIndigo الحيّين (`39522076999` و`39522076907`) دون dispatch يدوي؛ سجّل كل منهما 7/7، وأُرسلت طلبات خارجية فعلية. لم تُطبع قيم الأسرار. أزيلت مشغلات `push` وجُعل التشغيل اليدوي شرطًا، وفحص Indigo offline افتراضيًا. لم يحدث deploy؛ أبقِ `CELA_USE_LIVE_PROVIDER=0` |
| Render / PostgreSQL / بوابة النشر | **لم تُنفَّذ** خدمة Render أو قاعدة PostgreSQL حيّة؛ البوابة المركزية ما تزال مانع النشر |

## ٣) ما لا يغطّيه هذا الملف — بصراحة

* **لا Postgres ولا Redis حيّين في CI.** `PostgresRunStore` و`DistributedRunStore`
  مُختبَران بمزودين مزيَّفين (اتصال DBAPI على SQLite + عميل Redis في الذاكرة)،
  وتشغيلهما متعدد النسخ يحتاج خدمتين حقيقيتين — انظر [ADR-0006](../adr/0006-distributed-run-store.md).
* **تكاملات شبكة اختيارية لم تُربط كلها بالنقل المثبّت.** إلى جانب مزوّد النموذج
  (`os_core.models`)، ما زالت واجهات Firecracker-manager وOTLP وWAAPI تستخدم عملاء HTTP
  منفصلين. ليست مفعّلة في إعداد Render الافتراضي؛ لا تفعّلها قبل إضافة سياسة egress وpinning
  واختبار المضيف لكل adapter على حدة. لا تُعدّ `CELA_SANDBOXED=1` بديلًا لذلك.
* **لا أرقام معيارية بعد.** GAIA / SWE-bench / OSWorld غير مبنيّة، والبوابة 2 في
  [`docs/ROADMAP.md`](ROADMAP.md) تنتظر ملف خط أساس منشورًا مثل هذا.
* **لا قياس زمن استجابة منشور.** الخطة المجانية تنام (Render) وتتوقّف بعد خمول
  (Supabase)؛ زمن أول طلب بعد سكون **مقيس** لا مُقدَّر، ولم يُقَس بعد.
