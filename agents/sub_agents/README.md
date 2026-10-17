# إعدادات الوكلاء الفرعيين (Sub-Agents)

يقرأ هذا المجلد `os_agents.subagents.load_sub_agents`، ويُفعَّل الوكلاء أمام المنسّق
(Orchestrator) عند ضبط `CELA_SUBAGENTS_DIR` على هذا المسار — الغياب هو التعطيل.

## الصيغ المقبولة

1. **Markdown/نص `key: value`** (كما في الخطة) — مثل `explorer.md`:

```markdown
# Explorer Sub-Agent Config
model: "llama3:8b"
role: "Search and repository exploration"
allowed_tools: ["file_reader", "grep_search"]
```

2. **JSON** — مثل `auditor.json`، للمفاتيح نفسها.

## المفاتيح

| المفتاح | الافتراضي | المعنى |
| --- | --- | --- |
| `name` | اسم الملف بدون اللوحة | المعرّف الذي يُستدعى به عبر `delegate_subagent` |
| `model` | `llama3:8b` | نموذج محلي خفيف (Ollama / llama.cpp-server / vLLM) |
| `base_url` | `http://localhost:11434/v1` | نقطة النهاية المتوافقة مع OpenAI |
| `role` | — | الدور الظاهر للمنسّق في `subagent_list` |
| `allowed_tools` | `["file_reader", "grep_search"]` | القائمة المسموحة حصراً — أي أداة خارجها تُرفض |
| `required_ram_gb` | `4` | فحص الذاكرة قبل إطلاق النموذج (`os_core.resources`) |
| `required_cpus` | `1` | فحص الأنوية |
| `max_steps` / `max_seconds` | `6` / `120` | حدود التشغيل — تقرير عند الاستنفاد ولا تفاوض |

الوكلاء هنا **للقراءة والاستكشاف فقط**: لا كتابة، لا shell، لا شبكة. التخطيط يبقى
للوكيل الرئيسي، والتنفيذ الخفيف يُفوَّض لهنا.
