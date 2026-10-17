# Celia.sh — Mission OS: تثبيت النطاق وخطة التنفيذ فوق `open_source-`

**التاريخ:** 2026-10-17 · **الحالة:** تثبيت النطاق قبل التنفيذ (Scope Lock & Gap Analysis)
**المستودع المعتمد:** `sayedelazameydesign-crypto/open_source-` (الفرع: `arena/a3bfef89-open-source`)
**الهدف:** بناء وإطلاق **Celia.sh — Mission OS** (من المهمة ← الخطة ← التنفيذ بالأدوات الفعلية ← الأدلة ← التقرير القابل للتنزيل) بتكلفة تشغيل **0 دولار ودون بطاقة بنكية**، مع **عدم إعادة بناء أي مكوّن موجود ومُختبَر بالفعل في هذا المستودع**.

---

## 1) لماذا `open_source-` هو المستودع الصحيح (جرد ما هو مبني ومُختبَر فعلًا)

قبل كتابة سطر كود جديد، هذا جرد دقيق يثبت أن **أكثر من 75% من البنية التحتية والأمنية لواجهة ومحرك `Celia.sh — Mission OS` مبني ومُختبَر بالفعل** في هذا المستودع (أكثر من **1,069 اختبارًا آليًا ناجحًا** دون شبكة ودون مفاتيح):

| مكوّن `Celia.sh — Mission OS` المطلوب | الموجود فعليًا في `open_source-` (لا يُعاد بناؤه) | الملفات والأدلة في الشجرة |
|---|---|---|
| **1. الهوية والواجهة العربية (`CELIA WORKSPACE`)** | واجهة عربية أصيلة (RTL) متجاوبة (`1440px` / `1024px` / `390px`) بتبويبات **«المهمة · النشاط · الأدلة والملفات»**، مع وكيل خادمي يخفي الرموز السرية عن المتصفح | `packages/web/components/app-shell.tsx`, `packages/web/app/page.tsx`, `docs/UI_1_WORKSPACE.md` |
| **2. صدق العرض (عدم ادعاء نجاح وهمي)** | مكون `ExecutionDisclosure` وحقول `execution.kind` (`scripted` / `provider_adapter`) و`tool_results_summary` و`goal_verification: not_verified` التي تفصل انتهاء دورة النموذج عن نجاح الأداة الفعلي | `packages/web/components/execution-disclosure.tsx`, `packages/os_core/os_core/state.py`, `docs/UI_0_EXECUTION_TRUTH.md` |
| **3. وكيل المهام والتخطيط والموافقات (`Mission Agent` + `HITL`)** | محرك `PlanningFlow` (DAG) و`ReAct` مع كاسر حلقات وميزانية (`budget.py`) وموافقات بشرية تفشل مغلقةً (`QueueApprover` + `POST /runs/{id}/approve`) | `packages/os_agents/{flow,react,budget}.py`, `packages/os_core/os_core/hitl.py` |
| **4. تأمين التنفيذ وحارس SSRF** | حارس `SSRFGuard` (يمنع IP الخاص، `169.254.169.254`، وDNS rebinding، ويعيد فحص كل redirect) + `EgressPolicy` + حواجز حقن الأوامر بالعربية والإنجليزية والصينية (`Guardrails`) | `packages/os_tools/{ssrf,egress,code_exec}.py`, `packages/os_core/os_core/guardrails.py` |
| **5. فحص الروابط والمستودعات العامة** | حزمة `os_reach` تجلب صفحات الويب وبيانات مستودعات GitHub وRSS عبر `httpx` خالص خلف `SSRFGuard` و`EgressPolicy` وسجل تدقيق (`audit.py`) | `packages/os_reach/os_reach/{native,security,audit,tools}.py` |
| **6. توليد التقارير الحتمية وتنزيلها بـ `SHA-256`** | مسار `DocumentRuns` يولّد تقرير Markdown حتميًا ويحسب بصمة `sha256` ويتحقق منها قبل التنزيل مع `nosniff` و`no-store` | `packages/os_server/os_server/documents.py`, `docs/MVP_0_DOCUMENT_RUNS.md` |
| **7. التخزين الدائم وسجل النتائج (`RunStore` + `RunLedger`)** | واجهة `RunStore` مع `SQLiteRunStore` و`PostgresRunStore`، وسجل نتائج دائم `RunLedger` (`ledger.jsonl`) وتنقية الأسرار (`os_core.redact.scrub_credentials`) | `packages/os_server/os_server/storage.py`, `packages/os_core/os_core/{ledger,redact}.py` |
| **8. تعدد مزوّدي الذكاء الاصطناعي المجانيين** | دعم `Gemini` (`gemini-2.5-flash`) و`Groq` و`OpenRouter Free` عبر بروتوكول OpenAI المتوافق، مع فحص اتصال صريح (`PASS` / `FAIL` / `BLOCKED` / `INCONCLUSIVE`) وعدم التبديل الصامت | `cela_pro_app.py`, `packages/os_core/os_core/models.py`, `CELA_PRO_STATUS.md` |

