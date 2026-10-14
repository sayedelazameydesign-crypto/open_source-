# خطة توسيع القدرات: الذاكرة · الاسترجاع · أطر التنفيذ · المهارات · المراقبة

**التاريخ:** 2026-10-14 · **الحالة (محدَّثة 2026-10-14 بعد التنفيذ):** **المراحل ٠–٤ مُنفَّذة** —
‏`os_memory` + `os_know` + `SKILL.md` + تصدير OTLP + واجهة سطر أوامر؛ **المرحلتان ٥ و٦ مؤجَّلتان بقرار
مُعلَن** (لا بطاقة/توثيق عند المشغِّل، ولا مستودع كود في CI). الأرقام المقيسة في **§١٠-أ** أسفل هذا
الملف — منها رقم **لا** يُظهر تحسّنًا، ومكتوب كما هو.
**النطاق:** ما يُضاف **إلى جانب** مسار البحث/الوصول (`os_reach` — [ADR-0013](../adr/0013-reach-channels.md))
والذي يقرأ الإنترنت العام اليوم. الفئة المفقودة فعلًا في منصتنا هي **الذاكرة الدائمة** و**الاسترجاع من
مجموعات مستندات**، لا «مزيد من الوكلاء».
**المرجع:** [ADR-0016](../adr/0016-memory-and-retrieval-layer.md) — **معتمَد**، والبند ٥ (مزوّدو
الخدمة) مُعلَّق فيه صراحة.

> هذه الخطة مبنية على **فحص كل مستودع ذكرته القائمة من مصدره** (GitHub API + PyPI metadata) بتاريخ
> 2026-10-14، لا على الوصف المتداول. الفرق ظهر في **٧ أخطاء واقعية** في القائمة (القسم ١.ب).

---

## 0) الملخص التنفيذي — الأحكام

| القدرة | المرشّح | الحكم | السبب بكلمة واحدة |
|---|---|---|---|
| **ذاكرة** | **`os_memory` داخلية** (SQLite + `LocalAIEngine`) | ✅ **ابْنِ** | الفجوة حقيقية، والعقد والمخاطر ملكُنا |
| ذاكرة | Letta (`letta-ai/letta`) | 🟡 **محوّل خدمة اختياري** | خادم خارجي + SDK خفيف (٨ تبعيات)؛ لا يُستورَد في النواة |
| ذاكرة | Mem0 (`mem0ai/mem0`) | 🟡 **محوّل HTTP فقط** (بلا `import mem0`) | ٥٦ تبعية أساسية منها **`posthog` للتلّمتر** |
| ذاكرة | claude-mem | 🟡 **نمط فقط** (لا كود) | TypeScript/plugin لوكلاء السطر — الفكرة صالحة لمقطِّرة الجلسة |
| ذاكرة | OpenMemory (`mem0ai/openmemory`) | ❌ **رفض** (كمرجع) | ٩٨ نجمة، MIT، نسخة self-host تجريبية — **ليست** النظام المؤسسي الموصوف |
| **استرجاع** | RAG (درجة ٠) فوق `index_documents`/`semantic_search` | ✅ **وسِّع** | موجود ومُختبَر بلا شبكة؛ ينقصه مستودع مستندات دائم فقط |
| استرجاع | LlamaIndex | 🟡 **نمط فقط** | تقسيم `reader → node → index → retriever` يُستعار بلا تبعية |
| استرجاع | RAGFlow | 🟡 **خدمة جانبية** (HTTP) خلف نفس أرضية SSRF | Apache-2.0 فعلًا، لكن شجرة ١٧٩ م.ب ومتطلّباته ثقيلة |
| استرجاع | Crawl4AI | ❌ **رفض كتبعية** | يجرّ `torch` + `playwright`؛ والزاحف **مُضخِّم SSRF** |
| استرجاع | Graphify (`graphifyy`) | ✅ **أفضل ملاءمة في القائمة كلها** | CLI + خادم MCP (`[mcp]`)، AST محلي بـtree-sitter حتمي وبلا LLM |
| رسم معرفي | Graphiti / GraphRAG | 🟡 **محوّل MCP اختياري** | عقدنا `MCPServer` موجود؛ يبقى خارج النواة |
| **أطر تنفيذ** | CrewAI · AutoGen · LangGraph · OpenHands | ❌ **رفض كلّي كإطار تنسيق** | أيّها يستبدل `ToolRegistry` = **يُلغي تصنيف الخطر وHITL** — جوهر أمان المنصة |
| أطر تنفيذ | ما يُؤخَذ منها | 🟡 **أنماط** (القسم ٦) | بطاقات أدوار، نقاط تحقّق، أحداث sourced |
| **مهارات** | صيغة `SKILL.md` (Anthropic) | ✅ **استيراد محتوى** + بوابة مراجعة | حزمة `os_skills` تفتقر لأي استيراد/تصدير قياسي اليوم |
| مهارات | `anthropics/skills` | 🟡 **صيغة لا محتوى** | **١٨١,٠٥٥ نجمة وبلا ملف ترخيص** — تحقّق لكل مهارة قبل إعادة التوزيع |
| مهارات | `ai-workspace-archive` (٣٠ ألف ملف) | ❌ **رفض** | ٣٧ نجمة، **بلا ترخيص**، والرقم الحقيقي ١١ ألفًا لا ٣٠ ألفًا |
| **مراقبة** | **Parea** | ❌ **رفض** | **ليست مفتوحة المصدر**: منتج مستضاف، وعلى-البريميوم فقط، و`parea` على PyPI متوقّفة عند ٠.١.١ (٢٠٢٠) |
| مراقبة | تصدير **OTLP** من `RunLedger` + `EventBus` | ✅ **ابْنِ** | يستهلكه Langfuse/Phoenix/Braintrust بلا قفل مورد |
| تعدّد مزوّدين | LiteLLM | ❌ **رفض كتبعية** / 🟡 كـ**بوابة** منشورة | ١٠٦ تبعيات، و`enterprise/` برخصة تجارية؛ `OpenAICompatModel` يفي بالغرض |

**الحصيلة:** من ٢١ اسمًا في القائمة، **٣ تُدمَج فعلًا** (Graphify، صيغة `SKILL.md`، تصدير OTLP)،
و**٤ كمحوّلات خدمة اختيارية** (Letta، Mem0، RAGFlow، Graphiti/GraphRAG)، و**٧ أنماط تُستعار ideasها**،
و**٧ تُرفض** — وأربعة منها لأسباب أمنية لا تجارية.

---

## ١) أ) كيف تحقّقت — وأنت تتحقّق بنفسك

كل رقم في هذا الملف صدر عن أمرين فقط، ويمكن إعادة إنتاجهما في ٣٠ ثانية:

```bash
# الرخصة + النشاط + الأرشفة (مصدر واحد للحقيقة: سجل المستودع نفسه)
for r in letta-ai/letta mem0ai/mem0 mem0ai/openmemory infiniflow/ragflow unclecode/crawl4ai \
         Graphify-Labs/graphify crewAIInc/crewAI microsoft/autogen langchain-ai/langgraph \
         OpenHands/OpenHands BerriAI/litellm anthropics/skills thedotmack/claude-mem \
         getzep/graphiti HIDORAKAI002/ai-workspace-archive; do
  curl -sS "https://api.github.com/repos/$r" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d.get('full_name'), d.get('stargazers_count'), (d.get('license') or {}).get('spdx_id'),
      'archived='+str(d.get('archived')), (d.get('pushed_at') or '')[:10])"
done

# وزنها الحقيقي: عدد التبعيات الفعلية عند `pip install`
python3 - <<'PY'
import json, urllib.request
for n in ["mem0ai","letta-client","crawl4ai","cognee","litellm","langgraph","crewai",
          "autogen-agentchat","graphifyy","langfuse","fastmcp"]:
    d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{n}/json"))["info"]
    print(f"{n:16} v{d['version']:<10} deps={len(d.get('requires_dist') or []):<4} py={d.get('requires_python')}")
PY
```

## ١) ب) التصحيحات السبعة — القائمة التي أرسلتها فيها أخطاء واقعية

| # | الوارد في القائمة | الحقيقة المُتحقَّقة | الأثر علينا |
|---|---|---|---|
| ١ | «**Parea AI**: حزمة SDK مفتوحة المصدر للتقييم والمراقبة» | **ليست مفتوحة المصدر**: منتج مستضاف؛ On-prem على خطة Enterprise فقط. ‏`github.com/parea-ai` لم يعد يُحلّ (404)، و`parea` على PyPI عند **٠.١.١ من ٢٠٢٠** | **تُسقَط من الخطة**؛ البديل: تصدير OTLP (القسم ٨) |
| ٢ | «**AutoGen** مناسب لسير العمل البحثي» | README الخاص به يبدأ بشارة **Maintenance Mode**؛ آخر دفع كود **2026-04-15**؛ آخر إصدار `autogen-agentchat` **2025-09-30** (أكثر من سنة)؛ رخصة المستودع **CC-BY-4.0** (والكود في `LICENSE-CODE`) | لا تُبنى قدرة على مكدّس متقادم؛ الفكرة (group-chat) تُستعار فقط |
| ٣ | «**OpenMemory**: نظام مؤسسي لآلاف الوكلاء المتزامنين، ويتطلب ترخيصًا تجاريًا مدفوعًا» | `mem0ai/openmemory` = **٩٨ نجمة، MIT**، آخر دفع 2026-07-29، نسخة self-host/Docker توضيحية. الجزء المدفوع هو **Mem0 Platform** (السحابة)، لا OpenMemory | الترخيص المزعوم غير صحيح؛ والمستودع لا يقدّم ما وُصِف |
| ٤ | «**Graphify** يحول قاعدة أكواد إلى رسم معرفي» | موجود و**ضخم** (‏126,353★، Apache-2.0) — لكن **اسم الحزمة على PyPI هو `graphifyy` بحرفين y**، و`graphify` غير موجود أصلًا | **فخّ سلسلة توريد**: أي `pip install graphify` في `pyproject.toml` يُثبّت شيئًا آخر أو يفشل |
| ٥ | «**AI Workspace Archive**: ~٣٠٬٠٠٠ ملف مهارة + ٢٣ خادم MCP + ٣٬٣٠٠ قاعدة IDE» | `HIDORAKAI002/ai-workspace-archive` = **٣٧ نجمة، بلا ملف ترخيص**، ووصفه نفسه يقول **١١٬٠٠٠+** مهارة | أرشيف محتوى غير مرخّص وغير مُراجَع = **ناقل حقن على نطاق واسع**، لا مورد |
| ٦ | «**MemGPT (الآن Letta)** — بنية تحتية تُدمَج كمكتبة» | `letta-ai/letta` Apache-2.0 وحيّ، لكن حزمة PyPI باسم `letta` صارت **«Letta Code»** (CLI، **٠ تبعيات**)؛ الخادم **خدمة** والـSDK هو `letta-client` (٨ تبعيات) | الإدماج الصحيح = **عميل HTTP لخدمة** + `[memory-letta]` اختياري، لا `pip install letta` |
| ٧ | «**RAGFlow** Apache-2.0» و«**claude-mem** مستودع بايثون» | RAGFlow Apache-2.0 فعلًا ✅ (تصحيح مقبول)، لكن **لا حزمة `ragflow` حقيقية على PyPI** (ما يوجد هناك ٠.٠.١ من 2023 = اسم مُختطَف)، و`claude-mem` **TypeScript** (plugin/hooks لوكلاء المحرّر) لا مكتبة بايثون | RAGFlow = **خدمة**؛ وclaude-mem = **فكرة مقطِّرة الجلسة** (القسم ٤.٤) لا اعتماد |

> **الدرس الإجرائي:** أي بند وارد من وصف متداول لا يدخل الخطة إلا بعد `api.github.com` +
> PyPI metadata + قراءة عقد الإدماج. هذا هو نفس معيار [ADR-0013](../adr/0013-reach-channels.md).

---

## ٢) وزنها الحقيقي مقابل «عهد النواة»

عهد النواة معلن في `pyproject.toml`: **`pydantic` + `httpx` فقط**، ومجموعة اختبار **بلا شبكة وبلا
مفاتيح**. القياس الفعلي اليوم (2026-10-14، Python 3.11.2، `pip install -e ".[dev,server]"`):
**‏713 مُجتازًا · ١ مُعلَّقًا اختياريًا (psycopg) · 0 فشل** — و`README` ما زال يحمل الرقم القديم
**٦٦٥** (تُصحَّح في PR مستقل لا في هذه الخطة). جدول التبعيات هو الحكم الفاصل — لا الانطباع:

