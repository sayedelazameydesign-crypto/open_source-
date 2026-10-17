# خطة توصيل ودعم وإضافة أدوات الأتمتة ومنصات النشر المجانية (2026)

**التاريخ:** 2026-10-17 · **الحالة:** مقترحة للاعتماد (المرحلة 0 — فحص وتصميم معماري)
**النطاق:** توصيل ودعم وإضافة **4 أدوات أتمتة** (`Activepieces` · `n8n` · `Windmill` · `GitHub Actions`)
و**4 منصات نشر واستضافة مجانية دون بطاقة ائتمان** (`Cloudflare Pages + Workers` · `Render` ·
`GitHub Pages` · `Netlify`) إلى منصة **`cela_use_browser` (`open_source-`)**.
**المراجع المعمارية:** [ADR-0002](../adr/0002-event-bus-state-sync.md) (استئناف SSE بـ`after_seq`) ·
[ADR-0004](../adr/0004-run-store-repository.md) (`RunStore`) ·
[ADR-0005](../adr/0005-production-isolation-and-web.md) (الواجهة وعزل الرموز) ·
[ADR-0014](../adr/0014-run-outcome-ledger.md) (`RunLedger`) ·
[ADR-0017](../adr/0017-mcp-transport.md) (`os_mcp`) ·
[ADR-0018](../adr/0018-third-party-ip-governance.md) ([`docs/THIRD_PARTY_POLICY.md`](THIRD_PARTY_POLICY.md)).

> هذه الخطة مبنية على **فحص المصادر الرسمية مباشرةً** (`api.github.com` + `PyPI` + ملفات `LICENSE`
> لكل مستودع بتاريخ `2026-10-17`) ومطابقتها مع الكود الفعلي في `packages/os_server` و`packages/os_mcp`
> و`packages/web` و`.github/workflows`. أظهر الفحص **6 حقائق تقنية وترخيصية حاسمة** (القسم 1.ب)
> تمنع الوقوع في فخاخ سلسلة التوريد أو كسر رخصة MIT أو تسريب مفاتيح API إلى المتصفح.

---

## 0) الملخص التنفيذي — الأحكام

| الفئة | الأداة / المنصة | الحكم | طريقة التوصيل في `cela_use_browser` | السبب بكلمة واحدة |
|---|---|---|---|---|
| **أتمتة** | **Activepieces** (`Cloud` / `Self-Host`) | ✅ **ادعم ووصّل** | موصّل ويب هوك موحّد (`os_server/automation.py`) + جسر MCP (`os_mcp`) + قوالب JSON جاهزة | مجاني دون بطاقة (1,000 رصيد/شهر)، ونواته MIT، ويتكامل عبر HTTP/MCP بلا تبعية داخل النواة |
| **أتمتة** | **Windmill** (`Self-Host` / `Docker`) | ✅ **ادعم للمهام الخلفية** | استدعاء `/cela/v1` و`/runs` من سكربتات Python/TS المجدولة + استقبال/إرسال Webhooks | محرك ممتاز للمهام المجدولة (`os_evals`، فهرسة `os_know`)؛ **لا يستبدل** `code_exec.py` المعزول |
| **أتمتة** | **n8n** (`Self-Host Community`) | 🟡 **دعم خارجي فقط (عبر HTTP/MCP)** | عقدة HTTP Request / Webhook قياسية تخاطب `/api/webhooks/automation/*` و`/cela/v1/*` | ترخيصه `Sustainable Use` (تصنيف **D** غير حر) وحزمة `n8n` على PyPI فخ `GPLv3`؛ لا يُستورَد ولا يُستضاف في المستودع |
| **أتمتة** | **GitHub Actions** | ✅ **مُفعَّل — وسِّع للنشر** | توسيع `.github/workflows/` بمسارات فحص النشر ونشر التوثيق مع تثبيت الوجهات بأسرار | لدينا 6 مسارات عاملة فعلًا (`ci`, `web-ci`, `launcher-ci`, `eval-real-model`, `codeql`, `codacy`) |
| **نشر** | **Cloudflare Pages + Workers** | ✅ **الخيار الأول للواجهة والـBFF** | نشر `packages/web` + عامل `Cloudflare Worker` كوكيل BFF يحفظ `CELA_API_TOKEN` ويمرّر SSE | مجاني دون بطاقة (500 بناء/شهر، 100 ألف طلب Worker/يوم) ويحمي الرموز من الوصول للمتصفح |
| **نشر** | **Render** (`Free Web Service`) | 🟡 **مدعوم لتجربة `os_server` بشروط** | ملف `render.yaml` + فصل المخزن الدائم خارج القرص المؤقت + طابور استيقاظ للويب هوكس | يشغّل FastAPI مجانًا، لكن **نومه بعد 15 دقيقة** و**قرصه المؤقت** و**انتهاء Postgres بعد 30 يومًا** تتطلب مخزنًا خارجيًا |
| **نشر** | **GitHub Pages** | 🟡 **للتوثيق وتقارير التقييم فقط** | نشر `docs/` وتقارير `os_evals` الثابتة عبر GitHub Actions | يستضيف ملفات ثابتة فقط؛ **لا يصلح** لواجهة `packages/web` الحية لأنها تتطلب وكيلاً خادميًا يحمل الرموز السرية |
| **نشر** | **Netlify** | 🟡 **بديل ثانوي للواجهة** | وصفة نشر اختيارية عند الحاجة | حدّ 300 رصيد شهري قد يوقف البناء/الوظائف، ومسار التسجيل قد يطلب بطاقة بحسب الحساب والمنطقة |

---

## 1) أ) كيف تحقّقنا — وأوامر إعادة الإنتاج