---

## 2) الفجوة الدقيقة (Delta) — ما نحتاج إلى بنائه فقط لإطلاق `Celia.sh — Mission OS`

بدل إعادة كتابة الخادم أو الواجهة، ينحصر العمل المطلوب للوصول إلى **قائمة قبول الإطلاق (8 من 8)** في **5 إضافات مركّزة** فوق الطبقات القائمة:

### الإضافة 1: الأدوات الثلاث الحقيقية لمهندس فحص المشاريع (`os_tools` / `os_reach`)
نبني ثلاث أدوات حتمية مسجَّلة في `ToolRegistry`، تنتج كلٌّ منها **دليلًا مهيكلًا (`EvidenceRecord`: التوقيت، المدخل، المخرج، بصمة `sha256`، وحالة التحقق `VERIFIED_TOOL` أو `STATIC_ONLY` أو `FAILED`)**:
1. **`inspect_public_url` (فحص رابط عام):**
   * يستخدم `os_reach` + `SSRFGuard` + `EgressPolicy` لفحص حالة الرابط (`status_code`، زمن الاستجابة، ترويسات الأمان مثل `CSP` و`HSTS` و`X-Frame-Options` و`X-Content-Type-Options`، وعنوان الصفحة).
   * لا يرسل أي مفتاح سري، ويرفض العناوين الداخلية وروابط البيانات الوصفية السحابية (`169.254.169.254`).
2. **`inspect_project_files` (فحص بنية المشروع وإعداداته):**
   * يفحص مستودعًا عامًا (عبر `github_repo` في `os_reach`) أو ملفات مشروع مرفوعة/محلية ضمن مسار مسموح: يكتشف ملفات الإعداد (`pyproject.toml`, `package.json`, `Dockerfile`, `.github/workflows/*.yml`, `.env.example`)، ويفحص وجود أسرار مسرّبة عبر `os_core.redact.contains_secret`، ويكتشف غياب قفل التبعيات أو فحوص CI.
   * **قاعدة الصدق الإلزامية:** يصنّف نتيجته صراحةً بأنها `STATIC_INSPECTION` («فحص ساكن للملفات — لم تُشغَّل اختبارات المشروع تنفيذيًا»)، ولا يعلن نجاح الاختبارات إلا إذا شُغِّلت أداة اختبار فعلية وعادت برمز خروج `0`.
3. **`generate_deployment_plan_report` (بناء خطة النشر والتقرير النهائي):**
   * أداة حتمية تجمع مخرجات الفحص وتبني خطة نشر مجانية وآمنة (Cloudflare Pages + Render + Supabase Postgres + GitHub Actions + Activepieces) مخصّصة لبنية المشروع المفحوص، وتصدر **3 ملفات مخرجات (Artifacts)** مرتبطة بالمهمة وموقّعة بـ `sha256`:
     * `mission-report.md` (تقرير Markdown كامل).
     * `mission-report.json` (تقرير مهيكل بالأدلة وحالات التحقق).
     * `mission-report.html` (تقرير HTML مستقل ومنقّح عبر `scrub_credentials`، معزول عن تنفيذ أي سكربت، جاهز للتنزيل أو المشاركة الآمنة).

### الإضافة 2: عقد المهام الموثّقة وتنزيل التقارير (`os_server`)
دون كسر `POST /runs` أو `/document-runs` أو `/cela/v1`:
1. **استرجاع المهمة بعد تحديث الصفحة (البند 1 من قائمة القبول):**
   * ربط معرف المهمة في رابط الواجهة (`/?run_id=run_...`) وإضافة قائمة آخر المهام من `GET /runs` حتى يستطيع المستخدم تحديث الصفحة أو العودة للمهمة ورؤية الخطة والأدلة والتقرير دون ضياعها.