| الحزمة | تبعيات عند التثبيت الأساسي | PyPI | الحكم مقابل العهد |
|---|---|---|---|
| `langgraph` | **٦** | 1.2.15 · MIT · نشِط | الأخفّ بينها، لكنه **يزاحم `PlanningFlow`** لا يكمله |
| `letta-client` | **٨** | 1.12.1 · Apache-2.0 | مقبول **داخل extra** فقط |
| `langfuse` | **٩** | 5.0.1 · OTel | مقبول — لكن `opentelemetry-*` يكفي وأصفى |
| `fastmcp` | **٩** | 4.1.0 | لا حاجة: عقد `MCPServer` عندنا منذ `os_tools/mcp.py` |
| `mem0ai` | **٥٦** (منها `posthog`, `qdrant-client`, `openai`, `litellm`, `elasticsearch`, `chromadb`, `pymongo`, `redis`) | 2.2.1 · Apache-2.0 | **مرفوض كمكتبة**: ٥٦ تبعية + **تلّمتر افتراضي** |
| `crawl4ai` | **٥٤** (منها `torch`, `playwright`, `playwright-stealth`, `aiohttp`, `numpy`, `rich`) | 0.9.5 · Apache-2.0 | **مرفوض كمكتبة**؛ يُقبل كخدمة جانبية |
| `crewai` | **٥٧** | 1.15.28 · MIT | مرفوض (مكرر + يلفّ `ToolRegistry`) |
| `graphifyy` | ١٠٤ **في extras**؛ الأساس CLI خفيف | 0.9.90 · Apache-2.0 | يُستخدم **كأمر خارجي** لا كاستيراد → لا يلمس النواة |
| `cognee` | **١٣٧** | 1.6.4 · Apache-2.0 | مرفوض (الأثقل في فئة الذاكرة) |
| `litellm` | **١٠٦** | 1.105.1 · MIT + `enterprise/` تجاري | مرفوض داخل النواة؛ كـ**بوابة** أمام `OpenAICompatModel` |
| `openhands-ai` | **٨٥** (`openhands-sdk`: ٢١) | 1.11.0 · MIT | مرفوض — يملك حلقة تنفيذ خاصة به تتجاوز HITL |

**قاعدة مُلزِمة:** لا تدخل النواةَ تبعيةٌ `len(requires_dist) > 10` إلا داخل extra صريح، ومُغلّفة
بـ`ImportError` برسالة تسمية، ومُختبَرة بـ`MockTransport`/بدائل وهمية.

---

## ٣) أين نحن اليوم — الفجوات بأدلة من الشجرة (لا من الوصف)

| القدرة المطلوبة | الموجود فعلًا | الفجوة الدقيقة |
|---|---|---|
| ذاكرة طويلة المدى | `os_skills` (بطاقات **أنماط** مستخلَصة) · `RunLedger` (**نتائج** التشغيلات) · `RunStore` (حالة **تشغيل حي**) | **لا شيء يخزّن «حقائق» عن المستخدم/المجال عبر الجلسات**. الثلاثة كلها تخزّن شيئًا آخر: مهارة ≠ حقيقة ≠ نتيجة تشغيل |
| استرجاع من مستندات | `LocalAIEngine.index_documents` + `semantic_search` (هجين، درجات TF-IDF/مفاهيم) | المستودع **داخل الذاكرة فقط** وبلا تدبير حياة (إضافة/حذف/إذن/مصدر) ولا تقييم جودة استرجاع |
| زحف/قراءة ويب | `os_reach`: `web_read`/`rss_read`/`github_repo`/`github_search` + محوّل CLI | كافٍ للقراءة؛ **غير كافٍ للبناء على «مجموعة مستندات»** — وهذا بالضبط دور `os_know` |
| رسم بياني للمعرفة | لا شيء | `graphify query` يُعطيه قيمة فورية كأداة **بلا تبعية في النواة** (CLI خلف نفس بوابة `os_reach`) |
| تنسيق المهارات | `Skill` بحقول بايثون (`skill_id`, `triggers`, `steps_template`, `status`, `confidence`) — **بلا أي استيراد/تصدير** | `grep -rn "SKILL.md" packages/os_skills/` → **فارغ**: لا قراءة ولا كتابة لمعيار Anthropic |
| مراقبة خارجية | `RunLedger` (JSONL لكل تشغيل) + `EventBus` بأحداث مُرقّمة `seq` | لا مِرقال قياسي: كل شيء في تنسيقنا الخاص → تصدير **OTLP** يحوّله إلى لوحة قابلة للقراءة فورًا |
| تعدّد مزوّدين | `OpenAICompatModel` + `ModelRouter` بتوجيه متكيّف وسقوف تكلفة | لا يُحتاج LiteLLM؛ يُحتاج **مصفوفة عقود مزوّدين** مُختبَرة |

**استنتاج معماري واحد:** الفجوة في **طبقتَي البيانات** (ذاكرة + مجموعة مستندات)، لا في **طبقة التنسيق**.
ولهذا يبقى `os_agents` كما هو تمامًا، وتُضاف حزمتان صغيرتان فقط.

---

## ٤) الذاكرة — حزمة `os_memory` (البناء الأساسي)

### ٤.١ لماذا حزمة جديدة لا توسيع `os_skills`

`os_skills` يملك دورة حياة **مثبتة ومُختبَرة** (candidate → active → quarantined، `prune`,
`rollback`, `PermissionGrant`) مصمَّمة لـ**سلوك قابل للتنفيذ**. الذاكرة **حقائق قابلة للكذب**: لا تُنفَّذ،
لكن تُثقِل السياق ويجب أن تُفنَّد. المزج بينهما يفسد عقديهما معًا. **الصحيح:** نفس *النمط* الهندسي
(بروتوكول تخزين + مسترجع دلالي + مدير دورة حياة + أدوات مسجَّلة يدويًا)، والكود منفصل.

### ٤.٢ البنية المقترحة

```
packages/os_memory/os_memory/
├── types.py         # MemoryRecord: kind=fact|preference|episode|corpus_note
│                    #   provenance=user|sandbox|reach|import  ·  trust: asserted|verified
│                    #   status=candidate|active|quarantined|expired · ttl_at, source_run_id, sha256
├── store.py         # MemoryStore (Protocol) + InMemory + SQLite (+Postgres بنفس عقد os_skills/store.py)
├── gate.py          # بوابة الكتابة: scrub_credentials + Guardrails.scan_text + منع الأسرار + سقف طول
├── distiller.py     # RunState → episode مضغوط (offline، عبر LocalAIEngine — لا LLM مطلوب)
├── retriever.py     # recall(task): hybrid فوق LocalAIEngine + خصم ثقة/قِدَم (نفس صيغة SkillRetriever)
├── manager.py       # remember/recall/feedback/quarantine/forget/prune + سجل إصدارات
├── tools.py         # memory_remember (RiskLevel.WRITE) · memory_recall (READ_ONLY) · memory_forget (WRITE)
└── adapters/        # كلها اختيارية خلف CelaMemoryProvider — لا استيراد في المسار الافتراضي
    ├── mem0.py      # httpx → Mem0 REST المستضاف ذاتيًا (لا `import mem0ai` إطلاقًا)
    ├── letta.py     # httpx → Letta server `/v1/...` (أو `letta-client` داخل `[memory-letta]`)
    └── graphiti.py  # خادم MCP عبر عقد MCPServer القائم (list_tools/call_tool)
```

