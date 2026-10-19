# packages/web — Interactive Canvas / Artifacts UI

> **الحالة: الواجهة مبنيّة، و`npm run build` ينجح (2026-10-14، ADR-0005).** تطبيق Next.js 15 (App Router) بلوحة داكنة مقسومة؛ خادم التطوير قدّم `/` و`/launcher` برمز HTTP 200. معاينة الـArtifacts المضمنة وشجرة التنفيذ الحية غير مكتملتين بعد.
>
> التشغيل: شغّل الـ backend أولًا، ثم من هذا المجلد:
> `CELA_BACKEND_URL=http://127.0.0.1:8080 CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... npm run dev`
> الرموز تبقى في خادم Next فقط (وكيل `app/api/backend/*`) ولا تصل للمتصفح أبدًا.

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
```


## بوابة وصول المشغّل (`CELA_WEB_ACCESS_SECRET`)

مراجعة المصادقة 2026-10-18 (Finding 1) لاحظت أن الوكيلين يصادقان *الخادم* على الخلفية لكن لا يصادقان *المتصل*: أي من يصل إلى الوكيل العام يركب على توكنات الخادم، بما فيها نطاق `/cela/v1` الخاص. هذه البوابة هي الحل المتفق عليه لشكل المنتج ذي المشغّل الواحد ("جلسة محلية · مستخدم واحد").

### الأوضاع الثلاثة (تُحسم عند كل طلب من الإعدادات)

| الوضع | متى | الأثر |
|---|---|---|
| **enforced** | `CELA_WEB_ACCESS_SECRET` مضبوط | كل طلب `/api/backend/*` يجب أن يحمل إثبات مشغّل صالح — كوكي `HttpOnly` موقّعة عبر `POST /api/access`، أو السر نفسه في ترويسة `X-Celia-Access` للأتمتة. بلا إثبات → `401 access_required`. |
| **unconfigured** | السر غائب والبيئة إنتاج أو غير معروفة (`NODE_ENV=production`، بلا `NODE_ENV` معروف، أو `CELA_WEB_GATE_REQUIRED=1`) | الوكيل يرفض **مغلقاً** بـ `503 access_gate_unconfigured` بدل فتح وكيل مفتوح بصمت. هذا ما يحدث على **Vercel** دائماً — Next.js هناك يعمل بـ `NODE_ENV=production` فغياب السر يكسر كل `/api/backend/*`. |
| **open** | السر غائب والتشغيل محلي/اختبار صريح (`NODE_ENV=development\|test` أو `CELA_WEB_GATE_REQUIRED=0`) | سلوك قديم بلا بوابة — لا فحص. |

`DELETE /api/access` يمسح الكوكي المحلي فقط — اشتباه سرقة ⇒ دوّر `CELA_WEB_ACCESS_SECRET`.

### تصميم الكوكي الموقّعة

الكوكي لا تحمل السر أبداً. هي `v1.<expires-at>.<hex HMAC-SHA256>` — إثبات منتهي الصلاحية مشتق من السر (`crypto.subtle` متطابق على Node وWorkers).

* `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000` (30 يومًا — يعيد المشغّل إدخال السر بعد شهر أو عند التدوير).
* الكوكي المسروقة لا تزال bearer حتى انتهاء صلاحيتها المضمّنة، لكنها لا تكشف السر، لا يمكن تمديدها (الصلاحية مغطاة بالتوقيع)، وتدوير `CELA_WEB_ACCESS_SECRET` يُبطل كل الإثباتات السارية فوراً.
* ترويسة الأتمتة `X-Celia-Access` لا تزال تحمل السر الخام؛ تُقارن بزمن ثابت ولا يجب تسجيلها أبداً (الوكيلان يسجلان اسم الخطأ فقط على فشل المنبع — لا ترويسات).

### الحدود

* البوابة **ليست هوية/صلاحيات لكل مستخدم** — هي حماية واجهة عامة لمشغّل واحد أمام الإنترنت العام. التوكنات (`CELA_API_TOKEN`/`CELA_PRIVATE_API_TOKEN`) تبقى مطلوبة عند الاتصال المباشر بخدمة Render.
* الوكيلان يضيفان التوكنات من بيئة الخادم فقط (`CELA_BACKEND_URL`, `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`) ولا تظهر في المتصفح أبداً (`NEXT_PUBLIC_*` ممنوع — `deploy_doctor` يفشله كـ `browser_exposed_secret_var`).
* `GET /api/access` يعيد `{required, authorized, configured, mode}` — في وضع `unconfigured` يكون `required:true` و `authorized:false` و `mode:"unconfigured"` ليتطابق مع رفض الوكيل 503 بدل إيهام الواجهة أن كل شيء مفتوح.

### أين يُضبط على Vercel

**المسار الفعلي:** المشروع `open-source` (فريق `celia-fashions-projects`) مربوط
بـ GitHub، فالدفع إلى `main` ينشر Production ودفع أي فرع ينشر Preview عبر
**تكامل Vercel نفسه** — لا يوجد في المستودع workflow ينفّذ `vercel deploy`
(`ls .github/workflows`). لذلك تُضبط المتغيرات في **Dashboard → Settings →
Environment Variables** مع تفعيل **Production وPreview معاً**، وكل تعديل يتطلب
**نشرًا جديدًا**.

| المتغير | النوع | أين يُوضع |
|---|---|---|
| `CELA_BACKEND_URL` | Secret | Dashboard (Production+Preview). وعند النشر اليدوي عبر CLI: `vercel deploy --env CELA_BACKEND_URL=...` (**وقت التشغيل** — لا `--build-env` ولا `NEXT_PUBLIC_*`). |
| `CELA_API_TOKEN` / `CELA_PRIVATE_API_TOKEN` | Secret | نفس المسار — متغير تشغيل في Dashboard أو `--env`. |
| `CELA_WEB_ACCESS_SECRET` | Secret | **إلزامي على Vercel** (Production وPreview) — متغير تشغيل. غيابه = `503` على كل وكيل. |
| `CELA_WEB_GATE_REQUIRED` | Plain | `1` صراحةً (لتوثيق اشتراط البوابة). `0` = فتح مقصود فقط. |

> `--build-env` للبناء فقط، `--env` للتشغيل — مسارات `app/api/*` تقرأ `process.env` وقت الطلب (انظر `vercel.com/docs/cli`).

الفحص المحلي قبل النشر:

```bash
VERCEL=1 CELA_BACKEND_URL=https://... CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... \
  CELA_WEB_ACCESS_SECRET=... CELA_WEB_GATE_REQUIRED=1 \
  python scripts/deploy_doctor.py --strict
# ready:true + next_bff_proxy:true + بلا تحذير vercel_*
```

التحقق بعد النشر:

```bash
SITE=https://<project>.vercel.app
curl -sS $SITE/api/access                    # {"mode":"enforced", "required":true, "authorized":false}
curl -sS -X POST -H "Content-Type: application/json" -d '{"secret":"..."}' $SITE/api/access  # يضع الكوكي
curl -sS $SITE/api/backend/health            # {"status":"ok"} عبر الكوكي أو X-Celia-Access
```

> **تدوير السر** يُبطل كل الكوكيز فوراً. لا حاجة لمسح قاعدة بيانات.


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
