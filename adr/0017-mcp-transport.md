# ADR-0017: طبقة MCP (`os_mcp`) — نقل حقيقي في الاتجاهين، وكل أداة خارجية تمرّ على الحواجز قبل أن تُسجَّل

**التاريخ:** 2026-10-14 · **الحالة:** مقبول ومُنفَّذ (عميل stdio + Streamable HTTP، جسر إلى `ToolRegistry`، خادم MCP فوق السجل، CLI، أسطح HTTP)
**المصدر:** مواصفة Model Context Protocol، مراجعة `2025-06-18` (مع التفاوض نزولًا إلى `2025-03-26` و`2024-11-05`)

## السياق

كان `README.md` و`docs/ARCHITECTURE.md` و`docs/INTEGRATIONS_PLAN.md` تَصِف المنصة بأنها «تدعم MCP»،
بينما الموجود فعليًا هو `os_tools/mcp.py`: **عقد داخل العملية** (`LocalMCPServer`: `list_tools` /
`call_tool` فوق `ToolRegistry`) بلا أي نقل. أي:

* لا يمكن للوكيل استدعاء **خادم MCP خارجي** واحد (ملفات، قواعد بيانات، GitHub، …) — وهي الفئة
  التي تُقاس بها منصات الوكلاء اليوم.
* لا يمكن لأي مضيف MCP (Claude Desktop, Cursor, …) أن يستعمل **أدوات هذه المنصة**
  (sandbox، متصفح، الذكاء المحلي) من خارجها.
* بندان من المرحلة 6 في `docs/ROADMAP.md` (`Graphify` عبر MCP، و`graphiti` «عبر عقد MCPServer
  القائم») يعتمدان **ضمنيًا** على نقل غير موجود.

وفي الوقت نفسه يفرض `ROADMAP.md` و`ADR-0016` قيودًا صريحة: لا تبعيات نواة جديدة (نكتفي
بـ`pydantic` + `httpx`)، لا شبكة ولا مفاتيح في الاختبارات، لا إطار خارجي يستبدل حلقة
`Guardrails → RiskLevel → HITL`، وكل سطح جديد مغلق افتراضيًا.

## القرار

1. **حزمة جديدة `os_mcp` بلا تبعيات جديدة.** JSON-RPC 2.0 فوق `asyncio` subprocess (stdio) وفوق
   `httpx` (Streamable HTTP بنمطَي `application/json` و`text/event-stream`). لا `mcp` SDK ولا
   `fastmcp`: النقل بضع مئات من الأسطر، والـSDK الرسمي يجرّ تبعيات ويملك نموذج جلسات لا يمرّ على
   `EgressPolicy` عندنا.
2. **الأداة الخارجية لا تُسجَّل قبل أن تمرّ على الحواجز.** `bridge_tools` يمرّر وصف كل أداة ومخططها
   (`review_text`) على `Guardrails.scan_text`؛ أي تطابق مع أنماط الحقن (en/ar/zh) يضع الأداة في
   **حجر** (`quarantined`) فلا يراها النموذج أصلًا، ويُبلَّغ عنها في `cela mcp doctor` و`/api/mcp/servers`.
   أسماء الأدوات تُقصّ إلى ≤128 حرفًا، والأوصاف إلى ≤1024، والمخرجات إلى `max_chars` (20k افتراضيًا).
3. **الخطر الافتراضي لكل أداة خارجية هو `dangerous`** (أي HITL قبل كل نداء) ما لم يخفّضه المالك
   صراحة في الإعداد (`risk` للخادم، أو `tool_risk` لأداة بعينها). إشارة الخادم نفسه
   (`annotations.destructiveHint`/`readOnlyHint=false`) **ترفع** الخطر ولا تخفّضه أبدًا — الخادم
   طرف غير موثوق ولا يُمنح حق تصنيف نفسه.
4. **الاسم في السجل مُسبَّق دائمًا:** `mcp_<server>_<tool>` (≤64 حرفًا مع لاحقة hash عند الحاجة)،
   والوصف يبدأ بـ`[MCP:<server>]`. التصادم يُحلّ بلاحقة، **لا بالكتابة فوق أداة قائمة** — لا يستطيع
   خادم MCP أن يستبدل `python_execute`.
5. **الشبكة والبيئة:** خوادم HTTP تمرّ على `EgressPolicy` تسمح بأصل الخادم المُعدّ **فقط** فوق أرضية
   `SSRFGuard`؛ خوادم stdio تنطلق ببيئة بيضاء (`PATH`, `HOME`, `LANG`, … + ما أُعلن في `env` مع توسيع
   `${VAR}`) لا ببيئة العملية كاملة، وترفض العمل داخل الصندوق (`CELA_SANDBOXED=1`). ملف الإعداد يُرفض
   إن كان قابلًا للكتابة من المجموعة/العالم. الترويسات السرية تُحلّ وقت الطلب ولا تُطبع
   (`describe()` يُظهر أسماء المتغيرات فقط — ADR-0011).
6. **مغلق افتراضيًا:** لا يُقرأ أي إعداد ولا تُطلق أي عملية إلا مع `CELA_MCP_ENABLE=1`؛ المسار
   الافتراضي `~/.config/cela/mcp.json` (يُبدَّل بـ`CELA_MCP_CONFIG`) بصيغة `mcpServers` المألوفة
   (نفس شكل إعداد Claude Desktop/Cursor) مع مفاتيحنا الإضافية (`risk`, `tool_risk`, `tools`,
   `timeout`, `max_chars`). نوع `sse` القديم يُقرأ ويُعطَّل بسبب مُعلَن بدل أن يفشل بصمت.