### ٤.٣ القيد الذي لا يُساوَم عليه: تسميم الذاكرة (Memory Poisoning)

أخطر ما في إضافة الذاكرة على منصتنا **ليس** الجودة، بل أن `os_reach` صار يجلب نصًا خارجيًا عدائيًا.
بدون بوابة، تصبح جملة حقن وصلت من صفحة ويب **حقيقةً دائمة** تُقرأ في كل جلسة لاحقة — أي ترقية
هجوم لمرة واحدة إلى هجوم دائم. القاعدة المقابلة، بنفس روح «يفشل مغلقًا» في `hitl.py`:

| المسار | القاعدة |
|---|---|
| **الكتابة** | تمرّ على `Guardrails.scan_text` (أنماط en/ar/zh الموجودة) + `scrub_credentials` + سقف بايت. **نصّ من `provenance=reach` لا يُرقّى إلى `active` أبدًا** — يبقى `candidate` بانتظار تأكيد صريح |
| **القراءة** | تُحقن ككتلة **غير موثوقة** موسومة `⟨untrusted-memory source=… run=…⟩` — لا في رسالة النظام، ولا بعد تعليمات، ولا كـ`tool` مسموح به ضمنيًا |
| **الحذف/التجميد** | `quarantine` فوري لكل سجل مصدره تشغيل فشل بقرار حقن؛ و`prune` بـTTL افتراضي ٩٠ يومًا للـ`candidate`، بلا نهاية لـ`verified` |
| **التدقيق** | حدثان على الناقل: `memory_written` / `memory_recalled` (بالمعرّفات والدرجات فقط، لا النص) + عمود `memory_ids` في `LedgerEntry.metadata` |
| **الصندوق** | `python_execute` **لا يلمس الذاكرة**؛ الأداة تُنفَّذ على المضيف عبر `ToolRegistry` فقط — نفس حدود جدول «حدود الثقة» في [REACH_PLAN](REACH_PLAN.md) §٤ |
| **MCP** | `memory_*` تُصدَّر عبر `LocalMCPServer` كما هي، مع `ToolFilter` (allow-list) افتراضي يُبقي `memory_forget` خارجًا حتى يُصرَّح |

### ٤.٤ مقطِّرة الجلسة (من claude-mem — الفكرة، والكود لا)

`claude-mem` (‏100,628★، Apache-2.0، TypeScript) يعمل كـ**hooks** لوكلاء المحرّر: يلتقط ما جرى، يضغطه،
ويحقنه لاحقًا. الجزء المفيد لنا **الضغط**، وهو موجود أصلًا بشكل بدئي: `SkillExtractor.extract(RunState)`
يولّد بطاقة. الخطوة التالية الموثوقة:

* `distiller.py` يقرأ `RunState` + `LedgerEntry` (لا الشبكة) → **episode** من ٣–٨ أسطر + كيانات من
  `LocalAIEngine.extract_entities` — **حتمي، offline، وبلا LLM**.
* حقنه في `Platform.run` يتم **كما يُحقن الـskill فعلًا**: عبر `skill_manager` duck-typed في
  `os_orchestration/platform.py:48-53`، بإضافة `memory_manager` بنفس النمط — **بلا استيراد متبادل**
  بين الحزم، فيبقى «`os_tools` و`os_agents` لا يعرف أحدهما الآخر» صحيحًا.
* القياس الإلزامي قبل الادّعاء: **توفير/إضافة التوكنز** في `os_evals` (`--ab-skills` يقيس التوكنز فعلًا).

### ٤.٥ محوّلات مزوّدي الذاكرة — شروط القبول الثلاثة

1. **HTTP/MCP فقط، لا استيراد.** `import mem0ai` = ٥٦ تبعية + `posthog` يتصل بالمضيف من تلقاء نفسه
   فينتهك «بلا شبكة» ويجلب تسريبًا للبيانات. المحوّل يكلّم **خادم Mem0** بـ`httpx`.
2. **خلفية `EgressPolicy` صريحة** (`base_url` من متغيّر بيئة، مضاف للمفوّذين حصرًا لأدوات `memory_*`).
3. **حالة `off` شريفة:** تعذّر الوصول → `ChannelHealth(state=off, reason=…)` بنفس عقد `os_reach`،
   **وتعطّل ذاكرة-الخدمة لا تعطيل الذاكرة المحلية** (انحدار إلى `SQLite`، لا صمت).

---

## ٥) الاسترجاع — ثلاث درجات، وRAGFlow كخدمة لا كمكتبة

### ٥.١ التدرّج

| الدرجة | ما هو | التبعيات | متى تُستخدَم |
|---|---|---|---|
| **T0** | `LocalAIEngine` الهجين (TF-IDF جذور + مفاهيم) — **موجود ومُختبَر** | **٠** | الافتراضي الدائم؛ يعمل في CI وبلا مفاتيح |
| **T1** | `os_know`: مستودع مستندات دائم (SQLite) + تقطيع بالهيكل + مصادر مُوقَّعة + **تقييم استرجاع** | `pydantic`+`httpx` | الفجوة الحقيقية في منصتنا |
| **T2** | محرّكات خارجية كـ**قنوات**: RAGFlow / Graphiti / GraphRAG / Graphify | extra `[know]` | حين تتجاوز المجموعة قدرات T0/T1 |

### ٥.٢ `os_know` — الحزمة الوحيدة الجديدة الثانية

```
packages/os_know/os_know/
├── corpus.py     # Corpus(corpus_id, name, acl, provenance) + Document(id, sha256, mime, source_uri)
├── chunking.py   # تقطيع بالهيكل (رؤوس Markdown، دوال Python via ast، فقرات نص) — حتمي
├── store.py      # KnowStore Protocol: put/delete/list + hnsw-ish الفهارس عبر التوكنز (بلا FAISS في النواة)
├── retrieve.py   # query(text, *, corpus_ids, top_k, min_score) → hits بالدرجات + المصدر + sha256
├── eval.py       # recall@k / MRR على مجموعة ذهبية JSONL — يقيس، لا يروِّج
└── channels.py   # محوّلات T2 بنفس عقد ReachChannel (can_handle/probe) + EgressPolicy قبل كل قفزة
```

* **التغذية:** `python_execute` (بلا شبكة) يستخرج النص من ملفات **مرفوعة إلينا**؛ والصيغ المعقّدة
  (PDF/DOCX) تمرّ عبر `LocalAIEngine.document_ocr` الموجود، أو أداة T2 عند توفرها.
* **مبدأ LlamaIndex المستعار (بلا استيراده):** `reader → node → index → retriever` كأربع مراحل **منفصلة
  وقابلة للإبدال**؛ عندنا أربع دوال لا مكتبة.