كل حقيقة عن المستودعات والحزم في هذه الخطة صدرت عن فحص مباشر بتاريخ `2026-10-17`، ويمكن إعادة إنتاجها في 15 ثانية:

```bash
# 1) فحص المستودعات والتراخيص والنشاط عبر GitHub API
python3 - <<'PY'
import base64, json, urllib.request

for r in ["activepieces/activepieces", "n8n-io/n8n", "windmill-labs/windmill"]:
    req = urllib.request.Request(f"https://api.github.com/repos/{r}", headers={"User-Agent": "cela-verify"})
    d = json.load(urllib.request.urlopen(req, timeout=10))
    lic = d.get("license") or {}
    print(f"{d['full_name']}: stars={d['stargazers_count']} spdx={lic.get('spdx_id')} lang={d['language']} pushed={d['pushed_at'][:10]}")
PY

# 2) فحص وجود الحزم وتبعياتها ورخصها على PyPI
python3 - <<'PY'
import json, urllib.request
for pkg in ["wmill", "activepieces", "n8n"]:
    try:
        d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=10))["info"]
        print(f"PyPI {pkg:14} v{d['version']:<10} deps={len(d.get('requires_dist') or []):<3} license={d.get('license')}")
    except Exception as e:
        print(f"PyPI {pkg:14} -> {e}")
PY
```

**نتيجة الفحص المباشر (`2026-10-17`):**
* `activepieces/activepieces`: **25,083 ★** · TypeScript · `spdx=NOASSERTION` · آخر دفع `2026-10-17` · على PyPI: **`404 Not Found`**.
* `n8n-io/n8n`: **207,773 ★** · TypeScript · `spdx=NOASSERTION` · آخر دفع `2026-10-17` · على PyPI: `v0.11.0` برخصة **`GPLv3`** (حزمة قديمة غير رسمية).
* `windmill-labs/windmill`: **18,208 ★** · Rust · `spdx=NOASSERTION` · آخر دفع `2026-10-17` · على PyPI (`wmill`): `v1.839.0` · **تبعية واحدة فقط (`httpx`)** · رخصة **`Apache-2.0`**.

---

## 1) ب) الحقائق والتصحيحات الست — ما يجب الانتباه له قبل كتابة سطر كود واحد

| # | الافتراض الشائع | الحقيقة المُتحقَّقة من الكود والمصدر | الأثر المعماري على `cela_use_browser` |
|---|---|---|---|
| **1** | «Activepieces مفتوح المصدر بالكامل ويمكن تثبيته كمكتبة بايثون» | ملف `LICENSE` في `activepieces/activepieces` ينص صراحةً على أن `packages/ee/` و`packages/server/api/src/app/ee` خاضعة لرخصة تجارية خاصة (تصنيف **D** في `THIRD_PARTY_POLICY.md`)، بينما بقية الكود **MIT Expat** (تصنيف **B**). ولا توجد حزمة `activepieces` على PyPI أصلًا (`404`) | **لا استيراد بايثون ولا استضافة (vendoring) للشجرة.** التوصيل يتم عبر **HTTP Webhooks / REST** وجسر **`os_mcp`** فقط |
| **2** | «يمكن إضافة حزمة `n8n` من PyPI أو دمج كود n8n في المشروع» | مستودع `n8n` مرخّص بـ **Sustainable Use License** + رخصة Enterprise لملفات `.ee.` (تصنيف **D** مقيّد الحقوق — يمنع تقديمه كخدمة مستضافة للعملاء). والأخطر: حزمة `n8n` على PyPI (`v0.11.0`) حزمة طرف ثالث قديمة مرخّصة بـ **`GPLv3`** | **حظر تام** لـ `pip install n8n` أو نسخ أي ملف من `n8n` إلى المستودع. يُدعم `n8n` كـ **عميل خارجي** يخاطب نقاط نهاية HTTP/Webhook القياسية لدينا |
| **3** | «Windmill مفتوح المصدر ويمكن استخدامه بديلًا لصندوق تنفيذ الكود `code_exec.py`» | مستودع `windmill` مرخّص افتراضيًا بـ **AGPLv3** (تصنيف **C** copyleft قوي) مع أجزاء Enterprise، بينما عميل بايثون `wmill` على PyPI مرخّص بـ **Apache-2.0** وله تبعية واحدة (`httpx`). لكن نقل تنفيذ `python_execute` الخاص بالوكيل إلى Windmill خارجي **يتجاوز `setrlimit` و`unshare --net` وحلقة `Guardrails → RiskLevel → HITL`** | يبقى تنفيذ كود الوكيل داخل `os_tools/code_exec.py` (أو E2B/Firecracker وفق ADR-0005). يُستخدم Windmill **فوق** المنصة (لتشغيل المهام المجدولة، فهرسة `os_know`، تشغيل `os_evals`، والربط بين الخدمات) |
| **4** | «يمكن نشر واجهة `packages/web` كموقع ثابت (`static export`) على Cloudflare Pages أو GitHub Pages مباشرةً» | الواجهة الحالية (`packages/web/app/api/backend/[...path]/route.ts`) تعتمد على **وكيل خادمي (Node.js Route Handler)** يحقن `CELA_API_TOKEN` و`CELA_PRIVATE_API_TOKEN` من بيئة الخادم ويمرّر بث SSE (`duplex: "half"`). التصدير الثابت يلغي هذا المسار، ووضع الرموز في المتصفح (`NEXT_PUBLIC_*`) **يكسر القاعدة الأمنية الحاكمة** ([ADR-0005](../adr/0005-production-isolation-and-web.md)) | لنشر `packages/web` على Cloudflare Pages، **يجب** اقترانها بـ **Cloudflare Worker / Pages Function** يعمل كوكيل BFF يحتفظ بالرموز السرية في بيئة الـWorker ويمرّر الطلبات وبث SSE إلى الخادم الخلفي |
| **5** | «Cloudflare Workers يمكنه تشغيل خادم `os_server` (FastAPI) بالكامل» | خادم `os_server` يعتمد على عمليات فرعية (`subprocess` + `setrlimit` + `unshare --net` في `code_exec.py`)، وSQLite WAL (`storage.py`, `os_memory`, `os_know`, `waapi_inbox.py`)، ومحرك `LocalAIEngine` على CPU. بيئة Workers (V8 isolates) لا تدعم `fork/exec` ولا نظام ملفات POSIX محلي | يُستخدم **Cloudflare Workers** كـ **بوابة حافة (Edge API Gateway / BFF / Webhook Ingress & Queue)**، بينما يعمل `os_server` على بيئة Python حقيقية (Render للتجربة، أو حاوية/خادم خاص للإنتاج) |
| **6** | «Render المجاني كافٍ لتشغيل `os_server` مع حفظ السجل والذاكرة في `.cela/`» | خطة Render المجانية: (أ) **تنام بعد 15 دقيقة** وتستغرق ~60 ثانية للاستيقاظ (مما يُسقط طلبات Webhook السريعة من Activepieces/n8n)، (ب) **نظام ملفاتها مؤقت (ephemeral)** يمسح `.cela/ledger.jsonl` وSQLite عند كل إعادة تشغيل/نوم، (ج) **قاعدة PostgreSQL المجانية فيها تنتهي وتُحذف بعد 30 يومًا** | عند النشر على Render، يُمنع الاعتماد على `.cela/` المحلي للبيانات الدائمة؛ يُربط `RunStore` و`os_memory` و`os_know` بقاعدة خارجية دائمة (أو يُصدَّر السجل عبر OTLP — `os_server/otel.py`)، وتُمرَّر الويب هوكس عبر طابور إعادة محاولة (Activepieces / Worker) |

