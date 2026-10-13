# الهندسة المعمارية — منصة ذكاء اصطناعي مستقلة وتفاعلية

> حالة التنفيذ: **نموذج أولي يعمل** (Runnable MVP Backend). كل ما هو موثّق هنا مُنفَّذ
> ومُختبَر في `packages/`، ما لم يُذكر صراحةً أنه "غير مُنفَّذ بعد".

---

## 1. نظرة عامة

المنصة تجمع بين نمطين للتنفيذ خلف واجهة واحدة (`Platform.run`):

| الوضع | متى يُستخدم | التنفيذ |
|---|---|---|
| **Direct Agent (ReAct)** | مهام قصيرة، خطوة أو خطوتان | `os_agents/react.py` |
| **Flow Orchestration (PlanningFlow)** | مهام متعددة الخطوات | `os_agents/flow.py` — DAG + إعادة تخطيط |

الاختيار تلقائي عبر `Platform.choose_mode()`، ويُجبر بـ `mode="react"|"flow"`.

```
User UI (Next.js split-view)  ← WebSockets / SSE
        │
   os_server (FastAPI)         ← النقل: REST + WS + SSE + HITL
        │
   os_orchestration.Platform   ← اختيار الوضع + تركيب الطبقات
        ├──────────────┬─────────────────┐
   os_agents        os_core          os_tools
   (ReAct/Flow)     (router,          (sandbox, browser,
                    guardrails,        MCP registry)
                    events, state)
```

## 2. الحزم

| الحزمة | المسؤولية | الملفات الرئيسية |
|---|---|---|
| `os_core` | الأنواع المشتركة، توجيه النماذج، الحواجز، الأحداث، الحالة، سجل النتائج | `types.py`, `router.py`, `guardrails.py`, `hitl.py`, `events.py`, `state.py`, `ledger.py` |
| `os_agents` | حلقة ReAct، مخطط DAG، الميزانية، المراجع | `react.py`, `flow.py`, `dag.py`, `budget.py`, `reviewer.py` |
| `os_tools` | تنفيذ الكود المعزول، المتصفح، حارس SSRF، سجل الأدوات | `code_exec.py`, `browser.py`, `ssrf.py`, `registry.py`, `mcp.py` |
| `os_orchestration` | واجهة المنصة واختيار الوضع | `platform.py` |
| `os_server` | REST/WS/SSE + CLI + حزمة العرض دون مفاتيح | `app.py`, `cli.py`, `demo.py` |

لا يوجد تبعيات دائمة بين `os_tools` و `os_agents` — يلتقيان فقط عبر
`ToolRegistry` و `ChatModel`، لذا يمكن استبدال أي طبقة وحدها.

## 3. القيود الهندسية الثلاثة وكيف عولجت

### القيد 1 — الاستقلالية مقابل التكلفة (Adaptive Model Routing)
`os_core/router.py`. كل طلب يُحوَّل إلى `TaskProfile` ثم يُوجَّه:

| الإشارة | الطبقة | النموذج الافتراضي |
|---|---|---|
| صورة مرفقة | `vision` | Qwen2.5-VL-7B |
| مراجعة مسار | `critic` | Qwen2.5-14B |
| إشارة تخطيط / سياق طويل (> 6000 توكن) | `planner` | DeepSeek-R1 |
| خلاف ذلك | `fast` | Qwen2.5-7B |

**آلية التحكم في التكلفة:** `ModelRouter.charge()` يسجّل الاستهلاك الحقيقي، وعند اقتراب
`token_budget` يُخفَّض التخطيط من `planner` إلى `fast` تلقائيًا (`allow_fallback`)، مع
`fallback_planner` (Llama-3.3-70B) كطبقة وسيطة. هذا السلوك مُختبَر في
`test_oversized_prompt_downgrades_when_it_cannot_fit_the_budget`.

### القيد 2 — العزل والأمان
طبقتان مستقلتان:

1. **حد الشبكة (Hard boundary)** — `os_tools/ssrf.py`: كل URL يُفحص قبل أي طلب.
   يُرفض: loopback، link-local (`169.254.169.254`)، RFC1918، reserved، multicast،
   وأي مخطط غير `http/https`. عند `resolve_dns=True` تُفحص **كل** العناوين المُرجعة،
   فتُسَد ثغرة DNS rebinding (مُختبَرة في `test_dns_rebinding_to_internal_ip_is_blocked`).
