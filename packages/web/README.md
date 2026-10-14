# packages/web — Interactive Canvas / Artifacts UI

> **الحالة: مبني ويعمل (2026-10-14، ADR-0005).** Next.js 15 (App Router) بعرض مقسوم داكن.
>
> التشغيل: شغّل الـ backend أولًا، ثم من هذا المجلد:
> `CELA_BACKEND_URL=http://127.0.0.1:8080 CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... npm run dev`
> الرموز تبقى في خادم Next فقط (وكيل `app/api/backend/*`) ولا تصل للمتصفح أبدًا.

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
| تذكرة WebSocket | `POST /runs/{id}/ws-ticket` → `{ticket, run_id, expires_in}` |
| تدفق حي | `WS /ws/{id}?ticket=...` أو `GET /runs/{id}/events` (SSE) |
| موافقة بشرية | `POST /runs/{id}/approve` |
| جلب artifact | `GET /artifacts/{id}/{filename}` |
| فحص الحياة (عام) | `GET /health` → `{status}` |

كل النقاط أعلاه — عدا `/health` — تتطلب الترويسة
`Authorization: Bearer $OS_API_TOKEN`. ولأن المتصفح لا يستطيع ضبط `Authorization`
على اتصال WebSocket مباشرة، يطلب الوسيط تذكرة قصيرة العمر أحادية الاستخدام عبر
`POST /runs/{id}/ws-ticket` ثم يتصل المتصفح بـ `/ws/{id}?ticket=<ticket>`.

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