---

## 2) أين نحن اليوم — ما الموجود في المستودع وما الفجوة الدقيقة

قبل إضافة أي مكوّن جديد، نُثبّت ما يملكه المستودع اليوم بالفعل بالأدلة من الشجرة:

| القدرة | الموجود اليوم في المستودع | الفجوة الدقيقة لتوصيل الأدوات الثماني |
|---|---|---|
| **استقبال Webhooks وإرسال إشعارات** | `packages/os_server/os_server/waapi.py` + `waapi_inbox.py` + `waapi_outbox.py` (خاص بـ WhatsApp/WaAPI فقط، مع سر مسار 32+ حرفًا ومنع تكرار SQLite وتنقيح مسار ASGI) | لا يوجد **سطح ويب هوك عام للأتمتة** (`/api/webhooks/automation/...`) يقبل مهام من Activepieces / Windmill / n8n أو يرسل إليها إشعارات انتهاء التشغيل (`RunOutcome`) |
| **ربط أدوات خارجية بالوكيل** | `packages/os_mcp/` ([ADR-0017](../adr/0017-mcp-transport.md)): عميل `StdioMCPClient` و`HttpMCPClient` + خادم `MCPRegistryServer` + فحص `Guardrails` + `EgressPolicy` | نحتاج **ملفات إعداد ووصفات ربط MCP قياسية** لـ Activepieces وn8n وWindmill دون تعديل نواة `os_mcp` |
| **أتمتة CI/CD وفحص الأمان** | `.github/workflows/{ci,web-ci,launcher-ci,eval-real-model,codeql,codacy}.yml` + اختبار الحماية `tests/test_eval_workflow_security.py` | ينقصنا مسار نشر التوثيق/التقارير إلى `GitHub Pages` ومسار فحص جاهزية النشر (`deploy_doctor`) |
| **نشر الواجهة (`packages/web`)** | Next.js 15 مع وكيل Node.js في `app/api/backend/[...path]/route.ts` | ينقصنا **وكيل Cloudflare Worker / Pages Function** يحافظ على نفس عقد الأمان (عدم كشف الرموز للمتصفح + تمرير SSE `?after_seq=N`) عند النشر على Cloudflare Pages |
| **نشر الخادم (`os_server`)** | `docs/publish-runbook.md` يوثّق متغيرات Render وVercel | ينقصنا ملف **`render.yaml`** مضبوط بحواجز الأمان وفحص جاهزية يمنع تشغيل الخادم بقرص مؤقت دون علم المشغّل |

---

## 3) المعمارية المستهدفة: التركيبة الصفرية التكلفة (Zero-Card Stack 2026)

بدل الاعتماد على منصة واحدة أو كسر عزل النواة، تُوزَّع الأدوار بحيث تعمل كل أداة في الطبقة التي تتقنها، مع بقاء **حواجز الأمان (`SSRFGuard` + `EgressPolicy` + `Guardrails` + `HITL`)** داخل `os_server`:

```text
┌───────────────────────────────────────────────────────────────────────────────────┐
│ 1. التطوير والتكامل المستمر (GitHub + GitHub Actions — مجاني بلا بطاقة)          │
│    • ci.yml (Python 3.11-3.13: ruff + pytest 1069+ offline + smoke tests)         │
│    • web-ci.yml (Next.js 15 lint + typecheck + tests + build)                     │
│    • codeql.yml + codacy.yml + eval-real-model.yml (destination-pinned)           │
│    • [جديد] docs-pages.yml ──► ينشر التوثيق وتقارير os_evals على GitHub Pages      │
└───────────────┬───────────────────────────────────────────────┬───────────────────┘
                │                                               │
                ▼                                               ▼
┌───────────────────────────────────────────┐   ┌───────────────────────────────────┐
│ 2. الواجهة وحافة الشبكة                   │   │ 4. محركات الأتمتة والجدولة        │
│    (Cloudflare Pages + Workers — مجاني)   │   │    • Activepieces Cloud (سحابي)   │
│                                           │   │    • Windmill (استضافة ذاتية)     │
│  [المتصفح: packages/web (React/Next)]     │   │    • n8n Community (ذاتي فقط)     │
│       │ (لا يرى أي توكن أبدًا)            │   │         │               ▲         │
│       ▼                                   │   │         │ Webhook       │ Callback│
│  [Cloudflare Worker BFF Proxy]            │   │         │ (بدء مهمة)    │ (النتيجة)│
│   • يحقن CELA_API_TOKEN من أسرار Worker   │   └─────────┼───────────────┼─────────┘
│   • يمرّر SSE (?after_seq=N) بلا تخزين    │             │               │
│   • يمتص تباطؤ استيقاظ الخادم الخلفي       │             ▼               │
└───────────────┬───────────────────────────┘   ┌─────────────────────────┴─────────┐
                │ HTTPS + Bearer Token          │ 3. الخادم الخلفي (os_server)      │
                └──────────────────────────────►│    (Render Free للتجربة / VPS)    │
                                                │                                   │
                                                │  • /runs, /ws, /cela/v1 (FastAPI) │
                                                │  • /api/webhooks/automation/*     │
                                                │  • os_mcp (Streamable HTTP/stdio) │
                                                │  • ReAct + PlanningFlow + HITL    │
                                                │  • Sandbox (rlimits + netns)      │
                                                │  • SSRFGuard + EgressPolicy       │
                                                └─────────────────┬─────────────────┘
                                                                  │
                                                                  ▼
                                                ┌───────────────────────────────────┐
                                                │ 5. التخزين الدائم والمراقبة       │
                                                │    (خارج القرص المؤقت لـ Render)  │
                                                │  • RunStore / Memory / Know DB    │
                                                │  • تصدير OTLP (os_server/otel.py) │
                                                └───────────────────────────────────┘
```

---

## 4) تصميم موصّل الأتمتة الموحّد (`os_server/automation.py`)

بدل كتابة كود مخصّص لكل أداة (واحد لـ Activepieces وآخر لـ Windmill وثالث لـ n8n)، نبني **موصّل أتمتة موحّدًا** في `packages/os_server/os_server/automation.py` يتبع نفس الانضباط الهندسي الصارم الموجود في `waapi.py`، ويعمل **بلا أي تبعية جديدة** (`pydantic` + `httpx` فقط):

### 4.1 المسار الوارد (Inbound Webhook): تشغيل الوكيل من Activepieces / Windmill / n8n

* **المسار:** `POST /api/webhooks/automation/{webhook_secret}`
* **التفعيل الصريح (Fail-Closed):** مغلق افتراضيًا (`CELA_AUTOMATION_ENABLE=0`). لا يعمل إلا عند ضبط `CELA_AUTOMATION_ENABLE=1` وتعيين `CELA_AUTOMATION_WEBHOOK_SECRET` (سر عشوائي مستقل بطول 32 حرفًا على الأقل، مختلف عن `CELA_API_TOKEN` وعن `CELA_PRIVATE_API_TOKEN`).
* **المصادقة المزدوجة وتنقيح السجلات:**
  1. مقارنة سر المسار في زمن ثابت (`secrets.compare_digest`).
  2. دعم اختياري لتوقيع HMAC-SHA256 في الترويسة (`X-Automation-Signature: sha256=<hex>`) باستخدام `CELA_AUTOMATION_SIGNING_SECRET` (يدعمه كل من Activepieces وWindmill وn8n).
  3. تنقيح مسار الطلب في `ASGI scope` فور التوجيه (كما في `waapi.py`) حتى لا يظهر سر المسار في سجلات الوصول أبدًا.
* **منع التكرار (Idempotency) — حاسم لمعالجة نوم Render:**
  * عندما ينام خادم Render المجاني ويتأخر في الرد، قد تعيد Activepieces أو n8n إرسال الويب هوك.
  * يحمل الطلب حقل `idempotency_key` (أو `event_id`). يُحفظ المفتاح مع بصمة الحمولة (`sha256`)؛ فإذا تكرر نفس المفتاح بنفس الحمولة يعيد الخادم **نفس `run_id`** القائم (`200`/`202`) دون إطلاق تشغيل ثانٍ يستهلك ميزانية النموذج، وإذا أُعيد استخدام المفتاح بحمولة مختلفة يُرفض بـ `409 Conflict`.
* **الحواجز قبل التشغيل (`Guardrails` + `EgressPolicy`):**
  * يمرّ حقل `task` على `Guardrails.scan_text` قبل إنشاء المهمة.
  * إذا تضمّن الطلب عنوان `callback_url` لإرسال النتيجة عند الانتهاء، يُفحص العنوان **مسبقًا (Pre-flight)** عبر `parse_http_target` + `SSRFGuard` + `EgressPolicy` (قائمة سماح `CELA_AUTOMATION_ALLOW_HOSTS`) **قبل** قبول المهمة؛ فإن كان العنوان داخليًا (`127.0.0.1`, `169.254.169.254`) أو غير مصرح به، يُرفض الطلب فورًا بـ `403` ولا يُنشأ تشغيل.
* **عقد الحمولة والاستجابة:**

