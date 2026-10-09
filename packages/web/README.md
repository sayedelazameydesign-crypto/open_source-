# packages/web — Interactive Canvas / Artifacts UI

> **الحالة: هيكل فقط — لم يُبنَ بعد.** هذه المرحلة 3 في `docs/ROADMAP.md`.
> الـ backend (`os_server`) مُكتمل ومُختبَر ويمكن بناؤه عليه مباشرة.

## المخطط

Next.js 15 + TailwindCSS + Shadcn/UI، بعرض مقسوم:

```
┌──────────────────────────────┬──────────────────────────────┐
│  Conversation + Live Agent   │        Artifacts Pane        │
│  ─────────────────────────   │  ─────────────────────────   │
│  💭 thought                  │  ┌────────────────────────┐  │
│  🧠 model routing            │  │  chart.html (preview)  │  │
│  🔧 tool call                │  │                        │  │
│  ✓  observation              │  └────────────────────────┘  │
│  ▶ execution tree (DAG)      │  [code] [html] [md] [image]  │
│  🔐 approval request         │                              │
└──────────────────────────────┴──────────────────────────────┘
```

## العقد مع الـ backend (جاهز ومُختبَر)

| الحاجة | النقطة |
|---|---|
| بدء مهمة | `POST /runs` → `{run_id, mode}` |
| استعادة الحالة | `GET /runs/{id}` → `RunState.snapshot()` |
| تدفق حي | `WS /ws/{id}` أو `GET /runs/{id}/events` (SSE) |
| موافقة بشرية | `POST /runs/{id}/approve` |
| جلب artifact | `GET /artifacts/{id}/{filename}` |
| الأدوات المتاحة | `GET /health` |

### أنواع الأحداث التي ترسمها الواجهة

```
run_started, plan_created, plan_updated,
step_started, step_finished,
model_selected, thought,
tool_call, tool_result, artifact,
approval_requested, approval_resolved,
review, log, warning, error, run_finished
```

## قواعد البناء (من ADR-0002)

1. **`GET /runs/{id}` أولًا، ثم WebSocket.** لا تعتمد على الأحداث وحدها؛ المخزن يُعيد
   التاريخ لكن `snapshot()` هو مصدر الحقيقة.
2. **أعد الاتصال واستعد.** عند انقطاع WS، اطلب `snapshot` ثم تابع — لا تُعد تشغيل المهمة.
3. **`run_finished` يُنهي البث.** الخادم يُغلق بعد هذا الحدث.
4. **الموافقة تفشل مغلقة.** إن لم يُجب المستخدم خلال المهلة، القرار `denied`. صمّم
   الواجهة لتُظهر ذلك بوضوح بدل تعليق دائم.

## قبل البدء

```bash
# من جذر المستودع — شغّل الـ backend أولًا
python -m os_server.cli serve --demo --host 0.0.0.0 --port 8080
```

ثم في هذا المجلد: `pnpm create next-app . && pnpm i` (غير مُنفَّذ بعد).