2. **مسار تنزيل مباشر وآمن لمخرجات المهام (`GET /runs/{run_id}/artifacts/{filename}/download`):**
   * يتحقق من بصمة `sha256`، ويمرّر المحتوى على `scrub_credentials`، ويعيد الملف بترويسات `Content-Disposition: attachment` و`X-Content-Type-Options: nosniff` و`Cache-Control: no-store`.
3. **حالات المهمة القياسية:**
   * توحيد عرض الحالات الخمس المطلوبة في الواجهة والـ API: `queued` (`pending`) · `running` · `awaiting_approval` · `completed` · `failed` (مع الإبقاء على التمييز الصريح بين اكتمال المحرك وحالة التحقق من الهدف).

### الإضافة 3: تشغيل الذكاء الاصطناعي ومحرّك الفحص دون بطاقة بنكية (`Model Provider + Fallback Core`)
1. **توصيل مزوّد النموذج الاختياري في `os_server` عبر البيئة:**
   * قراءة `GEMINI_API_KEY` (أو `GROQ_API_KEY` / `OPENROUTER_API_KEY`) على الخادم فقط لتشغيل `OpenAICompatModel` بميزانية قصوى مضبوطة (`token_budget`) وبلا أي مزوّد يتطلب فوترة مدفوعة افتراضيًا.
2. **نواة تنفيذ فحص المشاريع المستقلة عن النموذج (`Deterministic Mission Fallback`):**
   * إذا لم يُعيَّن مفتاح نموذج أو نفدت الحصة المجانية (`429 Quota Exceeded`) أو تعطّل المزوّد، **لا ينهار النظام ولا يولّد ردًا وهميًا يدّعي أنه ذكاء اصطناعي**؛ بل يعرض إشعارًا صريحًا («المزوّد غير متاح/نفدت الحصة — يجري التنفيذ عبر نواة الفحص الحتمية المحلية») وينفّذ خطوات الفحص الـ 3–7 بأدواتها الحقيقية ويُخرج التقرير القابل للتنزيل.

### الإضافة 4: التخزين الدائم على `Supabase Postgres` وموصّل `Activepieces`
1. **دعم `postgresql://` (Supabase Postgres) مباشرةً في `run_store_from_url`:**
   * حاليًا `storage.py` يشترط `distributed://` (Postgres + Redis معًا). نضيف دعم `postgresql://` / `postgres://` المباشر عبر `PostgresRunStore` حتى يعمل الخادم على **Render Free + Supabase Postgres Free (500 MB)** دون الحاجة لخدمة Redis إضافية في النسخة الأولى.
2. **موصّل الأتمتة الاختياري (`os_server/automation.py`) لـ Activepieces:**
   * استقبال وإرسال Webhooks موقّعة ومحمية بـ `idempotency_key` و`EgressPolicy` وحارس رصيد يومي يحمي حصة Activepieces المجانية (100 رصيد يوميًا / 1,000 شهريًا).

### الإضافة 5: ترقية واجهة `Celia.sh — Mission OS` (`packages/web`)
1. **عرض المراحل الأربع بوضوح بصري من أول استخدام:**
   * `1. فهم المهمة` ← `2. إنشاء خطة (3–7 خطوات)` ← `3. التنفيذ والتحقق بالأدلة` ← `4. تسليم النتيجة وتنزيل التقرير (MD / JSON / HTML)`.
2. **ثنائية اللغة (`عربي RTL` ↔ `English LTR`):**
   * زر تبديل فوري في شريط `CELIA WORKSPACE` يبدّل اتجاه الصفحة (`dir="rtl"` / `dir="ltr"`) ونصوص الواجهة الأساسية.
3. **صفحة/لوحة «حالة المنصة الحية» (`/status` أو شريط الحالة):**
   * توضّح بصدق ما هو حيّ فعليًا (نواة الفحص، المخزن الدائم، حارس SSRF) وما هو اختياري يحتاج مفتاحًا (مزوّد النموذج، ربط Activepieces).

---

## 3) مطابقة قائمة قبول الإطلاق (8 من 8) مع الاختبارات الآلية التي سنكتبها

