# cela_use_browser (`open_source`) — Autonomous Browser & Sandboxed AI Platform

نموذج أولي **يعمل فعلًا** لمنصة **`cela_use_browser`** المستقلة على مكدّس مفتوح المصدر بالكامل
(2026): استدلال وكيلي + واجهة تصفح خاصة (`/cela/v1` و`CelaBrowser`) + مخرجات Artifacts
لحظية + تنفيذ معزول — بنمطي Claude (Artifacts) و Manus (التنفيذ الذاتي متعدد الخطوات).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> **العدد الحالي للاختبارات في [`docs/EVIDENCE.md`](docs/EVIDENCE.md)** — مُولَّد من المجموعة
> نفسها بأمر (`make evidence`) ولا يُحرَّر يدويًا. المجموعة تعمل دون مفاتيح API ودون شبكة.

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
| **الذاكرة والمعرفة** | `os_memory` (بوابة كتابة تفحص الحقن والأسرار **قبل** التخزين + خصم موثوقية) و`os_know` (تقطيع حتمي + اقتباس مُتحقَّق منه بـ`sha256`) — محليّتان بالكامل، بلا تبعية جديدة، ومُفعَّلتان بالعنوان فقط |

البنية الكاملة: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · الـ API الخاص: [`docs/CELA_API.md`](docs/CELA_API.md) ·
القرارات: [`adr/`](adr) · ما تبقّى: [`docs/ROADMAP.md`](docs/ROADMAP.md)
ربط Agent Mode بـ GitHub: [`docs/arena-github-integration.md`](docs/arena-github-integration.md)
دمج ArtCraft Launcher (مضيف + محوّل): [`docs/CRAFT_LAUNCHER_PLAN.md`](docs/CRAFT_LAUNCHER_PLAN.md)
توسيع القدرات (ذاكرة/معرفة/مهارات/مراقبة): [`docs/INTEGRATIONS_PLAN.md`](docs/INTEGRATIONS_PLAN.md) —
المراحل ٠–٤ **مُنفَّذة بمعاييرها** (القرار مُعتمَد في `adr/0016`)، والمرحلتان ٥ و٦ **مؤجَّلتان بقرار مُعلَن**:
محوّلات المزوّدين تحتاج خدمة جانبية، وقناة Graphify لا مستودع كود في CI يستدعيها.
أرقام الاسترجاع المقيسة — ومنها **ما لا يُظهر تحسّنًا** — في §١٠-أ من نفس الملف.

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