```json
// الطلب: POST /api/webhooks/automation/<CELA_AUTOMATION_WEBHOOK_SECRET>
{
  "idempotency_key": "ap_flow_9812_step_1",
  "source": "activepieces",
  "task": "لخّص آخر تغييرات المستودع واستخرج نقاط المخاطر",
  "mode": "auto",
  "max_steps": 15,
  "wait": false,
  "callback_url": "https://cloud.activepieces.com/api/v1/webhooks/..."
}

// الاستجابة الفورية (202 Accepted عند wait=false — تتفادى مهلة 5-15 ثانية في منصات الأتمتة):
{
  "accepted": true,
  "deduplicated": false,
  "run_id": "run_7f9a2c...",
  "status": "running",
  "status_url": "/runs/run_7f9a2c...",
  "outcome_url": "/cela/v1/runs/run_7f9a2c.../outcome"
}
```

### 4.2 المسار الصادر (Outbound Callback & Tool): إشعار منصات الأتمتة بالنتيجة

1. **إشعار انتهاء التشغيل التلقائي (`Callback on Run Completion`):**
   * عند انتهاء التشغيل المسجَّل بـ `callback_url`، يرسل عامل الإرسال الخلفي نتيجة التشغيل (`run_id`, `outcome` من `RunLedger`، `final_answer`، وملخص `artifacts`) بعد تمريرها على `os_core.redact.scrub_credentials` (فلا تتسرّب أي مفاتيح API إلى Activepieces أو n8n).
   * يُوقَّع الجسم بترويسة `X-Cela-Signature: sha256=<hmac>` لتمكين Activepieces/Windmill من التحقق من مصدر النتيجة.
2. **أداة `automation_trigger` في `ToolRegistry`:**
   * تتيح للوكيل نفسه (أثناء `ReAct` أو `PlanningFlow`) استدعاء مسار عمل في Activepieces أو مهمة في Windmill، مصنَّفة تحت `RiskLevel.NETWORK` (أو `DANGEROUS` حسب الإعداد) وخاضعة لـ `EgressPolicy` وسقف يومي للطلبات (`CELA_AUTOMATION_DAILY_LIMIT` لحماية رصيد الـ 1,000 عملية الشهرية في Activepieces).

---

## 5) خطة توصيل ودعم كل أداة بالتفصيل

### 5.1 Activepieces — الأتمتة السحابية والاستضافة الذاتية (الخيار الأول)

* **لماذا هو الخيار الأول:** يعطي 1,000 رصيد شهريًا مجانًا دون بطاقة ائتمان، ومسارات غير محدودة، ويدعم Webhooks وMCP، ونواته المفتوحة مرخّصة بـ **MIT Expat**.
* **مسارات التوصيل الثلاثة مع منصتنا:**
  1. **Activepieces ──(Webhook)──► `os_server`:** خطوة `HTTP Request` في Activepieces تستدعي `POST /api/webhooks/automation/{secret}` مع `wait=false` و`callback_url` يشير إلى Webhook استئناف في Activepieces.
  2. **`os_server` ──(Webhook)──► Activepieces:** عند اكتمال مهمة الوكيل، يرسل `os_server` النتيجة المنقّاة إلى رابط Webhook الخاص بـ Activepieces لإكمال المسار (مثل إرسال بريد، تحديث جدول، أو إرسال إشعار).
  3. **ربط MCP المباشر (`os_mcp` — ADR-0017):** يمكن لوكلاء Activepieces استهلاك أدوات `cela_use_browser` عبر خادم MCP الخاص بنا، أو تسجيل Webhook مسار Activepieces كأداة HTTP محكومة بـ `EgressPolicy`.
* **حارس استهلاك الرصيد (Credit Budget Guard):**
  * لأن الخطة المجانية تتوقف عند استهلاك **1,000 رصيد شهريًا** (~33 عملية يوميًا)، يضيف `AutomationConfig` متغير `CELA_AUTOMATION_MAX_DAILY_OUTBOUND` (الافتراضي `30`)؛ عند بلوغه يرفض الإرسال الصادر محليًا برسالة واضحة بدل استنزاف الرصيد الشهري في حلقة خطأ واحدة.

### 5.2 Windmill — تشغيل السكربتات والمهام المجدولة (Docker / استضافة ذاتية)

* **متى يُستخدم:** عندما نحتاج إلى تشغيل مهام خلفية مجدولة أو طويلة الأمد دون حدود أرصدة سحابية، مثل:
  * تشغيل التقييم المعياري الدوري (`python -m os_evals operations --out report.json`).
  * إعادة فهرسة المستندات في `os_know` (`python -m os_server.cli know index ...`).
  * تصدير وإرسال بيانات المراقبة (`python -m os_server.cli telemetry export --send`).
* **قاعدة الفصل المعماري (مهم جدًا):**
  * **لا يُستبدل** `os_tools/code_exec.py` بـ Windmill لتنفيذ الأكواد غير الموثوقة التي يولّدها النموذج أثناء المحادثة، لأن `code_exec.py` يفرض `setrlimit` و`unshare --net` ويمرّ على `Guardrails` و`HITL`.
  * بدلًا من ذلك، يعمل Windmill **كمنسّق مهام خلفية (Orchestrator/Scheduler)** يستدعي واجهة `/cela/v1/browse` أو `/runs` باستخدام الرمز المحفوظ في `Windmill Variables/Resources`.
* **الترخيص والتبعيات:**
  * لا ننسخ أي كود من مستودع Windmill (`AGPLv3`) إلى مستودعنا (`MIT`).
  * إذا احتجنا لاحقًا إلى استدعاء Windmill API برمجيًا، فإن التواصل يتم عبر `httpx` المباشر في `os_server/automation.py` (أو حزمة `wmill` المرخّصة بـ `Apache-2.0` ذات التبعية الواحدة `httpx` داخل extra اختياري `[windmill]` فقط).

### 5.3 n8n — الأتمتة المتقدمة عند الاستضافة الذاتية