| # | معيار القبول في `Celia.sh — Mission OS` | كيف يُثبَت برمجيًا في المستودع (بلا ادعاء) |
|---|---|---|
| **1** | يستطيع المستخدم إنشاء مهمة بالعربية وإعادة فتحها بعد تحديث الصفحة | حفظ `run_id` في الرابط (`?run_id=...`) + استرجاع اللقطة والأحداث من `RunStore` (`SQLite` / `Postgres`) + اختبار في `tests/test_mission_os.py` واختبار متصفح |
| **2** | تظهر خطة المهمة (3–7 خطوات) وحالة كل خطوة دون ادعاء تنفيذ غير حقيقي | فرض نطاق 3–7 خطوات في مخطط المهمة + ربط حالة الخطوة بسجل `recorded_tool_results` + `ExecutionDisclosure` |
| **3** | تعمل أدوات الفحص الثلاث فعليًا وتُظهر الأدلة والأخطاء | اختبارات حقيقية لـ `inspect_public_url` و`inspect_project_files` و`generate_deployment_plan_report` تسجّل الأدلة (`sha256` + التوقيت) وتختبر حالة الخطأ |
| **4** | ينتج النظام تقريرًا قابلًا للتنزيل (`MD` / `JSON` / `HTML`) مرتبطًا بالمهمة | مسار `GET /runs/{id}/artifacts/{name}/download` مع فحص `sha256` و`Content-Disposition` وتنقيح `scrub_credentials` |
| **5** | تظل البيانات المهمة محفوظة بعد إعادة تشغيل الخادم | اختبار يغلق التطبيق ويعيد إنشاء `create_app` فوق نفس قاعدة `SQLiteRunStore` / `PostgresRunStore` ويتأكد من بقاء المهمة والخطة والتقرير |
| **6** | تنجح اختبارات API والفحوص الأمنية في CI | نجاح `ruff check .` + `pytest -q` + بناء `packages/web` في GitHub Actions |
| **7** | لا تُكشف مفاتيح API ولا يُسمح بالتنفيذ غير المصرح به | المفاتيح على الخادم فقط، تنقيح `scrub_credentials` على كل مخرج وتقرير، منع أوامر Shell الاعتباطية، وحارس `SSRFGuard` + `EgressPolicy` + `HITL` |
| **8** | لا تتطلب المسارات الأساسية اشتراكًا مدفوعًا أو بطاقة بنكية | عمل رحلة الفحص والتخطيط والتقرير كاملةً بنواة الفحص الحتمية حتى عند غياب مفتاح LLM أو نفاد حصته المجانية (`429`) |

---

## 4) ترتيب التنفيذ المرحلي المقترح (4 مراحل متسلسلة)

1. **المرحلة 1 (نواة المهام والأدوات الثلاث والتقارير القابلة للتنزيل — `os_tools` + `os_server`):**
   * بناء أدوات الفحص الثلاث (`inspect_public_url`, `inspect_project_files`, `generate_deployment_plan_report`).
   * إضافة مسار تنزيل التقارير (`MD` / `JSON` / `HTML` منقّح) ودعم `postgresql://` المباشر لـ Supabase Postgres.
   * دعم التراجع الصادق عند نفاد حصة النموذج (`Quota/Provider Fallback` إلى نواة الفحص الحتمية).
2. **المرحلة 2 (واجهة `Celia.sh — Mission OS` في `packages/web`):**
   * تحديث الواجهة لعرض المراحل الأربع (فهم المهمة ← الخطة 3–7 خطوات ← التنفيذ والأدلة ← تنزيل التقرير).
   * دعم استعادة المهمة بعد تحديث الصفحة (`?run_id=...`) + تبديل اللغة (`AR RTL` / `EN LTR`) + صفحة حالة المنصة الحية.
3. **المرحلة 3 (تأمين التنفيذ وموصّل `Activepieces`):**
   * إضافة `os_server/automation.py` للويب هوكس الموقّعة وحارس الرصيد اليومي + اختبارات SSRF وحجب الأسرار وفشل النموذج/قاعدة البيانات.
4. **المرحلة 4 (ملفات النشر المجاني والتحقق الشامل):**
   * إعداد `render.yaml` ووكيل Cloudflare Pages/Workers وفحص `deploy_doctor.py` وتشغيل حزمة التحقق الكاملة.
