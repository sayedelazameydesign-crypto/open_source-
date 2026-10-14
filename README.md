# cela_use_browser (`open_source`) — Autonomous Browser & Sandboxed AI Platform

نموذج أولي **يعمل فعلًا** لمنصة **`cela_use_browser`** المستقلة على مكدّس مفتوح المصدر بالكامل
(2026): استدلال وكيلي + واجهة تصفح خاصة (`/cela/v1` و`CelaBrowser`) + مخرجات Artifacts
لحظية + تنفيذ معزول — بنمطي Claude (Artifacts) و Manus (التنفيذ الذاتي متعدد الخطوات).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> **644 اختبارًا ناجحًا · 0 فشل · دون مفاتيح API ودون شبكة.**

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
وفشل الكتابة نفسه لا يُفقد التشغيل (يُبلَّغ كحدث على ناقل الأحداث).

```bash
python -m os_server.cli run --demo "Sum 0..100"   # يكتب سطرًا في السجل
python -m os_server.cli ledger --limit 20          # آخر 20 نتيجة + الملخّص
curl -H "Authorization: Bearer $CELA_API_TOKEN" http://127.0.0.1:8080/ledger
curl -H "Authorization: Bearer $CELA_PRIVATE_API_TOKEN" http://127.0.0.1:8080/cela/v1/ledger
```

| الحكم `outcome` | متى يُسجَّل |
|---|---|
| `success` | أُنتج جواب نهائي (`status=completed`) |
| `failed` | خطأ، أو رفض الحواجز، أو فشل المخطِّط |
| `budget` | أوقفته الميزانية/المراجع دون جواب (`max_steps`) |
| `aborted` | أُلغي، أو قُطع قبل بلوغ حالة نهائية |

القرار وسببه: [`adr/0014-run-outcome-ledger.md`](adr/0014-run-outcome-ledger.md).

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
pytest -q          # 644 passed (1 اختبار مُعلَّق اختياريًا: psycopg)
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

## التقييم المعياري (`os_evals`)

أمر واحد يشغّل المجموعة المدمجة مقابل نموذج حقيقي، بعقد A/B للمهارات
([ADR-0012](adr/0012-real-model-eval-verification.md)):

```bash
pip install -e ".[dev,server]"
CELA_EVAL_API_KEY="<token>" python -m os_evals run --suite builtin \
  --model-url https://gateway.example.com/v1 --model-id <model> \
  --auth-header Authorization --api-key-env CELA_EVAL_API_KEY \
  --out eval-report.json --ab-skills
```

* **المفتاح لا يُمرَّر كوسيط**: يُقرأ عند الطلب من المتغيّر المسمّى بـ
  `--api-key-env` (هنا `CELA_EVAL_API_KEY`، يُحقن من سر CI)، ولا يُخزَّن ولا يُطبع.
* `--auth-header` يقبل `Authorization` (بصيغة `Bearer`) أو `x-api-key`.
* `--suite builtin` **فحص توصيل من حالتين وليس مقياس جودة** (يُطبع تنبيه بذلك)؛
  الأرقام الحقيقية تحتاج `--dataset gaia.jsonl` بصيغة GAIA أو SWE-bench.
* رموز الخروج: `0` نجاح كل الحالات (أو «لا تراجع» في `--ab-skills`)، `1` فشل
  حالة/تراجع، `2` لا قياس (تشغيل A/B انهارت فيه كل الحالات في الذراعين).
  التقرير يُكتب قبل رمز الخروج في كل الحالات.

## قنوات المحتوى (`os_reach` — ADR-0013)

طبقة قراءة للإنترنت العام: كل قناة تعلن ما تستطيع قراءته ومدى صحتها، والمنصة تحتفظ
بسياساتها — فحص الوجهة **قبل** كل نداء و**قبل كل قفزة إعادة توجيه** فوق أرضية
`SSRFGuard`، وسقف جسم صلب، وتنقية لكل نص مرئي.

```bash
export CELA_REACH_ENABLE=1                                  # تفعيل أدوات الوصول (مغلقة افتراضيًا)
export CELA_REACH_ALLOW_HOSTS="example.org,*.example.org"   # تضييق السياسة (اختياري)
python -c "from os_server.demo import build_registry; print(build_registry()[0].names())"
```

