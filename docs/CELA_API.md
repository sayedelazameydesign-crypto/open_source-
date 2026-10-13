# `cela_use_browser` — خطة وعقد الـ API الخاص وواجهة Python

توثّق هذه الوثيقة تصميم وعقد **`cela_use_browser`**: كيف أُضيفت هوية المنتج والـAPI
الخاص (`/cela/v1`) وواجهة Python المباشرة (`CelaBrowser`) فوق المنصة القائمة **بلا كسر**
لأي حزمة مستوردة أو مسار قائم.

---

## 1. المبدأ الذي حكم التسمية: فصل الهوية عن الواجهة

| الطبقة | قبل | بعد | أثر الكسر |
|---|---|---|---|
| اسم التوزيع (`pyproject.toml`) | `open-source-platform` | `cela-use-browser` | صفر |
| عنوان FastAPI (`os_server/app.py`) | `open-source autonomous platform` | `cela_use_browser private + platform API` | صفر |
| متغيّر رمز المنصة | `OS_API_TOKEN` | `CELA_API_TOKEN` **مع استمرار `OS_API_TOKEN`** | صفر |
| متغيّر الرمز الخاص | — | `CELA_PRIVATE_API_TOKEN` | صفر |
| حزم الاستيراد | `os_core`, `os_agents`, `os_tools`, `os_orchestration`, `os_server` | **كما هي** | صفر |
| مسارات المنصة العامة | `/runs`, `/ws/{id}`, `/runs/{id}/ws-ticket`, … | **كما هي** | صفر |

يحرس ذلك الاختبار الصريح:
`test_the_rename_did_not_move_the_importable_packages`.

---

## 2. بنية اللحام: `ApiRuntime` ومنطق واحد لبابين

كانت حالة الخادم (`runs`, `states`, `artifacts`, `tickets`) حبيسة closure داخل
`create_app`. جرى استخراجها إلى `os_server/runtime.py` (`ApiRuntime`)، فأصبح لدينا
تنفيذ واحد تشترك فيه ثلاثة أسطح:

1. **مسارات المنصة (`/runs`, `/ws/{id}`, …)** — محمية بـ `CELA_API_TOKEN` (أو `OS_API_TOKEN`).
2. **المسار الخاص (`/cela/v1/…`)** — محمي بـ `CELA_PRIVATE_API_TOKEN` عبر تبعية على مستوى `APIRouter`.
3. **واجهة Python (`CelaBrowser`)** — تستدعي منطق `ApiRuntime` نفسه داخل العملية دون HTTP.

---

## 3. الفصل الصارم بين الرمزين

لا يقبل أي سطح رمز السطح الآخر، ويرفض `create_app` ضبط الرمزين على القيمة نفسها:

```text
GET /runs/run_x      + رمز المنصة (CELA_API_TOKEN)         → 404 (مصادق، التشغيل غير موجود)
GET /runs/run_x      + الرمز الخاص (CELA_PRIVATE_API_TOKEN) → 401
GET /cela/v1/whoami  + رمز المنصة (CELA_API_TOKEN)         → 401
GET /cela/v1/whoami  + الرمز الخاص (CELA_PRIVATE_API_TOKEN) → 200 {"product": "cela_use_browser", ...}
```

إذا لم يُضبط `CELA_PRIVATE_API_TOKEN`، تفشل جميع مسارات `/cela/v1/*` مغلقةً بـ **503**.

---

## 4. فحص الوجهة قبل بدء أي تشغيل (Pre-flight Egress Enforcement)

قبل إنشاء أي مهمة في `CelaBrowser.browse()` أو `POST /cela/v1/browse`:

1. **التحقق النحوي (`parse_http_target`)**: يجب أن يكون العنوان `http` أو `https` مع مضيف ومنفذ صالحين:
   * `'ftp://example.org'` ← يُرفض بـ `'ftp://example.org' is not an http(s) URL with a host` (422 عبر HTTP، `ValueError` عبر Python).
2. **حدّ الشبكة غير القابل للتجاوز (`SSRFGuard`)**:
   * `'http://169.254.169.254/latest/meta-data/'` ← يُرفض بـ `egress denied: network boundary: 169.254.169.254 is a cloud metadata endpoint` (403 عبر HTTP، `EgressPolicyError` عبر Python).