* **ضوابط الترخيص الصارمة (`Sustainable Use License` — التصنيف D في `THIRD_PARTY_POLICY.md`):**
  1. **يُحظر** تضمين n8n داخل منتجنا أو تقديمه كخدمة مُدارة للعملاء النهائيين.
  2. **يُحظر** إضافة حزمة `n8n` من PyPI إلى `pyproject.toml` (لأنها حزمة غير رسمية برخصة `GPLv3`).
* **كيف ندعمه بأمان للمستخدم الذي يشغّله ذاتيًا:**
  * نوفر **قالب مسار عمل قياسي (`docs/automation-templates/n8n-cela-workflow.json`)** مكتوبًا من الصفر من قِبلنا (تصنيف **A**)، يستخدم عقدتين قياسيتين فقط في n8n:
    1. `HTTP Request Node` لإرسال المهمة إلى `POST /api/webhooks/automation/{secret}` أو `POST /cela/v1/browse`.
    2. `Webhook Node` لاستلام النتيجة النهائية من `callback_url`.

### 5.4 GitHub Actions — التطوير والفحص والنشر التلقائي

* **ما يعمل اليوم في المستودع (6 مسارات):**
  * `ci.yml`: فحص `ruff` + تشغيل `pytest -q` (1069+ اختبارًا بلا شبكة) + فحص حي لـ `ReAct` و`RunLedger` و`os_memory` و`os_know` و`telemetry` على Python 3.11 / 3.12 / 3.13.
  * `web-ci.yml`: فحص وبناء واجهة Next.js 15 (`lint`, `typecheck`, `test:components`, `test:hooks`, `build`).
  * `launcher-ci.yml`: فحص وبناء شجرة Rust المضافة والتحقق من بصمتها (`craft-launcher.sha256`).
  * `eval-real-model.yml`: تقييم النماذج الحقيقية المحمي بـ `ALLOW_LIVE_EXTERNAL_CALLS` وتثبيت الوجهة في `secrets.CELA_MODEL_BASE_URL` (`tests/test_eval_workflow_security.py`).
  * `codeql.yml` و`codacy.yml`: فحص الأمان الثابت.
* **ما نضيفه في هذه الخطة (مع الحفاظ على حصة الـ 2,000 دقيقة للمستودعات الخاصة):**
  1. **`docs-pages.yml`:** نشر التوثيق (`docs/`) وتقارير `os_evals` الثابتة إلى **GitHub Pages** عند التحديث على `main` فقط (مع فلتر مسارات `paths` لتجنّب استهلاك الدقائق في كل تعديل كود).
  2. **`deploy-check.yml`:** خطوة فحص إعدادات النشر (`deploy_doctor`) قبل استدعاء Deploy Hook الخاص بـ Render أو بناء Cloudflare Pages، مع تطبيق نفس قاعدة `test_eval_workflow_security.py`: **لا عنوان URL حر في `workflow_dispatch` أبدًا**؛ جميع عناوين النشر تأتي من `GitHub Secrets`.

---

## 6) خطة دعم منصات النشر والاستضافة المجانية (الأربع)

### 6.1 Cloudflare Pages + Cloudflare Workers (الواجهة الأمامية + بوابة الحافة)

**لماذا هي الأنسب لمشروعنا:**
* مجانية دون بطاقة ائتمان، وتعطي **500 عملية بناء شهريًا** على Pages و**100,000 طلب يوميًا** على Workers.
* **كيف نحلّ تعارض `packages/web` مع الاستضافة الثابتة:**
  * تحتوي `packages/web` على المسار الخادمي `app/api/backend/[...path]/route.ts` الذي يخفي `CELA_API_TOKEN` و`CELA_PRIVATE_API_TOKEN` عن المتصفح.
  * نضيف عامل حافة خفيفًا (`packages/web/functions/api/backend/[[path]].ts` متوافقًا مع Cloudflare Pages Functions / Workers) يقوم بالدور ذاته تمامًا:
    1. يقرأ `CELA_API_TOKEN` و`CELA_PRIVATE_API_TOKEN` و`CELA_BACKEND_URL` من **Cloudflare Worker Secrets** (لا تصل إلى المتصفح أبدًا).
    2. يوجّه الطلبات `/api/backend/*` إلى خادم `os_server` مع ترويسة `Authorization: Bearer ...`.
    3. يمرّر تدفق SSE (`GET /runs/{id}/events?after_seq=N`) مباشرةً (`Cache-Control: no-store, no-transform`) دون تخزين مؤقت يكسر البث اللحظي.
    4. عند نوم خادم Render الخلفي (تأخر الرد أثناء الاستيقاظ)، يعيد رسالة حالة مهذّبة قابلة لإعادة المحاولة بدل خطأ 502 خام.

### 6.2 Render — استضافة خادم `os_server` (FastAPI) للتجربة

**كيف نُعدّ `os_server` للعمل على Render بأمان رغم قيوده الثلاثة:**

| قيد Render المجاني | الخطر على `cela_use_browser` | الحل الهندسي المعتمد في الخطة |
|---|---|---|
| **النوم بعد 15 دقيقة من عدم النشاط** (~60 ثانية للاستيقاظ) | انقطاع بث SSE، أو انتهاء مهلة Webhook قادم من Activepieces/n8n | 1) استئناف البث تلقائيًا عبر `?after_seq=N` ([ADR-0002](../adr/0002-event-bus-state-sync.md)).<br>2) المسار `/api/webhooks/automation/{secret}` يعمل بـ `wait=false` ويعيد `202 Accepted` فورًا، مع `idempotency_key` يمنع تكرار التشغيل عند إعادة المحاولة |
| **نظام الملفات مؤقت (Ephemeral Disk)** | ضياع `.cela/ledger.jsonl` وقاعدة SQLite المحلية عند كل نوم أو إعادة نشر | 1) ضبط `CELA_RUN_STORE_URL` و`CELA_MEMORY_STORE_URL` و`CELA_KNOW_STORE_URL` على قاعدة خارجية دائمة، أو تفعيل تصدير `OTLP` (`CELA_OTEL_ENDPOINT`).<br>2) إضافة فحص إقلاع (`startup preflight`) في `os_server` يطبع تحذيرًا صريحًا إذا شُغِّل على Render (`RENDER=true`) مع مخزن محلي مؤقت (`memory://` أو `sqlite:///`) |
| **انتهاء PostgreSQL المجانية بعد 30 يومًا** | فقدان كامل لبيانات التشغيلات والذاكرة بعد شهر واحد | عدم استخدام قاعدة Render المجانية للتخزين الدائم؛ ربط `PostgresRunStore` بقاعدة مجانية دائمة لا تنتهي بعد 30 يومًا (مثل Supabase أو Neon في الخطة المجانية دون بطاقة، أو خادم ذاتي) |

