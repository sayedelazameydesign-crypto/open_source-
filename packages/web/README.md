# packages/web — Interactive Canvas / Artifacts UI

> **الحالة: الواجهة مبنيّة، و`npm run build` ينجح (2026-10-14، ADR-0005).** تطبيق Next.js 15 (App Router) بلوحة داكنة مقسومة؛ خادم التطوير قدّم `/` و`/launcher` برمز HTTP 200. معاينة الـArtifacts المضمنة وشجرة التنفيذ الحية غير مكتملتين بعد.
>
> التشغيل: شغّل الـ backend أولًا، ثم من هذا المجلد:
> `CELA_BACKEND_URL=http://127.0.0.1:8080 CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... npm run dev`
> الرموز تبقى في خادم Next فقط (وكيل `app/api/backend/*`) ولا تصل للمتصفح أبدًا.
>
> **بوابة وصول المشغّل:** الوكيل يوثّق *الخادم* أمام الـbackend ولا يوثّق *المتصل* — فمن
> يصل إلى الوكيل العام يستخدم رموز الخادم، بما فيها نطاق `/cela/v1` الخاص. لذلك تُفرض
> بوابة وصول المشغّل (`CELA_WEB_ACCESS_SECRET`) على `app/api/backend/*` — انظر
> [بوابة وصول المشغّل](#بوابة-وصول-المشغّل-cela_web_access_secret) أدناه.

## الواجهة الحالية

تطبيق Next.js 15 (App Router) وReact 19، بتنسيق CSS مخصص (لا يستخدم TailwindCSS أو Shadcn/UI حاليًا):

- **`/`** — مساحة عمل عربية لإدخال المهمة واختيار Auto أو ReAct أو PlanningFlow، تدفق أحداث SSE مع استئناف `after_seq`، لقطة الحالة، قائمة الخطوات، الجواب النهائي، الموافقات المعلّقة، التوجيه إلى النماذج والميزانية.
- **Artifacts** — تعرض الواجهة بيانات الملفات وروابط فتحها؛ المعاينة المضمنة لأنواع HTML/React/Markdown/الصور غير منفّذة بعد.
- **شجرة التنفيذ** — تعرض قائمة الخطوات والأحداث، لا شجرة DAG حية بعد.
- **`/launcher`** — كتالوج وإصدارات Craft App Suite بوضع القراءة فقط، مع فحص التحديثات.
- **وكيل الخادم `app/api/backend/*`** — يضيف رموز المصادقة من بيئة Next.js ويمرّر SSE؛ لا تصل الرموز إلى المتصفح.

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

## بوابة وصول المشغّل (`CELA_WEB_ACCESS_SECRET`)

الوكيل `app/api/backend/*` يُضيف `Authorization: Bearer …` من بيئة الخادم إلى كل طلب
صادر. هذا يوثّق **الخادم** أمام الـbackend، ولا يوثّق **المتصل** أمام الوكيل: أي شخص
يصل إلى الوكيل العامّ يركب على رموز الخادم — بما فيها نطاق `/cela/v1` الخاص. في
منتج «مستخدم واحد» هذا يعني أن الواجهة العامّة على الإنترنت تساوي صلاحية وصول
كاملة. البوابة هي الطبقة التي تغيّر ذلك: **إثبات وصول للمشغّل** يُطلب قبل أي
خروج للشبكة.

### الأوضاع الثلاثة

الوضع يُحسم من الإعداد **وقت الطلب** (`lib/access-gate.ts` — نفس الملف للتوأمين
Next.js وCloudflare Pages، فالعقد واحد):

| الوضع | الشرط | سلوك الوكيل |
|---|---|---|
| `enforced` | `CELA_WEB_ACCESS_SECRET` مضبوط | يُرفض الطلب بـ`401 access_required` حتى يقدّم المتصل إثباتًا صالحًا |
| `unconfigured` | السر غائب، والبيئة إنتاجية أو غير معروفة | يُرفض بـ`503 access_gate_unconfigured` — **فشل مغلق**، لا فتح صامت للوكيل |
| `open` | السر غائب، والتشغيل محلي/اختبار صراحةً (`NODE_ENV=development\|test` أو `CELA_WEB_GATE_REQUIRED=0`) | سلوك سابق بلا بوابة (وضع الجهاز المحلي) |

> «غير معروفة» تُعامَل كإنتاج عمدًا: بيئات بلا `NODE_ENV` (مثل Cloudflare Workers)
> تفشل مغلقة أيضًا.

### نقطتا النهاية

| الطلب | النتيجة |
|---|---|
| `GET /api/access` | `{ required, authorized, configured, mode }` — حالة البوابة كما يقرّرها الخادم |
| `POST /api/access` | `{ "secret": "…" }` أو ترويسة `X-Celia-Access`؛ عند المطابقة فقط يُضبط كوكي `HttpOnly` |
| `DELETE /api/access` | يمسح كوكي هذا المتصفّح فقط («اقفل هذا المتصفّح») |

### تصميم الكوكي

الكوكي **لا يحمل السر نفسه**، بل يحمل إثباتًا موقّعًا ومنتهي الصلاحية:
`v1.<تاريخ-الانتهاء-بالثواني>.<hex HMAC-SHA256>` مشتقّ من السر عبر `crypto.subtle`
(نفس التنفيذ على Node وWorkers). النتيجة:

- سرٌ مسروق يُبطل بإدارة الدوران (`CELA_WEB_ACCESS_SECRET` جديد) — لا إثباتات سارية.
- لا يمكن تمديد صلاحية الكوكي: تاريخ الانتهاء مغطّى بالتوقيع.
- لا صدى للسر في أي استجابة أو سجلّ؛ المقارنة في **زمن ثابت** (`constantTimeEqual`).
- `DELETE /api/access` يمسح الكوكي المحلي فقط — عند الاشتباه في سرقة، **درِّر السر**.
- الترويسة `X-Celia-Access` تحمل السر الخام (للأتمتة: `curl`، `CI`)، وتُقارن في زمن
  ثابت ولا تُسجَّل أبدًا — الوكيل يسجّل *اسم* الخطأ فقط عند فشل الـbackend.

### الحدود — ما ليست هي البوابة

البوابة **ليست** هوية مستخدم ولا نظام صلاحيات: هي إثبات «أنت المشغّل» لعملية واحدة.
رموز الـbackend (`CELA_API_TOKEN` / `CELA_PRIVATE_API_TOKEN`) تبقى مطلوبة للوصول
المباشر إلى خدمة Render، والوصول إلى الواجهة وحده لا يمنح صلاحية على الـbackend.
منشور على الإنترنت ⇒ اضبط `CELA_WEB_ACCESS_SECRET`، أو `CELA_WEB_GATE_REQUIRED=0`
لفتح مقصود صريح (لا تتركه على وضع `open` في الإنتاج).

### التحقق

```bash
npm run test:gate-parity   # تطابق التوأمين في كل قرار (6/6)
npm run test:next-proxy    # عقد الوكيل Next.js والبوابة (28/28)
npm run test:access-gate   # واجهة القفل عبر jsdom (5/5)
```

وللمسار الحيّ: شغّل `npm run start` (وضع الإنتاج) وتحقّق من
`GET /api/access` — بلا سر يجب أن يعيد `mode: "unconfigured"` وأن يعيد الوكيل
`503`؛ ومع سر يجب أن يعيد `mode: "enforced"` وأن يعيد `401` بلا إثبات.

## قواعد البناء (من ADR-0002)

1. **`GET /runs/{id}` أولًا، ثم WebSocket.** لا تعتمد على الأحداث وحدها؛ المخزن يُعيد
   التاريخ لكن `snapshot()` هو مصدر الحقيقة.
2. **أعد الاتصال واستعد.** عند انقطاع WS، اطلب `snapshot` ثم تابع — لا تُعد تشغيل المهمة.
3. **`run_finished` يُنهي البث.** الخادم يُغلق بعد هذا الحدث.
4. **الموافقة تفشل مغلقة.** إن لم يُجب المستخدم خلال المهلة، القرار `denied`. صمّم
   الواجهة لتُظهر ذلك بوضوح بدل تعليق دائم.

## قبل البدء

التطبيق مُهيّأ مسبقًا في هذا المجلد؛ لا حاجة إلى `create-next-app`.

```bash
# من جذر المستودع — شغّل الـ backend أولًا
python -m os_server.cli serve --demo --host 0.0.0.0 --port 8080

# في طرفية ثانية — ثبّت التبعيات وشغّل الواجهة
cd packages/web
npm ci
CELA_BACKEND_URL=http://127.0.0.1:8080 CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... npm run dev

# ومع بوابة الوصول (موصى به في أي نشر يُعامَل كعام):
CELA_BACKEND_URL=http://127.0.0.1:8080 CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... \
  CELA_WEB_ACCESS_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') \
  npm run dev
```

## override أمني لـPostCSS

يحتوي `package.json` على override مُقيَّد داخل Next:

```json
"overrides": { "next": { "postcss": "8.5.30" } }
```

السبب: `next@15.5.27` يثبّت `postcss@8.4.31`، وهي نسخة أبلغ `npm audit` عن وقوعها ضمن النطاق المتأثر (حتى `8.5.22`). الإصدار المثبّت `8.5.30` خارج ذلك النطاق؛ وأظهر كل من `npm audit` و`npm audit --omit=dev` صفر ثغرات بعد التحديث. التثبيت الدقيق مقصود، لذا أعد تقييمه عند تحديث التبعيات.

عند ترقية Next، أعد تقييم هذا الـoverride وأزله إذا كان الإصدار الجديد يثبت PostCSS مُصلحًا بنفسه. بعد تعديل `package.json`، حدّث القفل وتحقق من التبعيات والبناء:

```bash
npm install
npm ci && npm audit --omit=dev && npm run build
```

## التحقق من polling

منطق التشغيل في `hooks/use-agent-run.ts`؛ مؤقّت واحد ثابت لكل تشغيل بفاصل 1500ms،
مع منع تداخل طلبات snapshot للتشغيل نفسه، سواء جاءت من المؤقّت أو SSE أو الموافقات.
تُمنع الاستجابات القديمة من تغيير حالة التشغيل الجديد، ويتوقف polling عند snapshot نهائي.
قد تضيف أحداث SSE والموافقات طلبات فورية بين النبضات؛ الفاصل ليس حدًا عامًا لكل الطلبات.

```bash
npm run lint && npm run typecheck
npm run test:components && npm run test:hooks
npm run build
```

اختبارات hooks تستخدم React DOM مع jsdom ومؤقّتًا حتميًا؛ ليست بديلًا عن فحص Network
في متصفح فعلي. للفحص اليدوي: ابدأ مهمة طويلة، رشّح Network إلى
`/api/backend/runs/<id>` (دون `/events`)، وتحقق من طلب دوري واحد كل ~1.5 ثانية
أثناء هدوء الأحداث، وعدم وجود طلبين متزامنين عند إبطاء الشبكة، ثم توقف الطلبات الدورية
بعد الحالة النهائية. طلبا SSE والموافقة الإضافيان مقصودان.

نتائج التحقق المحلي وقياسات Chromium الفعلية، مع القيود، موثّقة في
[`docs/verification/web-polling.md`](../../docs/verification/web-polling.md).