3. **سياسة السماح الصريحة (`EgressPolicy`)**:
   * `'https://evil.net'` ← يُرفض بـ `egress denied: no allow rule permits https://evil.net:443 for 'browser'` (403 عبر HTTP، `EgressPolicyError` عبر Python).

وعند الرفض **لا يُنشأ أي تشغيل** في `runtime.runs`.

---

## 5. جدول مسارات `/cela/v1`

كل المسارات أدناه تتطلب الترويسة `Authorization: Bearer <CELA_PRIVATE_API_TOKEN>`:

| الطريقة والمسار | الوظيفة | الاستجابة |
|---|---|---|
| `GET /cela/v1/whoami` | هوية المنتج والأدوات والنطاقات المسموحة | `200 {"product": "cela_use_browser", ...}` |
| `POST /cela/v1/egress/check` | فحص عنوان URL مقابل سياسة الخروج دون بدء تشغيل | `200 EgressVerdict` أو `422` لعنوان غير صالح |
| `POST /cela/v1/browse` | فحص الوجهة ثم بدء مهمة تصفح (`wait=false` أو `wait=true`) | `202` (غير متزامن) / `200` (مع `wait=true`) / `403` عند رفض السياسة / `504` عند انتهاء مهلة الانتظار |
| `GET /cela/v1/runs` | قائمة التشغيلات الحالية والمنتهية | `200 {"runs": [...]}` |
| `GET /cela/v1/runs/{run_id}` | لقطة حالة التشغيل (`snapshot`) | `200` أو `404` |
| `POST /cela/v1/runs/{run_id}/wait` | انتظار اكتمال التشغيل (محمي بـ `asyncio.shield`) | `200` أو `404` أو `504` |
| `GET /cela/v1/runs/{run_id}/events` | بث SSE لأحداث التشغيل | `200 text/event-stream` أو `404` |
| `POST /cela/v1/runs/{run_id}/approve` | إجابة طلب موافقة بشرية (HITL) | `200 {"delivered": bool}` أو `404` / `409` / `422` |
| `GET /cela/v1/runs/{run_id}/artifacts` | قائمة المخرجات (بما فيها لقطات الشاشة) | `200 {"run_id": ..., "artifacts": [...]}` |
| `GET /cela/v1/runs/{run_id}/artifacts/{filename}` | جلب مخرج معيّن بصيغة JSON | `200` أو `404` |
| `GET /cela/v1/runs/{run_id}/outcome` | سطر السجل المسجَّل لتشغيل واحد | `200 LedgerEntry` أو `404` |
| `GET /cela/v1/ledger?limit=N` | سجل نتائج التشغيلات: آخر `N` مدخل + الملخّص | `200` (`limit` بين 1 و500، وإلا `422`) |
| `GET /cela/v1/ai/capabilities` | حالة محرك الذكاء المحلي (`local-cpu`) والمكتبات الاختيارية | `200` |
| `POST /cela/v1/nlp/entities` | استخراج الكيانات والكلمات المفتاحية (عربي/إنجليزي) | `200` |
| `POST /cela/v1/search/index` | فهرسة المستندات داخل مساحة أسماء (`namespace`) | `200` |
| `POST /cela/v1/search/semantic` | بحث دلالي هجين عابر للغتين (عربي/إنجليزي) | `200` |
| `POST /cela/v1/ml/analyze` | تحليل تعلّم الآلة (`classify`, `forecast`, `cluster`, `anomaly`) | `200` أو `422` |
| `POST /cela/v1/vision/analyze` | فحص ثنائي للصور (`PNG`, `JPEG`, `GIF`, `BMP`, `PPM`) | `200` أو `422` |
| `POST /cela/v1/documents/ocr` | استخراج النصوص والحقول المنظمة من `PDF`/`PNG`/`HTML` | `200` أو `422` |
| `POST /cela/v1/audio/transcribe` | تحليل ملفات `WAV` واستخراج النصوص المضمّنة | `200` أو `422` |