7. **الاتجاه الآخر بنفس السياسة:** `MCPRegistryServer` يعرض `ToolRegistry` لأي مضيف عبر stdio؛ كل
   نداء يمرّ على `Guardrails.screen` ثم تصنيف الخطر ثم المُقرِّر (`DenyApprover` افتراضيًا — أي أن
   الأدوات الخطرة **تفشل مغلقة** حتى يُمرَّر `--allow-dangerous`). ومع `CELA_MCP_ENABLE=1` يصبح
   `cela mcp serve` **بوابة سياسة**: المضيف يرى خادمًا واحدًا، وكل أداة من الخوادم العليا مرّت
   بالفحص والتصنيف قبل أن تُعرَض.

## ما نُفِّذ (مُختبَر — `tests/test_mcp.py`، 52 اختبارًا، بلا شبكة)

| الوحدة | المسؤولية |
|---|---|
| `os_mcp/protocol.py` | تأطير JSON-RPC، تصنيف الرسائل، SSE parser، تفاوض الإصدار، `MCPToolSpec.from_wire` (قصّ وحدود)، `parse_tool_result` (نص/صورة/مورد → `ToolResult` + `Artifact`) |
| `os_mcp/config.py` | `mcpServers` → `MCPServerConfig` (stdio أو http)، مرشّح الأدوات allow/deny، بيئة بيضاء، توسيع `${VAR}`، رفض الأذونات المتساهلة |
| `os_mcp/client.py` | `StdioMCPClient` (مهلة إقلاع، مهلة لكل نداء، سقف طول السطر، تجاهل الضجيج على stdout، قتل عند الإغلاق، ذيل stderr) و`HttpMCPClient` (جلسة `Mcp-Session-Id`، إعادة التهيئة عند 404، سقف جسم، `EgressPolicy`) |
| `os_mcp/bridge.py` | أدوات `ToolRegistry` من مواصفات الخادم مع الحجر والتصنيف وتقرير `BridgeReport` |
| `os_mcp/layer.py` | `MCPLayer`: اكتشاف كل الخوادم المُفعَّلة بتوازٍ، صحة لكل خادم (`ok/off/error`)، `register_mcp_tools` (لا يكتب فوق شيء)، `process_layer()` المُغلَق افتراضيًا |
| `os_mcp/server.py` | `MCPRegistryServer`: `initialize`/`ping`/`tools/list`/`tools/call` فوق السجل بالحواجز وHITL وعدّادات `calls/denied` |
| `os_server` | `cela mcp list/doctor/call/serve`، تسجيل الأدوات في دورة حياة التطبيق، `GET /api/mcp/servers` و`/cela/v1/mcp/servers` |

سيناريوهات تُثبتها الاختبارات (عبر خادم مزيّف حقيقي بعملية منفصلة `tests/fake_mcp_server.py`):
وصف مسموم → حجر؛ أداة `destructiveHint` → ترفع إلى `dangerous`؛ `tool_risk` يخفّض أداةً واحدة فقط؛
خادم يطلب `sampling`/`roots` → يُرفض بـ`METHOD_NOT_FOUND` ولا يُنفَّذ شيء؛ سطر أطول من السقف → تفشل
النداءات المعلّقة وتُقتل العملية؛ انتهاء مهلة أداة → `ToolResult.ok=False` ويبقى الاتصال سليمًا؛ خادم
يطبع ضجيجًا على stdout → يُتجاهل؛ إصدار بروتوكول غير مدعوم → `MCPConnectError`؛ جلسة HTTP منتهية →
إعادة تهيئة شفافة؛ `CELA_SANDBOXED=1` → رفض الإقلاع؛ وكيل ReAct كامل يستدعي أداة MCP ويجيب منها؛
المضيف الخارجي يستدعي `python_execute` عبر `cela mcp serve --demo` ويحصل على `5050`؛ وبوابة من
قفزتين (مضيف → cela → خادم علوي) تُخفي الأداة المحجورة في الطرف البعيد.

## الحدود الصريحة

* **خوادم stdio تعمل على المضيف خارج الصندوق وخارج `EgressPolicy`** — عملية تفتح شبكتها بنفسها
  (نفس حدّ قنوات CLI في ADR-0013). الضوابط البديلة: التفعيل الصريح، البيئة البيضاء، `dangerous`
  افتراضيًا مع HITL، الحجر، والرفض داخل الصندوق. لا حماية مزعومة لا تُنفَّذ.
* الدعم يشمل **الأدوات فقط**؛ `resources`/`prompts`/`sampling`/`elicitation`/`roots` غير مدعومة في
  الاتجاهين (تُرفض بأدب ولا تُنفَّذ). `notifications/tools/list_changed` تُسجَّل ولا تُعيد الاكتشاف
  تلقائيًا — القائمة تُثبَّت عند الإقلاع عمدًا كي لا تتبدّل الأدوات تحت النموذج أثناء التشغيل.
* لا OAuth للخوادم البعيدة؛ المصادقة بترويسات ثابتة من متغيرات بيئة فقط.
* `resolve_dns=False` افتراضيًا في سياسة خوادم HTTP (اختبارات محكمة بلا DNS)؛ النشر يمرّر
  `default_client_factory(resolve_dns=True)` للحماية من DNS rebinding.
* `cela mcp serve` يعمل بالسجل العرضي (`--demo`) فقط حتى يُقرَّر شكل إعداد الإنتاج (نماذج/مفاتيح).
* لا أرقام أداء بعد؛ القياس عبر `os_evals` كبقية الطبقات.