* **قاعدة المصدر:** كل hit يحمل `source_uri` + `sha256` + `retrieved_at`، وتُحقن النتائج كـ**اقتباسات غير
  موثوقة** — أي نتيجة استرجاع من مصدر `reach` تعدّي `Guardrails` قبل دخولها السياق (نفس بوابة §٤.٣).

### ٥.٣ لماذا **لا** Crawl4AI — ولماذا ليس «زاحفًا» أصلًا في منصتنا

| الاعتراض | التفصيل |
|---|---|
| الحجم | ٥٤ تبعية أساسية منها **`torch`** و`playwright` و`playwright-stealth` | 
| الأمان | الزاحف **مُضخِّم SSRF**: يتبع روابط لا نهائية من داخل نص عدائي. لدينا `SSRFGuard` يفحص **قبل كل قفزة إعادة توجيه** — الزاحف يفتح الباب من جهة أخرى |
| الحدود | `BrowserTool` + `web_read` يغطّيان ٩٥٪ من حالة استخدام «اقرأ هذه الصفحة»؛ الفرق الحقيقي هو **الزحف الجماعي**، وهو بالضبط ما نرفض تحميل مسؤوليته للنواة |
| التسوية | إن أصرّ المالك: **خادم Crawl4AI جانبي** (`docker`) يُستهلَك كقناة T2 بـ`httpx`، بسقف عمق/صفحات/زمن، و**`EgressPolicy` تُطبَّق على قائمة الروابط المُدخَلة لا على مخارج الخادم** — مع إعلان صريح في «حدود صادقة» أن **عزل الشبكة عن الخادم الجانبي ليس ملكنا** |

### ٥.٤ Graphify — الاستثناء الذي يستحقّ الاستيراد السريع

`Graphify-Labs/graphify` (‏126,353★، **Apache-2.0**، نشِط) هو المرشّح الوحيد الذي يلائم ما بنيناه فعلًا:

* **تحليل الكود محلي وحتمي:** AST بـ**tree-sitter**، «بلا LLM، ولا شيء يغادر الجهاز» — يطابق عهدنا تمامًا.
* **واجهة مألوفة لدينا:** `graphify query "<سؤال>"` و`graphify path A B` كـ**CLI** — و`os_reach` يملك
  محوّل CLI جاهزًا (`CLIChannelAdapter`: argv بلا shell، قائمة وسطاء، ميزانية بايت متدفقة، تدقيق).
* **وخادم MCP اختياري:** `graphifyy[mcp]` → يمرّ عبر عقد `MCPServer` القائم بلا أي كود جديد.
* **شرطان:** (١) التثبيت بـ**SHA مثبَّت من GitHub** مثل `[reach]`، لا من PyPI باسم خاطئ؛ (٢) اسم PyPI
  الصحيح **`graphifyy`** — وتوثيق ذلك في `pyproject.toml` لتفادي الاختطاف.
* **حدّ صادق:** الحزمة ٣٠٨ م.ب ومجلّدات extra فيها ١٠٤ تبعية؛ **لا** تُثبَّت في النواة ولا في CI الاختباري.

---

## ٦) أطر الوكلاء — الرفض، وماذا نأخذ

**الرفض ليس ذوقيًا.** كل إطار من الأربعة يريد أن يكون **حلقة التنسيق العليا**: `crewai` يملك
`Agent/Task/Crew`، ‏`langgraph` يملك `StateGraph`، ‏`autogen` يملك `GroupChat`، ‏`OpenHands` يملك
`Action/Observation` loop. عندنا الحلقة مغلقة بأربع خصائص لا تُستنسَخ:

1. **`Guardrails.classify()` لكل نداء أداة** → `RiskLevel` → `requires_approval()` → **HITL يفشل مغلقًا**.
2. **`BudgetConfig`** (سقوف خطوات/توكنز/تكاليف) + كاسر رُكود + `ModelReviewer`.
3. **`EgressPolicy` قبل كل نداء شبكة وكل قفزة إعادة توجيه.**
4. **اختبارات حتمية بلا شبكة** — ‏713 مُجتازًا اليوم؛ أي إطار يستورد `litellm`/`openai` يُسقط هذا الرقم
   (وليس فقط يبطّئه: النواة تفقد خصيصة «لا نموذج مطلوب»).

إدخال إطار تنسيق آخر = مسار تنفيذ **لا يمرّ على ١–٣**. هذا لا يُصلَح بغلاف.

| الإطار | الحالة المُتحقَّقة منها | ما نأخذه (نمطًا فقط) | أين يذهب |
|---|---|---|---|
| CrewAI (‏59,684★، MIT، 57 تبعية) | نشِط | بطاقات دور/هدف/قيود معلَنة | `PlanStep` + `Skill.preconditions` — لا كود |
| LangGraph (‏43,254★، MIT، **٦** تبعيات) | نشِط | `interrupt()`/checkpointer كحالة قابلة للاستئناف | **مُنفَّذ عندنا فعلًا**: `RunStore` + `after_seq` (ADR-0002/0004) — نأخذ الاختبارات: انقطاع/استئناف عبر ناقلين |
| AutoGen (‏61,397★، **Maintenance Mode**، آخر دفع 2026-04-15) | متقادم | محادثة جماعية بأدوار ناقد | `ModelReviewer` عندنا — لا اعتماد على مكدّس متقادم |
| OpenHands (‏90,874★، MIT، `openhands-sdk` ٢١ تبعية ويعتمد `litellm`) | نشِط | حدث-مصدَر (event-sourced) لأدوات تنفيذ الكود | `EventBus` عندنا؛ **وإن طُلب وكيل برمجة كامل: يُشغَّل كـ`Tool` داخل الصندوق المعزول، لا كمُنسِّق** |

---

## ٧) المهارات — `SKILL.md` كمحتوى، مع بوابة مراجعة

**فجوتنا الدقيقة:** `os_skills` يولّد بطاقاته من التشغيلات ولا يستورد شيئًا. معيار **Agent Skills**
(‏`anthropics/skills`: ١٨١,٠٥٥★) صار lingua franca — فنجعله **واجهة استيراد/تصدير** لا تبعية:

```
os_skills/skill_md.py     # parse/format: SKILL.md ↔ Skill
  frontmatter → name, description, license?, allowed-tools → Skill.tools/allowed_tools
  body        → Guidance  (يُحقَن كنص، لا كخطوات قابلة للتنفيذ)
  Skill.source_run_id=None + Skill.provenance="import" + status="candidate" دائمًا عند الاستيراد
```

**القواعد الأربع للاستيراد** (كلّها نتيجة فحص، لا تحفّظ نظري):

