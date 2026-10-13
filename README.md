# cela_use_browser (`open_source`) — Autonomous Browser & Sandboxed AI Platform

نموذج أولي **يعمل فعلًا** لمنصة **`cela_use_browser`** المستقلة على مكدّس مفتوح المصدر بالكامل
(2026): استدلال وكيلي + واجهة تصفح خاصة (`/cela/v1` و`CelaBrowser`) + مخرجات Artifacts
لحظية + تنفيذ معزول — بنمطي Claude (Artifacts) و Manus (التنفيذ الذاتي متعدد الخطوات).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> **392 اختبارًا ناجحًا · 0 فشل · دون مفاتيح API ودون شبكة.**

---

## التشغيل في 30 ثانية

```bash
git clone https://github.com/sayedelazameydesign-crypto/open_source-.git && cd open_source-
pip install -e ".[dev,server]"

# 1) وضع ReAct — تنفيذ كود حقيقي داخل البيئة المعزولة
python -m os_server.cli run --demo "Sum the integers from 0 to 100"

# 2) وضع PlanningFlow — DAG بتفرعات متوازية + إعادة تخطيط
python -m os_server.cli flow --demo "Plan and execute a 3-step research task"

# 3) سجل نتائج التشغيل — سطر دائم واحد لكل تنفيذ + ملخّص
python -m os_server.cli ledger --limit 20

# 4) واجهة HTTP + WebSocket + الـ API الخاص (/cela/v1)
export CELA_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CELA_PRIVATE_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m os_server.cli serve --demo --host 0.0.0.0 --port 8080
```

مخرَج حقيقي من الأمر الأول:

```
🧠 routed -> Qwen/Qwen2.5-7B-Instruct (fast): single-step tool use -> fast tier
💭 I will compute the answer with the sandboxed interpreter.
🔧 python_execute({"code": "import sys\nprint(sum(range(101)))..."})
✓ python_execute [success] 5050
🏁 completed after 2 steps
── answer: The sandbox returned: 5050. The sum of 0..100 is 5050.
```

## ما المُنفَّذ فعليًا

| الطبقة | التنفيذ |
|---|---|
| **النماذج** | `ModelRouter` بتوجيه متكيّف (DeepSeek-R1 / Qwen2.5 / Qwen2.5-VL / Llama-3.3) + تخفيض تلقائي عند نفاد الميزانية |
| **التنسيق** | ReAct + PlanningFlow (DAG، توازٍ، إعادة تخطيط ديناميكية) |
| **التنفيذ** | Sandbox بـ `setrlimit` + مهلة + `unshare --net`، ومتصفح بسلسلة fallback ثلاثية |
| **الأمان** | حارس SSRF + سياسة خروج صريحة (`EgressPolicy`) + حواجز حقن عربية/إنجليزية + HITL يفشل مغلقًا |
| **الذكاء المحلي** | `LocalAIEngine` على CPU (NLP عربي/إنجليزي، بحث دلالي هجين، تعلّم آلة، فحص صور، OCR مستندات، تحليل صوت WAV) |
| **النقل** | FastAPI: REST + WebSocket (مع تذاكر أحادية الاستخدام) + SSE + `/api/*` + `/cela/v1` + `CelaBrowser` |
| **سجل النتائج** | `RunLedger`: سطر JSONL دائم **لكل** تنفيذ (نجاح/فشل/ميزانية/انقطاع) + ملخّص إجمالي (معدّل النجاح، p50/p95)، عبر `/ledger` و`/cela/v1/ledger` |

البنية الكاملة: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · الـ API الخاص: [`docs/CELA_API.md`](docs/CELA_API.md) ·
القرارات: [`adr/`](adr) · ما تبقّى: [`docs/ROADMAP.md`](docs/ROADMAP.md)

## مصادقة API والـ API الخاص (`cela_use_browser`)

ينقسم الخادم إلى سطحين برمزين منفصلين تمامًا:

1. **سطح المنصة (`/runs`, `/ws/{id}`, …)** — يتطلب `CELA_API_TOKEN` (أو المتغيّر القديم `OS_API_TOKEN` للتوافق الخلفي).
2. **السطح الخاص (`/cela/v1/*`)** — يتطلب رمزًا مستقلًا `CELA_PRIVATE_API_TOKEN`، ويفحص عناوين URL مقابل سياسة الخروج (`EgressPolicy`) **قبل** إنشاء أي تشغيل.

```bash
export CELA_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CELA_PRIVATE_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m os_server.cli serve --demo --host 127.0.0.1 --port 8080

# فحص الهوية عبر الرمز الخاص
curl -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" http://127.0.0.1:8080/cela/v1/whoami

# بدء مهمة تصفح بعد التحقق المسبق من الوجهة
curl -X POST -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.org/search","goal":"Inspect search page","wait":true}' \
  http://127.0.0.1:8080/cela/v1/browse
```