2. **عزل التنفيذ** — `os_tools/code_exec.py`: العملية الوليدة تعمل بـ
   `setrlimit` (CPU، الذاكرة، حجم الملف، `RLIMIT_NPROC` ضد fork bomb)، مهلة
   wall-clock مع `proc.kill()`، `cwd` مؤقت خاص، و`unshare --net --map-root-user --fork
   --kill-child` عند توفره. يُبلَّغ عن حالة الشبكة بصدق
   (`blocked (netns)` أو `unverified`) بدل ادعاء العزل.

> **حدود صادقة (مثبتة بـ CI، لا نظرية):** وجود ملف `unshare` **لا يعني** أنه يعمل.
> على عدّاءات GitHub托管 الملف موجود لكنه يرفض كتابة `uid_map` برسالة
> `Operation not permitted`. لذا `netns_unshare_available()` **تُنفِّذ الأمر فعليًا**
> مرة واحدة وتخبّئ النتيجة، وعند الرفض يُبلَّغ `unverified (netns unavailable on this host)`
> ويُتابَع التنفيذ بلا netns بدل تحطيم كل تنفيذ. الواجهة `CodeExecBackend` هي نقطة
> الاستبدال بـ E2B/Firecracker للإنتاج.

### القيد 3 — تزامن الحالة
`os_core/events.py` + `os_core/state.py`. كل تغيير في شجرة التنفيذ يُنشَر كـ `Event`
مُصنَّف (17 نوعًا). `EventBus` يحتفظ بمخزن إعادة (replay buffer) فيمكن لواجهة تتصل
متأخرةً أن تستعيد السجل كاملًا ثم تتابع الحي. `RunState.snapshot()` هو المصدر الوحيد
للحقيقة المعروض على الواجهة.

### ما يبقى بعد انتهاء التشغيل
`os_core/events.py` و`os_core/state.py` يصفان التشغيل **أثناء** حدوثه و**داخل
الذاكرة**؛ لا شيء منهما يجيب عن سؤال المشغّل في اليوم التالي: كم تشغيلًا نُفِّذ، وكم
منها انتهى فعلًا؟ لذلك يضيف `os_core/ledger.py` سجلًا دائمًا:

* **سطر JSON واحد لكل تنفيذ** في ملف JSONL ثابت (`CELA_LEDGER_PATH`، والافتراضي
  `.cela/ledger.jsonl`؛ `off` يعطّله)، بلا أي تبعية جديدة — مقبض ملف وقفل و`os.fsync`.
* **الكتابة في `finally`** داخل `Platform.run`، فيُسجَّل التنفيذ المكتمل والفاشل
  والمستنفد للميزانية والمقطوع على السواء (`RunOutcome` بأربعة أحكام:
  `success` / `failed` / `budget` / `aborted`).
* **فشل السجل لا يُفقد التشغيل:** `LedgerError` يُلتقط ويُبلَّغ كحدث على الناقل، فلا
  يستبدل خطأ التشغيل نفسه، والأسطر التالفة تُتخطّى وتُعدّ بدل كسر القراءة.
* **سطح واحد للقراءة:** `GET /ledger` (رمز المنصة)، `GET /cela/v1/ledger` و
  `GET /cela/v1/runs/{id}/outcome` (الرمز الخاص)، `CelaBrowser.outcome_ledger()`،
  و`python -m os_server.cli ledger`.

## 4. الحالات الاستثنائية

### الحلقات المفرغة
`os_agents/budget.py` يكسر الحلقة بشرطين **متوازيين**، لا بعدّاد خطوات فقط:

* سقوف صلبة: `max_steps=25`، `max_seconds=300`، `max_tokens`، و`max_consecutive_errors=3`.
* **الركود (stagnation):** بصمة `tool:args` مُرتَّبة؛ تكرار نفس الاستدعاء
  `repeat_threshold=3` مرات يُوقف الحلقة ويُحيل إلى المراجع.

`reviewer.py` يعيد أحد ثلاثة أحكام: `pass` / `correct` (يحقن تعليمًا تصحيحيًا في
السياق) / `terminate`. عدد مراجعات الركود محدود بـ `max_stagnation_reviews=2`.

### فشل الأدوات وتحوّل الـ DOM
`os_tools/browser.py` — سلسلة من ثلاث مراحل:

1. إعادة محاولة مع إعادة استعلام الـ selector (تفاعلات React قد تنجح لاحقًا).
2. **إعادة التموضع بالنص**: استبدال مسار CSS الهشّ بـ `text=` من النص المُتاح.
3. **البديل البصري**: لقطة الشاشة تُرسل إلى طبقة VLM التي تُعيد إحداثيات.

عند استنفاد الثلاثة يُبلَّغ `fallbacks_exhausted=True` مع سبب CSS الأصلي.

## 5. الأمان والحوكمة

* **الحقن:** `Guardrails.scan_text()` يفحص العربية والإنجليزية (٧ أنماط). الإدخال
  المُشبوه يُرفض **قبل** أي استدعاء نموذج — مُختبَر بأن `model.call_count == 0`.
* **تصعيد الصلاحيات:** `Guardrails.classify()` يرفع مستوى الخطر بناءً على المحتوى
  (`rm -rf /`، fork bomb، عناوين metadata) ولا يخفضه أبدًا.
* **HITL:** خطر `DANGEROUS` يوقف التنفيذ على `Approver`. في الإنتاج
  `QueueApprover` **يفشل مغلقًا** (deny) عند انتهاء المهلة — مُختبَر.
* **فحص الوسائط:** وسائط الأدوات تُفحص هي أيضًا، فالنموذج قد يهرّب تعليمات داخل
  `args` (`test_argument_screening_catches_smuggled_injection`).

## 6. الاختبار والتقييم

392 اختبارًا، كلها دون مفاتيح API ودون شبكة. على عدّاءات CI التي لا تستطيع إنشاء
netns يُتخطّى اختبار الشبكة **صراحةً** (`1 skipped`) بدل ادعاء عزل غير موجود؛ وعلى
العدّاء الذي يوفّر netns تنجح الاختبارات كلها (`392 passed, 0 skipped`).

CI أخضر على Python 3.11 / 3.12 / 3.13 — انظر <https://github.com/sayedelazameydesign-crypto/open_source-/actions>:

```bash
pip install -e ".[dev,server]"
pytest            # 392 passed
ruff check .      # All checks passed
```

| ما يُختبَر فعليًا | الملف |
|---|---|
| توجيه النماذج وسقوف التكلفة | `test_router.py` |
| أنماط الحقن العربية/الإنجليزية + التصعيد | `test_guardrails.py` |
| كل فئات IP + DNS rebinding | `test_ssrf.py` |
| **تنفيذ كود حقيقي** في العملية المعزولة: المهلة، حد الذاكرة، artifacts | `test_code_exec.py` |
| سلسلة fallback الثلاثية للمتصفح | `test_browser.py` |
| كسر الحلقة + المراجع | `test_budget.py`, `test_reviewer.py` |
| HITL، الحواجز، بث الأحداث | `test_react.py` |
| DAG، التوازي، إعادة التخطيط الديناميكية | `test_flow.py` |
| المسار الكامل + MCP | `test_e2e.py` |
| REST + SSE + WebSocket | `test_server.py` |
| سجل النتائج: الإلحاق، استمرار التسلسل عبر إعادة التشغيل، الأسطر التالفة، الملخّص، `/ledger` | `test_ledger.py` |

**معايير خارجية (غير مُنفَّذة بعد):** GAIA، SWE-bench، OSWorld تحتاج نماذج حقيقية
وموارد؛ الطبقات جاهزة لكن مقاعد الاختبار (harnesses) لم تُبنَ. انظر
`docs/ROADMAP.md`.

## 7. التشغيل

```bash
# وضع ReAct — نموذج مُبرمَج، تنفيذ كود حقيقي
python -m os_server.cli run --demo "Sum the integers from 0 to 100"

# وضع PlanningFlow — DAG بتفرعات متوازية
python -m os_server.cli flow --demo "Plan and execute a 3-step research task"

# واجهة HTTP + WebSocket
python -m os_server.cli serve --demo --host 0.0.0.0 --port 8080

# سجل نتائج التشغيلات (الافتراضي .cela/ledger.jsonl بجوار العملية)
python -m os_server.cli ledger --limit 20
```

الربط بنماذج حقيقية — استبدل `ScriptedModel` بـ `OpenAICompatModel`:

```python
from os_core.models import OpenAICompatModel

model = OpenAICompatModel(
    base_url="http://localhost:8000/v1",  # vLLM
    model_id="deepseek-ai/DeepSeek-R1",
)
```

نفس المسار البرمجي تمامًا؛ لا تغيير في الوكلاء أو الأدوات.