**مواصفات ملف `render.yaml` المخطط إضافته:**
* أمر البناء: `pip install -e ".[server,distributed]"`
* أمر التشغيل: `python -m os_server.cli serve --host 0.0.0.0 --port $PORT`
* مسار فحص الصحة: `GET /health`
* المتغيرات الإلزامية (`sync: false`): `CELA_API_TOKEN`، `CELA_PRIVATE_API_TOKEN` (مختلفان وجوبًا)، `CELA_RUN_STORE_URL`، و`CELA_AUTOMATION_WEBHOOK_SECRET`.

### 6.3 GitHub Pages — للتوثيق وتقارير التقييم الثابتة فقط

* **الاستخدام الصحيح:** استضافة وثائق المعمارية (`docs/`)، ولوحة نتائج التقييم المعياري الثابتة (`os_evals` JSON/HTML — البوابة 2 في `docs/ROADMAP.md`)، وكتالوج `craft-launcher` للقراءة فقط.
* **الحدّ الأمني الصريح:** **لا تُنشر** واجهة العمل التفاعلية الكاملة (`packages/web`) على GitHub Pages كواجهة متصلة مباشرةً بـ `os_server` بدون وسيط Worker، لأن GitHub Pages لا يملك بيئة خادم لإخفاء `CELA_API_TOKEN` عن المتصفح.

### 6.4 Netlify — البديل الثانوي للواجهة

* يُوثَّق كبديل احتياطي لـ Cloudflare Pages فقط، مع تنبيه صريح في دليل النشر إلى:
  1. سقف **300 رصيد شهريًا** الذي قد يوقف عمليات البناء والوظائف الخادمية عند تجاوزه.
  2. ضرورة التحقق من عدم طلب بطاقة ائتمان أثناء التسجيل قبل اعتماده.

---

## 7) مصفوفة الامتثال لسياسة الطرف الثالث (`docs/THIRD_PARTY_POLICY.md`)

طبيقًا للقواعد **T-1** إلى **T-7** في [`docs/THIRD_PARTY_POLICY.md`](THIRD_PARTY_POLICY.md) و[ADR-0018](../adr/0018-third-party-ip-governance.md):

| الأداة / المادة | المالك والمصدر | الرخصة الفعلية | التصنيف | القرار القانوني والهندسي |
|---|---|---|---|---|
| كود موصّل الأتمتة (`os_server/automation.py`) وقوالبنا | كودنا نحن (`open_source-`) | **MIT** | **A** | يُضاف مباشرةً في المستودع مع اختبارات حتمية |
| عميل Windmill (`wmill` على PyPI) | Windmill Labs (`windmill-labs/windmill`) | **Apache-2.0** (للعميل `wmill` فقط) | **B** | لا حاجة لإضافته في النواة (`httpx` يكفي)؛ إن أُضيف مستقبلًا يكون في extra `[windmill]` مع إشعار في `THIRD-PARTY-NOTICES.md` |
| نواة Activepieces (`activepieces/activepieces`) | Activepieces Inc. | **MIT Expat** (ما عدا `ee/` فهو تجاري) | **B / D** | **لا يُستضاف (No Vendoring)**؛ التوصيل عبر الشبكة (HTTP/MCP) فقط |
| خادم Windmill الكامل | Windmill Labs | **AGPLv3** + Enterprise | **C / D** | **لا يُنسخ منه أي ملف** إلى المستودع؛ يُشغَّل كخدمة خارجية عبر Docker |
| منصة n8n (`n8n-io/n8n`) | n8n GmbH | **Sustainable Use License** + Enterprise | **D** | **غير حر**؛ يُمنع نسخه أو تضمينه أو إعادة بيعه؛ يُدعم كعميل HTTP خارجي فقط |
| حزمة `n8n` على PyPI (`v0.11.0`) | طرف ثالث غير رسمي | **GPLv3** | **C (مرفوض)** | **فخ ترخيصي**؛ يُمنع تثبيته أو إضافته في `pyproject.toml` |
| شعارات وعلامات المنصات الثماني | ملاكها الأصليون | علامات تجارية محفوظة | **D** | **لا ننسخ أي شعار PNG/SVG** لهذه المنصات داخل المستودع؛ نكتفي بأسمائها النصية في الواجهة والتوثيق (القاعدة **T-7**) |

---

## 8) مراحل التنفيذ ومعايير الخروج (Roadmap & Exit Criteria)

كل مرحلة مستقلة، تحافظ على عهد النواة (**صفر تبعيات إلزامية جديدة، وصفر شبكة أو مفاتيح في `pytest -q`**):