كما يمكن استدعاء المنطق نفسه مباشرةً من Python دون HTTP عبر `CelaBrowser` — راجع
[`docs/CELA_API.md`](docs/CELA_API.md).

## سجل نتائج التشغيل (Run-Outcome Ledger)

كل تنفيذ — نجح أو فشل أو استنفد ميزانيته أو قُطع — يُسجَّل كسطر JSON واحد في ملف
ثابت عند `CELA_LEDGER_PATH` (الافتراضي `.cela/ledger.jsonl` بجوار العملية، و`off`
لتعطيله). الكتابة تحدث في `finally` داخل `Platform.run`، فلا يفلت تنفيذ من السجل،
وفشل الكتابة نفسه لا يُفقد التشغيل (يُبلَّغ كحدث `warning` على ناقل الأحداث).

```bash
python -m os_server.cli run --demo "Sum 0..100"   # يكتب السطر الأول
python -m os_server.cli ledger --limit 20          # آخر 20 نتيجة + الملخّص
python -m os_server.cli ledger --json              # التقرير نفسه بصيغة JSON

curl -H "Authorization: Bearer $CELA_API_TOKEN" http://127.0.0.1:8080/ledger
curl -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" http://127.0.0.1:8080/cela/v1/ledger
```

مخرج حقيقي من الأمرين الأولين:

```
ledger: .cela/ledger.jsonl  (1 entries)
  #1    15:53:41 run_96ea63e213   react success  steps=2   tokens=370     34ms
  summary: total=1 success=1 failed=0 budget=0 aborted=0 success_rate=1.00
  duration_ms: p50=33.691 p95=33.691 max=33.691  corrupt_lines=0
```

| الحكم `outcome` | متى يُسجَّل |
|---|---|
| `success` | أُنتج جواب نهائي (`status=completed`) |
| `failed` | خطأ، أو رفض الحواجز، أو فشل المخطِّط |
| `budget` | أوقفته الميزانية/المراجع دون جواب (`max_steps`) |
| `aborted` | أُلغي، أو قُطع قبل بلوغ حالة نهائية |

القرار وسببه: [`adr/0004-run-outcome-ledger.md`](adr/0004-run-outcome-ledger.md).

## البنية (Monorepo)

```
packages/
├── os_core/          الأنواع، التوجيه، الحواجز، HITL، الأحداث، الحالة، سجل النتائج
├── os_agents/        ReAct، PlanningFlow، DAG، الميزانية، المراجع
├── os_tools/         Sandbox، المتصفح، SSRF، سياسة الخروج (egress)، سجل الأدوات، MCP
├── os_orchestration/ واجهة Platform واختيار الوضع
├── os_server/        REST/WS/SSE + /cela/v1 + CelaBrowser + CLI + حزمة العرض
└── web/              هيكل فقط (المرحلة 3 — لم يُبنَ)
```

`os_tools` و `os_agents` لا يعرف أحدهما الآخر؛ يلتقيان فقط عبر `ToolRegistry` و
`ChatModel`، فأي طبقة تُستبدل وحدها.

## الاختبار

```bash
pytest -q          # 392 passed
ruff check .       # All checks passed
ruff format .      # تنسيق موحّد
```

كل الاختبارات حتمية ودون شبكة. ما يُختبَر **فعليًا** وليس مُحاكى: تنفيذ كود في
العملية المعزولة (المهلة، حد الذاكرة، artifacts)، وكل فئات IP في حارس SSRF بما فيها
إعادة ربط DNS، وسلسلة fallback الثلاثية للمتصفح.

## الربط بنماذج حقيقية

```python
from os_core.models import OpenAICompatModel

model = OpenAICompatModel(
    base_url="http://localhost:8000/v1",  # vLLM — أو 11434 لـ Ollama
    model_id="deepseek-ai/DeepSeek-R1",
)
```

نفس المسار البرمجي؛ لا تغيير في الوكلاء أو الأدوات.

## حدود صادقة

* `unshare` يحتاج Linux + صلاحيات؛ عند غيابه **لا يوجد عزل شبكة فعلي** ويُبلَّغ
  `network="unverified"` بدل ادعاء العزل. الإنتاج يحتاج E2B/Firecracker.
* `packages/web` هيكل موثّق فقط — الواجهة لم تُبنَ.
* حالة التشغيل في `os_server/app.py` قواميس داخل الذاكرة، فلا تعمل مع أكثر من نسخة.
* سجل النتائج ملف محلي واحد بلا تدوير (rotation) ولا تنسيق بين المضيفين؛ قراءته
  تعيد قراءة الملف كاملًا — كافٍ لنموذج أولي، وغير كافٍ لأسطول (المرحلة 4).
* مقاعد GAIA / SWE-bench / OSWorld غير مبنية؛ الطبقات جاهزة لكن لا أرقام بعد.

## الترخيص

[MIT](LICENSE) — راجع [`CONTRIBUTING.md`](CONTRIBUTING.md) قبل المساهمة.