1. **الترخيص أوّلًا.** `anthropics/skills` **بلا ملف LICENSE** (فحص `/repos/anthropics/skills/license` →
   `NO LICENSE FILE`)؛ الكاتالوجات المرخّصة (‏`VoltAgent/awesome-agent-skills`: MIT،
   ‏`sickn33/agentic-awesome-skills`: MIT) لا تعني أن **المحتوى** مرخّص. ⇒ حقل `license` إلزامي في
   الاستيراد؛ غائبه = رفض، أو استيراد «للاستخدام الشخصي داخل هذه النسخة» مع وسم ظاهر.
2. **مهارة مستورَدة ≠ صلاحية.** خطواتها تبقى تحت `PermissionGrant` الحالي؛ `allowed-tools` في
   frontmatter يُقرأ **كتقييد لا كمنحة** (منحة الأداة لا تأتي من ملف نصي).
3. **بوابة التقييم.** أي مهارة مستورَدة تدخل `active` فقط بعد **تشغيلَين ناجحَين** في
   `os_evals` (نفس عتبة skill القائمة: `>=2 successes`)، وإلا بقيت `candidate`؛ الانحدار التلقائي موجود.
4. **تحذير الأرشيفات الضخمة.** `HIDORAKAI002/ai-workspace-archive` (٣٧★، **بلا ترخيص**، «١١ ألف مهارة»
   **لا** ٣٠ ألفًا كما يُتداول) — **مرفوض**: ملف `SKILL.md` غير مُراجَع هو **برومبت حقن مُغلَّف كبودي**.
   الاستيراد جملةً من أرشيف غير مُراجَع = تسليم سياق الوكلاء لجهة مجهولة.

---

## ٨) المراقبة والتقييم — تصدير OTLP، لا SDK لمورد

**البديل عن Parea** = طبقة تصدير معيارية فوق الموجود، لأن لدينا ما ينقص الآخرين:

| عندنا اليوم | ما يُضاف | التكلفة |
|---|---|---|
| `LedgerEntry` (run_id, mode, outcome, steps_used, tokens_used, replans, reviews, tool_calls, tool_failures, duration_ms) | **Trace** لكل تشغيل: span جذر + **span لكل خطوة** من `step_started/step_finished` | `[otel]` ‏`opentelemetry-{api,sdk,exporter-otlp-proto-http}` (~٤ تبعيات) |
| `EventBus` بـ`seq` ومهلة replay | **Events** كـspan events على نفس التسلسل — بلا ناقل ثانٍ ولا حالة مزدوجة | مُحوِّل واحد `os_server/otel.py` |
| `os_evals` + A/B للمهارات | **Dataset export** بصيغة OpenAI/`experiments` ليبتلعها Langfuse/Phoenix/Braintrust | `--out otel://` ‏`--dataset` |

* **لا قفل مورد:** Langfuse (‏35,813★، نواته MIT و`ee/` تجاري؛ SDK بايثون ٥.٠.١ بـ٩ تبعيات) وArize Phoenix
  وBraintrust كلها **مستهلكون لـOTLP**. نصدّر المعيار، لا رخصة بائع.
* **Honesty gate:** التصدير **opt-in** (`CELA_OTEL_ENDPOINT`)، ويخضع لـ`scrub_credentials` —
  **لا يغادر أي نص خام من مصدر `reach`**؛ الافتراضي `off` ويظهر في `doctor`.
* **LiteLLM:** مرفوض داخل النواة (١٠٦ تبعيات، و`enterprise/` تحت رخصة تجارية — نصّ `LICENSE` عنده
  «Portions … under the license defined in enterprise/LICENSE» حرفيًا). الصواب عندنا: **بوابة LiteLLM
  منشورة أمام `OpenAICompatModel`** كـ`base_url` — لا تغيّر سطرًا في الوكلاء (نفس ما يفعله `docs` الربط
  بـvLLM/Ollama اليوم). ومقابل ذلك نضيف **مصفوفة عقود مزوّدين** مُختبَرة بـ`MockTransport`
  (أسماء الأدوات، `stream`، `tool_choice`، أخطاء ٤٢٩، مهلات) — وهذا بند مفقود فعلًا.

---

## ٩) مخاطر جديدة تُدخلها هذه الخطة (تُسجَّل في «حدود صادقة» لا في الحاشية)

1. **تسميم الذاكرة** — أخطر من حقن جلسة واحدة: دائم ويُقرأ في كل جلسة. الحلّ في §٤.٣؛ وإن لم ينفَّذ
   **كلّه** فالحزم **تُغلَق**، لا تُفعَّل ناقصة.
2. **الحلقة الذاتية (loopback) ممنوعة أصلًا:** `SSRFGuard` = «لا loopback» (سطر ٣ من `os_tools/egress.py`)،
   بينما كل مزوّد ذاكرة/RAG self-host يعمل على `127.0.0.1:8000`. ⇒ يلزم **استثناء صريح ضيّق**:
   `EgressRule(host="127.0.0.1", ports={8000}, tools={"memory_mem0"}, action="allow", description=…)`
   مفحوصة **على أدوات `memory_*`/`know_*` وحدها** — لا على المتصفح ولا على `os_reach`، ولا wildcard على
   المنفذ. (السابقة عندنا في `allow_single_label_hosts`: موضع واحد قابل للتجاوز الصريح خير من قاعدتين.)
3. **تلّمتر خفيّ:** `posthog` في Mem0، و«phone home» في محرّكات RAG → منعها سبب كافي لاختيار HTTP بدل
   الاستيراد؛ وإن استُوردت مكتبة، `CELA_*` يُمرَّر عبر `env` قائمة سماح لا وارثة.
4. **سلسلة التوريد:** فخّ `graphify`/`graphifyy`؛ وكل `[extra]` خارج PyPI **مثبَّت بـSHA** (نفس
   `[reach]` اليوم) مع `sha256` في `VENDORED`-style memo.
5. **الترخيص الملوَّث:** استيراد **نصوص** من `anthropics/skills` (بلا ترخيص) أو محتوى `ai-workspace-archive`
   (بلا ترخيص) إلى مستودع **MIT** ⇒ رفض، أو مصدر+ترخيص لكل ملف.
6. **ادّعاء قدرة غير موجودة:** قاعدة ADR-0013 تبقى: قناة بلا خلفيتها → `off` بسبب مكتوب، لا «نجاح وهمي».

---

## ١٠) خطة التنفيذ — بمعايير خروج قابلة للإغلاق

> **القاعدة الحاكمة لكل مرحلة:** ‏`pytest -q` يبقى **أخضر بلا شبكة وبلا مفاتيح**، ‏`ruff` نظيفًا،
> والتثبيت الافتراضي `pip install -e ".[dev,server]"` ينجح **بلا أي** حزمة جديدة. لا مرحلة تنتقل
> قبل كتابة الاختبارات المذكورة.