| المرحلة | النطاق | الملفات المستهدفة | معيار الخروج (دليل قابل للقياس في CI) |
|---|---|---|---|
| **0 — الاعتماد (الحالية)** | توثيق الخطة وفحص التراخيص وتحديد الاستخدام الأساسي | `docs/AUTOMATION_AND_DEPLOYMENT_PLAN.md` | مراجعة المالك واختيار مسار البدء (القسم 9)؛ لا تغيير في الكود التنفيذي |
| **1 — موصّل الأتمتة الموحّد (`os_server/automation.py`)** | استقبال Webhooks من Activepieces/Windmill/n8n (`POST /api/webhooks/automation/{secret}`) + منع التكرار `idempotency_key` + فحص `Guardrails` و`EgressPolicy` المسبق لـ `callback_url` + إرسال النتيجة الموقّعة والمنقّاة (`scrub_credentials`) + أداة `automation_trigger` + سقف الرصيد اليومي | `packages/os_server/os_server/automation.py`, `packages/os_server/os_server/app.py`, `.env.example`, `tests/test_automation.py` | **+35 اختبارًا حتميًا دون شبكة (`MockTransport`)** تغطي: سر المسار الخاطئ (`401`/`404`)، تكرار `idempotency_key` بنفس الحمولة (`202` بلا تشغيل مزدوج) وبحمولة مختلفة (`409`)، رفض `callback_url` داخلي (`169.254.169.254` → `403` قبل بدء التشغيل)، تنقيح مسار ASGI، وحجب الأسرار في الـ callback |
| **2 — حزمة النشر الصفرية (Cloudflare + Render + Doctor)** | وكيل حافة Cloudflare Pages Function (`packages/web/functions/api/backend/[[path]].ts`) لحماية الرموز وتمرير SSE + ملف `render.yaml` + فحص التحذير من القرص المؤقت عند الإقلاع + أداة `scripts/deploy_doctor.py` | `render.yaml`, `packages/web/functions/api/backend/[[path]].ts`, `scripts/deploy_doctor.py`, `tests/test_deploy_config.py` | اختبار يتحقق من أن `render.yaml` لا يحتوي على أسرار مضمّنة، وأن وكيل Cloudflare لا يسرّب `CELA_API_TOKEN` للمتصفح ويمرّر `after_seq`، وأن `deploy_doctor.py` يكتشف غياب المتغيرات أو تطابق الرمزين |
| **3 — قوالب التكامل الجاهزة + جسر MCP** | قوالب JSON قياسية جاهزة للاستيراد في Activepieces وWindmill وn8n + مثال إعداد `mcp.json` لربطها عبر `os_mcp` + دليل تشغيل خطوة بخطوة | `docs/automation-templates/{activepieces-flow,windmill-script,n8n-workflow}.json`, `docs/AUTOMATION_GUIDE_AR.md` | اختبار يتحقق من سلامة بنية JSON للقوالب وخلوّها من أي مفاتيح حقيقية أو عناوين غير آمنة |
| **4 — النشر التلقائي للتوثيق والتقارير (`GitHub Pages`)** | مسار GitHub Actions ينشر `docs/` وتقارير `os_evals` الثابتة إلى GitHub Pages مع حماية مسارات التنفيذ | `.github/workflows/docs-pages.yml`, `tests/test_eval_workflow_security.py` | نجاح فحص YAML وأذونات `permissions: contents: read, pages: write, id-token: write` فقط دون وصول لأسرار النماذج |

---

## 9) الخطوة التالية — قرارات المالك لتحديد نقطة الانطلاق

بناءً على السؤال الختامي في وثيقتك (**«لتحديد أفضل مجموعة تناسب مشروعك، اختر استخدامك الأساسي»**)، هذه هي التوصية الهندسية المباشرة لمنصة `cela_use_browser` مع الخيارات المتاحة للبدء بالتنفيذ الفوري:

| # | القرار | الخيارات | التوصية الهندسية لمنصتنا |
|---|---|---|---|
| **1** | **الاستخدام الأساسي الأول (نقطة البدء)** | **(أ) أتمتة المهام والويب هوكس** (ربط Activepieces / Windmill / n8n بـ `os_server` عبر المرحلة 1)<br>**(ب) نشر الواجهة والخادم مجانًا** (Cloudflare Pages + Worker BFF + Render عبر المرحلة 2)<br>**(ج) المساران معًا بالتوازي** (المرحلتان 1 + 2 في نفس الجولة) | **(ج) المساران معًا** — لأن موصّل الأتمتة (`automation.py`) وملفات النشر (`render.yaml` + وكيل Cloudflare) يكمل أحدهما الآخر (معالجة نوم Render عبر `idempotency_key` و`wait=false`) |
| **2** | **محرك الأتمتة المفضّل للبدء** | **(أ) Activepieces Cloud** (سحابي جاهز بلا خادم، 1,000 رصيد/شهر)<br>**(ب) Windmill Self-Hosted** (مفتوح المصدر عبر Docker، بلا حد أرصدة، لتشغيل Python/TS)<br>**(ج) دعم الثلاثة بعقد موحّد** (`Activepieces` + `Windmill` + `n8n`) | **(ج) عقد موحّد في الكود (`automation.py`)** مع البدء بـ **Activepieces** للأتمتة السحابية السريعة و**Windmill** للمهام الخلفية الثقيلة |
| **3** | **التخزين الدائم عند النشر على Render** | **(أ) قاعدة خارجية مجانية دائمة** (`PostgresRunStore` على Supabase/Neon بلا بطاقة) لتفادي مسح القرص المؤقت وانتهاء الـ 30 يومًا<br>**(ب) تصدير OTLP فقط** (`os_server/otel.py`) مع قبول مسح الحالة المحلية عند نوم الخادم | **(أ) قاعدة خارجية دائمة** للإنتاج التجريبي المستمر، مع إبقاء **(ب)** خيارًا سريعًا للتجارب القصيرة |