> **طبقة ثانية في الواجهة:** هذه مصادقة *الخادم* أمام الـbackend. أما في `packages/web`
> فلا يُوثَّق المتصل، فيُضاف إثبات وصول المشغّل (`CELA_WEB_ACCESS_SECRET`) أمام
> الوكيل — انظر
> [بوابة وصول المشغّل](packages/web/README.md#بوابة-وصول-المشغّل-cela_web_access_secret).

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
├── os_tools/         Sandbox، المتصفح، SSRF، سياسة الخروج (egress)، سجل الأدوات، عقد MCP الداخلي
├── os_mcp/           عميل MCP (stdio + Streamable HTTP) + جسر إلى السجل + خادم MCP فوق السجل
├── os_orchestration/ واجهة Platform واختيار الوضع
├── os_server/        REST/WS/SSE + /cela/v1 + CelaBrowser + CLI + حزمة العرض
└── web/              واجهة Next.js 15 (ADR-0005) — مساحة عمل وكتالوج Launcher
```

`os_tools` و `os_agents` لا يعرف أحدهما الآخر؛ يلتقيان فقط عبر `ToolRegistry` و
`ChatModel`، فأي طبقة تُستبدل وحدها.

ملفات الجذر `cela_pro_app.py` · `launch_cela_pro.sh` · `CELA_PRO_COLAB.ipynb` ·
`CELA_PRO_STATUS.md` **ليست جزءًا من هذه الحزم**: إنها تطبيق CELA.PRO المستقل —
انظر قسم «تطبيق CELA.PRO (Gradio)» أدناه.

## الاختبار

```bash
pytest -q          # بلا فشل — العدد في docs/EVIDENCE.md (اختبار المخزن الموزّع يتخطّى مع psycopg)
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

تقرير تشغيلي محلّيّ (دون نموذج) — يقيس **آلات التشغيل** لا النموذج: تنفيذ
الصندوق في ReAct، تنفيذ خطوات الـDAG في PlanningFlow، وتوجيه `auto` في
الاتجاهين. لا يقبل `--model-url` ولا `--dataset` بحكم التصميم حتى لا يُقرأ
رقمه كأرقام جودة ([ADR-0012](adr/0012-real-model-eval-verification.md)):

```bash
python -m os_evals operations --out operations-report.json
```

مجموعة القنوات الحيّة (`reach` — [ADR-0013](adr/0013-reach-channels.md)): أربع
حالات تجلب فعليًا عبر `web_read`/`rss_read`/`github_repo`/`github_search`، والفحص
يقرأ **ملاحظة الأداة المسجّلة** — حقيقة لا تتغيّر (`full_name`، شكل رابط commit،
`Example Domain`) — لا صياغة الجواب. تُشترط شبكة حقيقية ونموذج حقيقي وقنوات
مسجّلة؛ يرفض الأمر التنفيذ برمز `2` قبل أي قياس إن نقص أحدهما، و`--ab-skills`
يقيس على الحالات نفسها أثر المهارات على القنوات:

```bash
CELA_REACH_ENABLE=1 CELA_EVAL_API_KEY="<token>" python -m os_evals run --suite reach \
  --model-url https://gateway.example.com/v1 --model-id <model> \
  --out reach-report.json
```

فحوص الانحراف الحيّة (تُعيد الفحوص على القنوات الحقيقية) بعلامة `network`
ومُستثناة من `pytest -q` بحكم `addopts`: تشغيلها `pytest -m network`.

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

## MCP في الاتجاهين (`os_mcp` — ADR-0017)

نقل MCP حقيقي بلا تبعيات جديدة: الوكيل يستدعي **خوادم MCP خارجية** (stdio أو Streamable HTTP)،
وأي مضيف MCP (Claude Desktop, Cursor, …) يستدعي **أدوات هذه المنصة** — وفي الحالتين تمرّ كل
أداة على `Guardrails` وتصنيف الخطر وHITL قبل أن تصل إلى النموذج أو تُنفَّذ.

```jsonc
// ~/.config/cela/mcp.json  (أو CELA_MCP_CONFIG=<path>) — صيغة mcpServers المألوفة + مفاتيحنا
{
  "mcpServers": {
    "fs": {
      "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/docs"],
      "env": {"HOME": "${HOME}"},
      "risk": "read_only",                       // الافتراضي dangerous = HITL قبل كل نداء
      "tool_risk": {"write_file": "dangerous"},  // استثناء لأداة بعينها
      "tools": {"allow": ["read_file", "list_directory", "write_file"]}
    },
    "remote": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}}
  }
}
```

```bash
export CELA_MCP_ENABLE=1                 # الإذن الأول: اقرأ الإعداد واتصل بالخوادم (مغلق افتراضيًا)
export CELA_MCP_ALLOW_HOST_EXEC=1        # الإذن الثاني: اسمح بخوادم stdio تحديدًا (انظر القسم التالي)
cela mcp list                            # ما هو مُعدّ ولماذا (بلا اتصال)
cela mcp doctor                          # يتصل بكل خادم: الأدوات المجسورة / المحجورة / المُرشَّحة
cela mcp call fs read_file '{"path": "README.md"}' --yes
cela run --demo "اقرأ README.md ولخّصه"     # الأداة تظهر للوكيل باسم mcp_fs_read_file
cela mcp serve --demo                    # الاتجاه الآخر: المنصة خادم MCP عبر stdio لأي مضيف
```

ما يحدث عند الاكتشاف: وصف كل أداة ومخططها يُفحَص بأنماط الحقن (en/ar/zh) — التطابق يعني **حجرًا**
فلا يرى النموذج الأداة أصلًا. هذا الحجر **إرشادي (heuristic)**: أنماط لها سلبيات كاذبة، وخادم مسموم
يستطيع إعادة الصياغة؛ خطّ الدفاع الفعلي هو الخطر الافتراضي `dangerous` مع HITL، لا الحجر.
`destructiveHint` من الخادم **يرفع** الخطر ولا يخفّضه؛ الأسماء تُسبَّق بـ`mcp_<server>_` والبادئة
**محجوزة برمجيًا** في `ToolRegistry` (أداة منصة لا تأخذها، وأداة MCP لا تستبدل أي اسم قائم)؛
المخرجات تُقصّ إلى `max_chars`. خوادم HTTP تمرّ على `EgressPolicy` لأصلها المُعدّ فقط فوق أرضية
`SSRFGuard`. قائمة الأدوات **تُثبَّت عند الإقلاع**: إن أعلن خادم تغييرها يظهر `stale` في `doctor`
و`/api/mcp/servers` وتُطلب إعادة التشغيل — التحديث الساخن مؤجَّل صراحة. النطاق: **أدوات فقط**
(لا `resources`/`prompts`/`sampling`/OAuth). الأسطح: `GET /api/mcp/servers` و`GET /cela/v1/mcp/servers`.
القرار وحدوده والمؤجَّل منه: [`adr/0017-mcp-transport.md`](adr/0017-mcp-transport.md).

### خوادم MCP على المضيف (stdio): إذن ثانٍ، وحدّ يبقى مفتوحًا

خادم MCP عبر stdio هو **برنامج لم تكتبه هذه المنصة يعمل كعملية على جهازك، بامتيازات عملية `cela`،
خارج الصندوق المعزول وخارج `EgressPolicy`** — يفتح اتصالاته بنفسه ويقرأ ما تسمح له أذونات مستخدمك
بقراءته. لا توجد آلية داخل هذه المنصة تُغلق هذا الحدّ، ولذلك لا نزعم ذلك. ما نفعله هو تضييقه وإظهاره:

| الضابط | ما يفعله |
|---|---|
| `CELA_MCP_ENABLE=1` | الإذن الأول: قراءة الإعداد والاتصال بالخوادم (HTTP وstdio) |
| `CELA_MCP_ALLOW_HOST_EXEC=1` | الإذن الثاني، **لخوادم stdio وحدها**: بدونه تُعرض `off` بسببها ولا تُطلق |
| ملف إعداد بـ`chmod 600` | يُرفض إن كان قابلًا للكتابة من المجموعة/العالم (من يكتب الملف يشغّل أوامر) |
| بيئة بيضاء | العملية ترى `PATH`/`HOME`/`LANG`… وما أعلنته في `env` فقط، لا بيئة cela كاملة |
| `risk: dangerous` افتراضيًا | كل نداء يمرّ على HITL حتى تخفّضه أنت لخادم أو أداة بعينها |
| الرفض داخل الصندوق | لا إطلاق خوادم stdio من داخل `CELA_SANDBOXED=1` |
| `argv` بلا shell، مهلات، سقوف | لا تفسير shell، مهلة إقلاع ومهلة لكل نداء، سقف طول السطر والمخرجات |

خوادم **HTTP** لا تحتاج الإذن الثاني لأنها لا تُشغّل شيئًا على جهازك وتمرّ على `EgressPolicy`.

## ArtCraft Launcher — استضافة + محوّل قراءة فقط

شجرة Rust طبق الأصل من [`storytold/craft-launcher`](https://github.com/storytold/craft-launcher)
تُستضاف في `packages/craft-launcher/` ([شروط الاستضافة](docs/VENDORED-craft-launcher.md))،
وحزمة `os_launcher` تقرأ **كتالوج Craft App Suite وإصداراته** فقط (قراءة — لا تنزيل
ولا تثبيت) عبر `EgressPolicy` بأربعة مضيفين مسموحين فوق أرضية SSRF. الأسطح
(`GET /api/launcher/catalog`، `GET /api/launcher/apps/{id}/release`، `POST /api/launcher/check`
ونظيرها في `/cela/v1/launcher/*`) مغلقة افتراضيًا وتُفعَّل بـ`CELA_LAUNCHER_ENABLE=1`،
وأدوات `craft_list_apps`/`craft_latest_release`/`craft_check_updates` تُسجَّل يدويًا فقط.
بناء شجرة Rust: `make launcher-test` (CI مستقل: `launcher-ci.yml`). التفاصيل والقرارات:
[`docs/CRAFT_LAUNCHER_PLAN.md`](docs/CRAFT_LAUNCHER_PLAN.md) · [`adr/0015-craft-launcher-integration.md`](adr/0015-craft-launcher-integration.md).

> **لا تعدّل شيئًا داخل `packages/craft-launcher/` — ولا ملفًا واحدًا.** «طبق الأصل»
> ليست وصفًا بل **خاصية مُختبَرة**: كل ملف من الـ198 مُسجَّل ببصمة `sha256` في
> [`docs/vendored/craft-launcher.sha256`](docs/vendored/craft-launcher.sha256)، وأي
> إضافة أو حذف أو تعديل يُفشل `make check`. السبب ليس تنظيميًا: إذن استعمال شعارات
> ArtCraft يغطي الشجرة **ما دامت نسخة غير معدَّلة**، ويسقط تلقائيًا لو لم تكن.
> التغيير يُقترح أعلى أولًا، ثم `make vendored-write COMMIT=<sha>`.

## طبقتا الذاكرة والمعرفة (`os_memory` · `os_know` — ADR-0016)

المنصة تتذكّر بين الجلسات وتقتبس من مستودعاتك — **بلا خدمة خارجية وبلا تبعية جديدة**. الحزمتان
اختياريتان في السلوك: بلا `CELA_MEMORY_STORE_URL`/`CELA_KNOW_STORE_URL` تبقى المنصة كما كانت.

| الطبقة | ما تفعله | ما لا تدّعيه |
|---|---|---|
| `os_memory` | سجلات `MemoryRecord` (SQLite/Postgres/ذاكرة) + **بوابة كتابة** تفحص الحقن والأسرار قبل التخزين، واسترجاع بخصم ثقةٍ وقِدَم، وتوثيق كل حقن بـ`id` ومنشأ | ليست «ذاكرة محسّنة بالنموذج»: بلا مفتاح لا LLM — القطع مستخرجة بالأدوات ومطابقة مفاهيمية حتمية |
| `os_know` | تقطيع هيكلي حتمي (1600 حرف سقفًا) + استرجاع مع **اقتباس مُتحقَّق منه** (`sha256` + نطاق أسطر) و`recall@k`/`MRR` على مجموعة ذهبية | **لم تُقَس فائدة ترتيب على متون طويلة** مقابل المستند الكامل؛ المكسب المقيس هو اقتصاد السياق (٥٫٣×) |

```bash
cela memory remember "أفضِل apt على npm" --key pkg.manager --provenance user
cela memory recall "أي مدير حزم؟"          # يُحقن موسومًا <untrusted-memory>
cela know index ./docs --corpus repo && cela know search "سياسة الخروج" --corpus repo
cela telemetry export --out trace.json     # OTLP/JSON بلا مجموعة جمع؛ ‏--send يحتاج CELA_OTEL_ENDPOINT
```

**بوابة الكتابة ليست نصيحة:** نصّ من `os_reach` يحمل تعليمة حقن **يُرفض عند التخزين**، وما يمرّ يبقى
`candidate` لا يُرقَّى تلقائيًا أبدًا. ‏`python_execute` لا يقرأ الذاكرة. والأخطاء في الطبقتين **لا تُفشل
تشغيلًا** — تُسجَّل كتحوّط وتُمضَى الجلسة. التفاصيل: [الخطة وأرقامها](docs/INTEGRATIONS_PLAN.md) ·
[القرار](adr/0016-memory-and-retrieval-layer.md).

**فضاءات الذاكرة (`namespace`):** كل سجل يحمل فضاءً، والدمج (التأكيد/التجاوز) والاسترجاع **لا يعبران
الفضاء أبدًا** — الجملة نفسها في فضاءين سجلان مستقلان. الخادم يربط ذاكرته كلّها بفضاء واحد يسمّيه
المشغّل: `CELA_MEMORY_NAMESPACE` (افتراضيًا `default`)؛ وفي CLI ‏`--namespace` لكل أمر، و`list --all`
لعرض المشغّل عبر الفضاءات. أدوات `memory_*` **لا تكشف** الفضاء للنموذج، فلا يختار وكيلٌ ذاكرةَ مَن يقرأ.
اسم فضاء غير صالح **خطأ إعداد** (رمز خروج 2) لا انزلاق صامت إلى `default`. الملفات المخزَّنة قبل هذا
العمود تُرحَّل في مكانها (`ADD COLUMN`) وتُقرأ صفوفها في `default`.

---

## الوكلاء الفرعيون المحليون والتحقق الذاتي (`os_agents.subagents` — ADR-0020)

تفويض روتيني **محدود ومغلق افتراضيًا**: المنسّق يخطّط، ووكيل خفيف محلي (‏`llama3:8b` عبر Ollama
أي نقطة OpenAI-متوافقة) يجمع البيانات — بلا تكاليف API وبلا تبعيات نواة جديدة.

```bash
export CELA_SUBAGENTS_DIR=./agents/sub_agents   # الغياب = التعطيل
export CELA_SUBAGENTS_ROOT=.                    # جذر أدوات القراءة
python -m os_server.cli run --demo "Sum the integers from 0 to 100"   # بلا مفاتيح

# ولنموذج محلي حقيقي عبر Ollama (نفس السجل نفسه، بلا تغيير في الوكلاء):
#   OpenAICompatModel(base_url="http://localhost:11434/v1", model_id="llama3:8b")
```

| المكوّن | ما يفعله | الحدّ الذي يفرضه |
|---|---|---|
| `SubAgent` + `agents/sub_agents/*` | إعداد بصيغة JSON أو `key: value` (‏`explorer.md` صيغة الخطة حرفيًا)، وتفويض عبر `delegate_subagent`/`subagent_list` | قائمة أدوات صارمة (قراءة+بحث فقط) تُرفض **وتسجَّل** خارجها + حدود `max_steps`/`max_seconds` |
| `os_core.resources` | فحص RAM/CPU **قبل** أول استدعاء استدلال (`/proc/meminfo` ← `sysconf` ← `psutil` اختياريًا) | نقص ⇒ `LocalResourceError` ونتيجة `resource shortage` — لا OOM في منتصف تشغيل |
| `os_know.reader` | `read_document`/`index_path`: PDF وPNG وHTML تُستخرج محليًا عبر `document_ocr` ثم تدخل المتجر المتجهي المحلي (‏`SQLiteKnowStore`) | سقف 5 MiB عند الحافة، وإعادة الاستخدام لا استيراد مكدّس OCR جديد |
| `os_agents.validation` | `OutputContract` (‏Pydantic + أنماط + `compile()` لكود المخرج) و`SelfCorrectionLoop` يعيد الإنتاج مع سرد المخالفات | قبول بمحاولات مُوثَّقة، رفض عند `max_attempts` — التحقق لا يرمي أبدًا، المخالفات بيانات |

أدوات الاستكشاف نفسها (`file_reader`/`grep_search`) مُحتوَية على جذر مساحة عمل، وقائمة رفض
لملفات الأسرار، وتمرّ عليها `scrub_credentials` طبقةً ثانية. القرارات والحدود والمؤجَّل:
[`adr/0020-local-sub-agents-and-guardrails.md`](adr/0020-local-sub-agents-and-guardrails.md).

---

## حدود صادقة

* `unshare` يحتاج Linux + صلاحيات؛ عند غيابه **لا يوجد عزل شبكة فعلي** ويُبلَّغ
  `network="unverified"` بدل ادعاء العزل. الإنتاج يحتاج E2B/Firecracker.
* `packages/web` واجهة Next.js 15 مبنيّة؛ معاينة الـArtifacts المضمنة وشجرة DAG الحية ما زالتا ضمن المتبقي.
* **بوابة وصول المشغّل (`CELA_WEB_ACCESS_SECRET`) إثبات «أنت المشغّل» لعملية واحدة، لا هوية ولا صلاحيات.** الوكيل `app/api/backend/*` يوثّق الخادم أمام الـbackend ولا يوثّق المتصل؛ لذلك تفرض البوابة (`/api/access`) إثباتًا موقّعًا ومنتهي الصلاحية — كوكي `HttpOnly` لا تحمل السر، أو ترويسة `X-Celia-Access` للأتمتة. الإنتاج بلا سر **يفشل مغلقًا** (`503 access_gate_unconfigured`)، والوكيل على `packages/web` أو `functions/` يبقى بلا حماية إن فُتح صريحًا بـ`CELA_WEB_GATE_REQUIRED=0`. رموز الـbackend تبقى مطلوبة للوصول المباشر إلى Render. التفاصيل: [`packages/web/README.md`](packages/web/README.md#بوابة-وصول-المشغّل-cela_web_access_secret).
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
* **فضاء الذاكرة لكل عملية خادم لا لكل مستدعٍ:** واجهة HTTP تُصادق برمز حامل واحد مشترك، فكل
  المستدعين مبدأٌ واحد ولا هوية يُشتق منها فضاء لكل طلب. الحدّ الصادق اليوم: `CELA_MEMORY_NAMESPACE`
  يسمّي فضاء الخادم كلّه؛ ومع `CELA_MEMORY_STORE_URL` مضبوطًا على خادم مشترك يقرأ كل طلب ويكتب في
  ذلك الفضاء الواحد (الكتابة التلقائية للحلقات قابلة للإيقاف بـ`memory_auto_write=False`). الفضاء لكل
  مبدأ ينتظر أن تعرف الواجهة مبادئ متعددة — نقطة الربط جاهزة (`manager.scoped(ns)`).
* **خوادم MCP عبر stdio تعمل على المضيف خارج الصندوق وخارج `EgressPolicy`** (عملية تفتح شبكتها
  بنفسها — نفس حدّ قنوات CLI)؛ القسم المخصص أعلاه يشرح الإذنين والضوابط. حجر الأوصاف المسمومة
  **إرشادي** لا ضمان. خوادم HTTP وحدها تمرّ على `EgressPolicy`. الدعم للأدوات فقط
  (لا `resources`/`prompts`/`sampling`/OAuth)، وقائمة الأدوات تُثبَّت عند الإقلاع — التحديث الساخن
  مؤجَّل صراحة (ADR-0017).

## تطبيق CELA.PRO (Gradio) — تطبيق مستقل، لا جزء من المنصة

أربعة ملفات في جذر المستودع لا تنتمي إلى الحزم أعلاه: [`cela_pro_app.py`](cela_pro_app.py)
(واجهة Gradio عربية RTL) و[`launch_cela_pro.sh`](launch_cela_pro.sh) و
[`CELA_PRO_COLAB.ipynb`](CELA_PRO_COLAB.ipynb) و[`CELA_PRO_STATUS.md`](CELA_PRO_STATUS.md).
هو تطبيق محادثة متعدد المزوّدين (Groq · Gemini · OpenRouter · Kimi · Ollama) بمحرّك مهارات
(`SkillEngine`) و`OrionAgent` بموافقة بشرية، وفحص اتصال يُصنَّف
`BLOCKED` / `FAIL` / `INCONCLUSIVE` / `PASS`، وحجب تلقائي لقيمة المفتاح (`[REDACTED]`)
في رسائل الخطأ ومخرجات الفحص.

**ما هو ليس كذلك — بصراحة:**

* لا تستورده أي حزمة `os_*` ولا يستوردها هو؛ ولا يسجّل أدواته في `ToolRegistry`.
* لا يمرّ على `SSRFGuard` ولا `EgressPolicy`، ولا ينفّذ كوده داخل الصندوق المعزول،
  ولا يكتب في سجل النتائج. ضوابط المنصة أعلاه **لا تغطّيه**.
* ليس ضمن حزم النشر: `pyproject` يضم حزم `os_*` وحدها.
* واجهته ليست واجهة المنصة. إن أردت واجهة **فوق** المنصة (سند ميزانية، حواجز،
  موافقات، سجل نتائج) فالطريق هو `packages/web` مع `/cela/v1`.

**تشغيله:** `gradio` **ليس** في أي extra في هذا المشروع — `pip install -e ".[dev,server]"`
لا يكفي لتشغيله؛ ثبّته بنفسك أو استخدم السكربت (يثبّته ويحذّر عند غياب حماية الرابط العام).
متغيّراته: `CELA_DEFAULT_PROVIDER` و`*_API_KEY` و`*_BASE_URL` و`*_MODEL` و
`GRADIO_USERNAME`/`GRADIO_PASSWORD` — نموذجها في [`.env.example`](.env.example).

**ضوابطه الأمنية الخاصة:** لا تبديل تلقائي بين المزوّدين عند الفشل (رسالة خطأ تخصّ
المزوّد المختار) · «المفتاح مُعيّن» **لا** يعني «متصل» — الادعاء الوحيد بالاتصال هو `PASS` ·
اختيار المزوّد يعرض حالة الإعداد فقط ولا يرسل قيمة المفتاح إلى الواجهة.

اختباراته [`tests/test_cela_pro_app.py`](tests/test_cela_pro_app.py): محلية بالكامل،
بلا نداءات API حقيقية، وتتحقّق من سلامة دفتر Colab أيضًا (وتعمل ببديل وهمي حين لا يكون
`gradio` مثبَّتًا، كما في CI).

## الترخيص وطرف ثالث

**[MIT](LICENSE)** — للكود المكتوب هنا. راجع [`CONTRIBUTING.md`](CONTRIBUTING.md) قبل المساهمة،
وللإبلاغ عن ثغرة أمنية: [`SECURITY.md`](SECURITY.md) — لا تفتح issue عامًا.

رخصة الجذر **لا تغطي كل ما في هذا الـcheckout**. النطاق الكامل، مادةً بمادة:
**[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)**.

| ما هنا | الرخصة | ملاحظة |
|---|---|---|
| `packages/os_*`, `tests/`, `docs/`, `scripts/` | **MIT** | كودنا |
| `packages/craft-launcher/` | **MIT OR Apache-2.0** | نسخة طبق الأصل من [`storytold/craft-launcher`](https://github.com/storytold/craft-launcher) @ `0f295d4b` — مُتحقَّق منها ببصمة `sha256` لكل ملف. [شروط الاستضافة](docs/VENDORED-craft-launcher.md) |
| الشعارات داخلها (`docs/brand/`, `assets/app-icon/`) | **ليست مفتوحة المصدر** | علامة ArtCraft التجارية — موجودة هنا لأن نسختنا **غير معدَّلة**. أي fork يُعدِّل الشجرة **يجب أن يزيلها**: `make defork-launcher ARGS="--i-am-forking --dry-run"` |
| `psycopg` (extra `distributed` الاختياري) | **LGPL-3.0-only** | التبعية غير المتساهلة الوحيدة — مستورَدة كمكتبة، لا تُنسخ ولا تُدمَج |

**هذه الشجرة لا تُوزَّع منا:** مستبعدة صراحةً من الـ`sdist` وغير مُدرجة في الـ`wheel`،
فلا شيء منشور باسم `cela-use-browser` يحتويها.

### الامتثال مُنفَّذ بفحوصات، لا بفقرات

القاعدة التي لا يفشل شيء عند كسرها ليست قاعدة، إنها تمنّى. القواعد الثمانية
(T-1…T-8) في **[`docs/THIRD_PARTY_POLICY.md`](docs/THIRD_PARTY_POLICY.md)** — بتصنيفات
أربعة، وشجرة قرار، وأعلام حمراء، وأمثلة **وقعت فعلًا في هذا المستودع**. القرار
المعماري: [ADR-0018](adr/0018-third-party-ip-governance.md).

```bash
make check                     # lint + الاختبارات + فحوصات الامتثال (بلا شبكة)
make vendored-verify           # الشجرة مقابل البصمة المسجَّلة
make vendored-verify-upstream  # + إعادة اشتقاق البصمة من النسخة الأصلية (شبكة)
```

```
docs/vendored/craft-launcher.sha256
  files   198
  digest  sha256:8e008dc770754f3765d0c8e545d9a16dc1224aa9bc6faff21b205719e1470039
          ← مطابق لـdigest النسخة الأصلية عند 0f295d4b
```

قبل إضافة أي كود أو أصل أو تبعية من طرف ثالث: **صنِّفه أولًا** (شجرة القرار في
السياسة)، واذكر المالك والرخصة في وصف الـPR، وأضِف الإشعار في نفس الـPR.
`«مفتوح المصدر» ليست رخصة` — الشعارات أعلاه تعيش في مستودع مفتوح المصدر وهي ليست مفتوحة المصدر.