| المرحلة | النطاق | الملفات | معيار الخروج (دليل، لا نيّة) |
|---|---|---|---|
| ✅ **٠ — اعتماد** | لا كود. إقرار الأحكام + أسئلة المالك (القسم ١٢) | `docs/INTEGRATIONS_PLAN.md`, `adr/0016-*.md`, `docs/ROADMAP.md` | ADR مُعتمد أو مرفوض صراحةً؛ لا تبعية مضافة |
| ✅ **١ — `os_memory` محلية** | العقد + بوابة الكتابة + `distiller` + recall؛ بدون أي provider | `packages/os_memory/{types,store,gate,distiller,retriever,manager,tools}.py` + `tests/test_memory.py` | +~٤٠ اختبارًا offline؛ **اختبار هزيمة**: نص حقن من `provenance=reach` → لا يُخزَّن `active`، والحقن داخل السياق موسوم `untrusted`؛ حقن `memory_manager` في `Platform.run` بلا استيراد متبادل |
| ✅ **٢ — `os_know` T0/T1** | مستودع مستندات دائم + تقطيع + `recall@k`/`MRR` | `packages/os_know/*` + `tests/test_know.py` + `tests/fixtures/golden.jsonl` | رقم منشور: **recall@5 ≥ خط أساس TF-IDF الخام** على مجموعة ذهبية ≥٢٠ سؤالًا، وإلا يُعلَن «بلا تحسّن» في ROADMAP |
| ✅ **٣ — مهارة `SKILL.md`** | `skill_md.parse/format` + `license` إلزامي + حالة `candidate` | `packages/os_skills/os_skills/skill_md.py`, `tests/test_skills_skillmd.py` | ذهاب-إياب (round-trip) نص↔بطاقة؛ **رفض** مهارة بلا ترخيص؛ **رفض** تنفيذ `allowed-tools` كمنحة؛ `os_evals --ab-skills` يقيس التوكنز قبل/بعد |
| ✅ **٤ — تصدير OTLP** | `os_server/otel.py` ‏opt-in؛ spans من `EventBus` + `LedgerEntry` | `packages/os_server/os_server/otel.py`, `tests/test_otel.py` | بلا نقطة نهاية = **صفر** مقابلات شبكة (يُثبَت بمراقبة httpx)؛ لا أسرار في أي span (فحص بـ`scrub_credentials` vectors) |
| ⏸ **٥ — محوّل Graphify** (مؤجَّل بقرار) | قناة T2/CLI فوق `os_reach` + خادم MCP إن طُلب | `packages/os_know/os_know/channels/graphify.py`, `[know]` ‏extra مثبَّت بـSHA | يعمل **بلا الحزمة** ⇒ `off` بسبب؛ argv فقط (لا shell)؛ سقف ٢٥٦ KiB/٦٤ KiB؛ تدقيق لكل نداء؛ **مرفوض داخل الصندوق** |
| ⏸ **٦ — محوّلات الخدمة** (مؤجَّل بقيد المشغِّل) | `adapters/{mem0,letta,graphiti}.py` خلف `CELA_MEMORY_PROVIDER` | ك above + `docs/MEMORY_PROVIDERS.md` | **كل** محوّل بـ`MockTransport` عقودًا؛ استثناء loopback ضيّق ومُختبَر (منفذ واحد، أداة واحدة)؛ **انحدار** إلى T0 عند تعذّر الوصول بلا فقدان حالة |
| ✅ **٧ — ما لا نفعله** | لا Crawl4AI، لا LiteLLM، لا Parea، لا إطار تنسيق، لا CrewAI/AutoGen | — | يُثبَّت رفضُه بسطر في `docs/ROADMAP.md` لا بصمت |

---

## ١٠-أ) ما قيس فعلًا (2026-10-14) — لا نيّات هنا، أرقام

**الحجم:** ‏`pytest -q` → **٨٣٥ ناجحًا، ١ متخطًّى** (كان ٧١٣ قبل هذه الطبقات؛ ‏+١٢٢ اختبارًا).
التوزيع: `test_memory.py` ٣٨ · `test_know.py` ٢٥ · `test_memory_platform.py` ١١ ·
`test_skills_skillmd` (`test_skills_skill_md.py`) ٢٢ · `test_otel.py` ١٧ ·
`test_cli_memory_know_telemetry.py` ٩. كله **offline بلا مفاتيح**، و`ruff check` +
`ruff format --check` نظيفان، و`pip install -e ".[dev,server]"` ينجح **بلا تبعية جديدة**.

**معيار خروج المرحلة ٢ — كما خرج، لا كما أُعيد كتابته:**

| مجموعة ذهبية | recall@5 | MRR | empty | مقابل خط الأساس |
|---|---|---|---|---|
| ٢١ سؤالًا / ٦ مستندات ماركداون | **1.0** | **0.976** | 0.0 | مذكور أدناه |
| ٣٠ سؤالًا / متْن طويل متقاطع الأطوال | **1.0** (مقطَّع) | — | — | **1.0** للمستند الكامل ⇒ **لا تحسّن ترتيب** |

الفرق المقيس الحقيقي في المجموعة الثانية هو **اقتصاد السياق**: ‏**28,293** حرفًا حقنًا مقابل
**148,720** للتقطيع بالمستند الكامل — **٥٫٣× أقل** عند نفس recall. لذلك ثبَّت الاختبار معيارين:
سقف ترتيب لا يُكسر (‏`recall ≥ 0.8`، `MRR ≥ 0.6`) ومعيار اقتصاد قابل للفشل (‏`≥ 3×`).
**لو قُرئت هذه الفقرة بعد قراءة «التقطيع يحسّن الاسترجاع» فهي قراءة خاطئة:** لم يحسّنه هُنا.

**بوابة التسميم (المرحلة ١) — السلوك المُختبَر، لا الموصوف:** رفض عند الكتابة (‏`scan_text` +
`contains_secret` على **النص الخام قبل** التنقية، لأن `scrub_credentials` يستبدل لا يحذف) ·
سقف ٢٠٠٠ حرف · `sanitize_boundaries` · `provenance ∈ {reach, import}` ⇒ `candidate` أبديّ لا يُرقَّى
تلقائيًا · رفض وجه تسريب (`169.254.169.254`, `localhost`, `::1`, `*.internal`) للنص غير الموثوق فقط ·
الحقن داخل `<untrusted-memory>` مع `id=mem_…` ومنشأ · **خطأ الذاكرة لا يُفشل تشغيلًا** (قراءة وكتابة
محميّتان، والقراءة تُسجَّل كتحذير `EventType.LOG` لا كحدث صامت).

**الواجهة (لا شيء كان موجودًا قبلها):**

