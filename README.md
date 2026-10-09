# open_source — Autonomous & Interactive AI Platform

نموذج أولي **يعمل فعلًا** لمنصة ذكاء اصطناعي مستقلة على مكدّس مفتوح المصدر بالكامل
(2026): استدلال وكيلي + مخرجات Artifacts لحظية + تنفيذ معزول — بنمطي Claude
(Artifacts) و Manus (التنفيذ الذاتي متعدد الخطوات).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

> **165 اختبارًا ناجحًا · 0 فشل · دون مفاتيح API ودون شبكة.**

---

## التشغيل في 30 ثانية

```bash
git clone https://github.com/sayedelazameydesign-crypto/open_source-.git && cd open_source-
pip install -e ".[dev]"

# 1) وضع ReAct — تنفيذ كود حقيقي داخل البيئة المعزولة
python -m os_server.cli run --demo "Sum the integers from 0 to 100"

# 2) وضع PlanningFlow — DAG بتفرعات متوازية + إعادة تخطيط
python -m os_server.cli flow --demo "Plan and execute a 3-step research task"

# 3) واجهة HTTP + WebSocket
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
| **الأمان** | حارس SSRF (يشمل DNS rebinding) + حواجز حقن عربية/إنجليزية + HITL يفشل مغلقًا |
| **النقل** | FastAPI: REST + WebSocket + SSE + `EventBus` بمخزن إعادة |

البنية الكاملة: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · القرارات: [`adr/`](adr) ·
ما تبقّى: [`docs/ROADMAP.md`](docs/ROADMAP.md)

## البنية (Monorepo)

```
packages/
├── os_core/          الأنواع، التوجيه، الحواجز، HITL، الأحداث، الحالة
├── os_agents/        ReAct، PlanningFlow، DAG، الميزانية، المراجع
├── os_tools/         Sandbox، المتصفح، SSRF، سجل الأدوات، MCP
├── os_orchestration/ واجهة Platform واختيار الوضع
├── os_server/        REST/WS/SSE + CLI + حزمة العرض دون مفاتيح
└── web/              هيكل فقط (المرحلة 3 — لم يُبنَ)
```

`os_tools` و `os_agents` لا يعرف أحدهما الآخر؛ يلتقيان فقط عبر `ToolRegistry` و
`ChatModel`، فأي طبقة تُستبدل وحدها.

## الاختبار

```bash
pytest -q          # 165 passed
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
* مقاعد GAIA / SWE-bench / OSWorld غير مبنية؛ الطبقات جاهزة لكن لا أرقام بعد.

## الترخيص

[MIT](LICENSE) — راجع [`CONTRIBUTING.md`](CONTRIBUTING.md) قبل المساهمة.