وتتوفر المسارات المماثلة على السطح العام تحت `/api/*` محميةً بـ `CELA_API_TOKEN` (أو `OS_API_TOKEN`):
`GET /api/capabilities`, `POST /api/nlp/entities`, `POST /api/search/index`,
`POST /api/search/semantic`, `POST /api/ml/analyze`, `POST /api/vision/analyze`,
`POST /api/documents/ocr`, `POST /api/audio/transcribe` — مع مشاركة قاعدة المعرفة نفسها داخل `ApiRuntime.ai`.

وعلى سطح المنصة نفسه يوجد `GET /ledger?limit=N` (برمز `CELA_API_TOKEN`) ويعيد التقرير
ذاته الذي يعيده `/cela/v1/ledger`: **نفس ملف السجل، رمزان مختلفان، ولا خلط بينهما**.

---

## 6. الاستخدام البرمجي المباشر (`CelaBrowser`)

```python
from os_server import CelaBrowser

browser = CelaBrowser()

# قراءة متزامنة
info = browser.whoami()
verdict = browser.check_url("https://example.org/search")

# محرك الذكاء الاصطناعي المحلي (CPU — دون اتصال خارجي ودون رسوم لكل طلب)
caps = browser.ai_capabilities()
entities = browser.extract_entities("شركة أرامكو في الرياض بتاريخ ١٥ أكتوبر ٢٠٢٦")
browser.index_documents(["الذكاء الاصطناعي وتعلم الآلة على المعالج المحلي"], namespace="kb")
hits = browser.semantic_search("machine learning artificial intelligence", namespace="kb")
forecast = browser.ml_analyze("forecast", series=[10, 20, 30, 40], horizon=2)

# تشغيل وانتظار النتيجة داخل حلقة أحداث asyncio
snapshot = await browser.browse(
    "https://example.org/search",
    "Check the open-source model list",
    mode="flow",
    wait=True,
)

# سجل النتائج: آخر 10 تشغيلات + الملخّص، وسطر تشغيل واحد
report = browser.outcome_ledger(limit=10)  # {"ledger": {...}, "entries": [...], "summary": {...}}
entry = browser.outcome_for_run(snapshot["run_id"])  # KeyError إن لم يُسجَّل
```

---

## 7. سجل نتائج التشغيل (Run-Outcome Ledger)

`ApiRuntime` يملك سجلًا واحدًا (`os_core/ledger.py`) يمرّره إلى كل منصة يبدأ بها
تشغيلًا، فالأسطح الثلاثة (REST، `/cela/v1`، `CelaBrowser`) تكتب في الملف نفسه:

| الخاصية | القيمة |
|---|---|
| الموضع | `$CELA_LEDGER_PATH`، والافتراضي `.cela/ledger.jsonl` بجوار العملية |
| التعطيل | `CELA_LEDGER_PATH=off` (أو `none` / `disabled` / `false` / `0` / سلسلة فارغة) |
| متى يُكتب | في `finally` داخل `Platform.run` — لكل تنفيذ، بما فيه الفاشل والمقطوع |
| الصيغة | JSON Lines: سطر واحد لكل تشغيل، مع `schema_version` و`seq` متزايد |
| المتانة | `fsync` بعد كل سطر؛ سطر تالٍ يُتخطّى ويُعدّ (`corrupt_lines`) ولا يكسر القراءة |
| عند تعطيله | `GET /ledger` يعيد `{"ledger": {"enabled": false}, "entries": [], "summary": null}` — القراءة ليست خطأ |

```bash
curl -H "Authorization: Bearer $CELA_API_TOKEN" \
  "http://127.0.0.1:8080/ledger?limit=20"
```

```json
{
  "ledger": {"enabled": true, "path": ".cela/ledger.jsonl", "schema_version": 1, "entries": 1, "corrupt_lines": 0},
  "entries": [{"seq": 1, "run_id": "run_96ea63e213", "mode": "react", "status": "completed",
               "outcome": "success", "steps_used": 2, "tokens_used": 370, "tool_calls": 1,
               "tool_failures": 0, "duration_ms": 33.691, "error": null}],
  "summary": {"total": 1, "outcomes": {"success": 1, "failed": 0, "budget": 0, "aborted": 0},
              "success_rate": 1.0, "duration_ms": {"p50": 33.691, "p95": 33.691, "max": 33.691}}
}
```

القرار وسببه: [`adr/0004-run-outcome-ledger.md`](../adr/0004-run-outcome-ledger.md).