```bash
cela memory remember "…" --key deploy.target --provenance user   # ← يُطبع status=candidate دائمًا
cela memory recall "أي نظام تشغيل نستخدم؟" [--json] · cela memory list · stats · forget <id> --yes
cela know index ./docs --corpus repo · cela know search "…" --corpus repo · cela know eval --golden g.jsonl
cela telemetry export [--run-id …] [--out trace.json] [--send]  # ‏--send يستلزم CELA_OTEL_ENDPOINT
```

**رموز الخروج مقصودة:** ‏**0** نجح · **1** رُفض أو لا شيء للعمل عليه · **2** خطأ إعداد (لا مخزن/لا
نقطة نهاية). الفرق بين ١ و٢ هو ما يجعل `cela telemetry export --send` في خط إنتاج لا يخلط «لم نُرسل»
عن «لا يوجد ما نُرسل إليه».

**ما لم يُنفَّذ (بلا تجميل):** محوّل Mem0/Letta/Graphiti (‏§٦) — مؤجَّل لأن قَيْد المشغِّل «بلا بطاقة
ائتمان» يسقط السحابة، ولم تُكتب استثناءات `EgressRule` لـloopback؛ وقناة Graphify (‏§٥) — لا مستودع
كود في CI يستدعيها. الاثنتان **خياريتان** ولا تُعلَّق عليهما أي قدرة مُعلَنة.

**تقدير الجهد:** ١ ≈ ٣–٥ أيام · ٢ ≈ ٣–٤ · ٣ ≈ ٢ · ٤ ≈ ٢ · ٥ ≈ ١–٢ · ٦ ≈ ٤–٦ لكل مزوّد.
المرحلتان ١+٣ وحدهما تُغلقان أكبر فجوة («لا ذاكرة عبر الجلسات») بأصغر سطح تعرّض.

---

## ١١) مصفوفة الترخيص — معيار MIT ↔ مُرشَّحات الإدماج

| المرشّح | الرخصة (متحقَّق 2026-10-14) | متوافق مع MIT؟ | القيد العملي |
|---|---|---|---|
| Letta · Mem0 · RAGFlow · Crawl4AI · Graphify · Graphiti · Cognee | **Apache-2.0** | ✅ | لا إعادة توزيع لملفاتها في مستودعنا؛ تثبيت اختياري فقط |
| LlamaIndex · LangGraph · CrewAI · GraphRAG · OpenHands | **MIT** | ✅ | متوافق، لكن **الرفض وظيفي** (§٦) لا ترخيصي |
| LiteLLM | **MIT + `enterprise/` تجاري** (نص `LICENSE` حرفيًا) | ⚠️ | يُستهلك كـ**بوابة منفصلة**، لا كاعتماد |
| Langfuse | **MIT للنواة + `ee/`** | ⚠️ | نصدّر OTLP؛ لا نستورد منصّته |
| `mem0ai/openmemory` | **MIT** | ✅ | مرفوض لأنّه نسخة تجريبية لا «نظامًا مؤسسيًا» |
| `anthropics/skills` | **لا يوجد ملف ترخيص** | ⚠️❗ | نصوصٌ غير مرخّصة ⇒ **صيغة** فقط، وترخيص لكل محتوى مستورَد |
| `HIDORAKAI002/ai-workspace-archive` | **لا ترخيص** | ❌ | **مرفوض** قانونيًا وأمنيًا |
| `microsoft/autogen` | **CC-BY-4.0 للمستودع** + `LICENSE-CODE` للكود | ⚠️ | لا نسخ من نصوصه التوثيقية إلى مستودعنا؛ حالة **صيانة** تُسقطه أيضًا |

---

## ١٢) قرارات مطلوبة منك (قبل سطر كود واحد)

| # | السؤال | الخيارات | توصيتي |
|---|---|---|---|
| ١ | **ما الفجوة المؤلمة أولًا؟** | (أ) ذاكرة عبر الجلسات · (ب) استرجاع من مستنداتك · (ج) رسم معرفي للأكواد | **(أ)** — ثم (ب)؛ (ج) أرخصها لكن أقلّها أثرًا على تجربة الاستخدام |
| ٢ | **نطاق الذاكرة** | (أ) لكل فضاء/مستخدم · (ب) لكل وكيل · (ج) عامة لكل النظام | **(أ) + عزل صريح**: الذاكرة المشتركة هي أسرع ناقل تسريب بين العملاء |
| ٣ | **هل الخدمة الجانبية مسموحة في النشر؟** | (أ) مكتبات فقط · (ب) مكتبات + حاويات | **(ب)** — بلا حاويات تسقط Letta/Mem0/RAGFlow، وتبقى T0 فقط |
| ٤ | **سياسة استيراد المهارات** | (أ) رسمية فقط · (ب) أي مصدر + مراجعة · (ج) أي مصدر تلقائي | **(ب)** — و(ج) **لا**: مهارة غير مُراجَعة = برومبت حقن دائم |
| ٥ | **من يملك بوابة الذاكرة؟** | (أ) `MemoryStore` عندنا · (ب) `os_skills` يوسَّع · (ج) مخزن متجهات عام | **(أ)** (§٤.١)؛ (ب) يفسد عقدين، و(ج) يجرّي FAISS إلى النواة |

---

## ١٣) ما لا نَعِد به (حتى لا يُقرأ هذا الملف تسويقًا)

* **لا** رقم جودة «ذاكرتنا أفضل» قبل مخرج المرحلة ٢ (`recall@k`/`MRR`) و`os_evals`.
* **لا** «عزل شبكة كامل» للمسارات التي تتصل بخادم جانبي على المضيف — العزل هناك **ليس ملكنا**.
* **لا** دعم أي مزوّد ذاكرة لم يُختبَر بعقود `MockTransport` في CI.
* **لا** استيراد مهارة واحدة بلا ترخيص معلوم ومصدر مُثبَّت — ولو كان المستودع صاحب الـ١٨١ ألف نجمة.
* **لا** إطار تنسيق جديد: حلقتنا مُقفَلة بالأمان، وكل بديل يعني إعادة بناء HITL من الصفر.

---

**خاتمة عملية:** القائمة الأصلية فيها ٢١ اسمًا، لكن ما يَصلح لمنصتنا تحديدًا هو
**حزمة ذاكرة محلية بعقد أمان صريح** + **مستودع مستندات دائم** + **استيراد/تصدير `SKILL.md`** +
**تصدير OTLP**؛ والباقي إمّا خدمة جانبية اختيارية (Letta/Mem0/RAGFlow/Graphiti/Graphify) أو نمط
يُستعار بلا كود. **ابدأ بالمرحلتين ١ و٣**: أقصر طريق إلى فرق ملموس، وأصغر سطح تعرّض ممكن.