تسجَّل الأدوات `web_read`, `rss_read`, `github_repo`, `github_search` (بخطر `network`)
و`reach_doctor` (فحص محلي بلا جلب). القنوات الأصلية تعمل بـ`httpx` وحدها بلا أي تثبيت
خارجي، وكل نداء — وكل قفزة إعادة توجيه — يمرّ على `EgressPolicy` فوق أرضية `SSRFGuard`.

```bash
cela reach list                    # الطبقتان: ما هو متاح ولماذا
cela reach doctor                  # الصحة الكاملة (بلا جلب)
cela reach enable github_search    # تفعيل قناة CLI — بعد تأكيد، ويكتب reach.toml (0600)
cela reach run github_search "agents" --yes
```

**قنوات CLI (opt-in صريح):** مع `pip install -e ".[dev,server,reach]"` (Agent-Reach
مثبَّت بـSHA من GitHub) تُضاف أوامر تعمل على المضيف — `gh`, `yt-dlp`, `bili`,
`mcporter`, `twitter`, `rdt`. **هذه القنوات لا يمكن تقييدها بـ`EgressPolicy`**: البرنامج
يفتح اتصالاته بنفسه. لذلك هي مغلقة افتراضيًا، تُفعَّل قناةً قناة، ترفض العمل داخل
الصندوق المعزول (`CELA_SANDBOXED=1`)، وكل نداء يُسجَّل في تدقيق
(`timestamp/user/channel/decision/exit_code/bytes_out/correlation_id` — والاستعلام
كـ`sha256` وطول فقط، لا نصًّا). الأسطح: `GET /api/reach/channels`،
`POST /api/reach/read|rss|github|cli/{channel}`، ونظيرها في `/cela/v1/reach/*` —
برمز المصادقة نفسه لكل فضاء. التفاصيل: [`docs/REACH_PLAN.md`](docs/REACH_PLAN.md).

## حدود صادقة

* `unshare` يحتاج Linux + صلاحيات؛ عند غيابه **لا يوجد عزل شبكة فعلي** ويُبلَّغ
  `network="unverified"` بدل ادعاء العزل. الإنتاج يحتاج E2B/Firecracker.
* `packages/web` هيكل موثّق فقط — الواجهة لم تُبنَ.
* حالة التشغيل الحية داخل العملية افتراضيًا؛ مع `CELA_RUN_STORE_URL=sqlite:///<path>` (أو `--store-url`) تُحفظ الحالات والأحداث والـArtifacts والتذاكر (مجزّأة) بشكل دائم وتنجو التشغيلات المنتهية من إعادة التشغيل — لكن تعدد النسخ المتزامن ما زال يحتاج Postgres+Redis (ADR-0004).
* سجل النتائج ملف محلي واحد بلا تدوير (rotation) ولا تنسيق بين المضيفين؛ قراءته
  تعيد قراءة الملف كاملًا — كافٍ لنموذج أولي، وغير كافٍ لأسطول.
* مقاعد GAIA / SWE-bench / OSWorld غير مبنية؛ الطبقات جاهزة لكن لا أرقام بعد.
* قنوات المحتوى الأصلية تقرأ الإنترنت العام، وتُقيَّد بـ`CELA_REACH_ALLOW_HOSTS`
  وتُستثنى بـ`CELA_REACH_DENY_HOSTS`، ويمكن تضييقها لنشرٍ ما بـ`create_app(..., reach_policy=...)`.
* **قنوات CLI تفلت من `EgressPolicy` بحكم طبيعتها** (عملية على المضيف تفتح شبكتها
  بنفسها): الضوابط البديلة هي التفعيل الصريح لكل قناة، وإقرار منفصل للقنوات التي
  تستعير جلستك (`CELA_REACH_CLI_ALLOW_AUTH`)، والرفض داخل الصندوق، والتدقيق الكامل
  لكل نداء — لا حماية مزعومة لا تُنفَّذ. ومع ذلك، لا أرقام عن أدائها: القياس عبر
  `os_evals` فقط (مرحلة 4).
* لا استخراج كوكيز من متصفح المستخدم ولا قراءة إعدادات Agent-Reach، ولا عمليات كتابة
  (نشر/تعليق) — القراءة فقط.

## الترخيص

[MIT](LICENSE) — راجع [`CONTRIBUTING.md`](CONTRIBUTING.md) قبل المساهمة.
