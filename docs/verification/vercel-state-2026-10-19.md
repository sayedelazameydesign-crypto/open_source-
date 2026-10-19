# توثيق حالة Vercel وPR #56 — 2026-10-19

**الغرض:** تثبيت ما أثبتته الأدوات فعليًا عن حالة النشر على Vercel بعد دمج
PR #56، حتى لا يُبنى القرار التالي («أضف المتغيرات ثم اختبر حيًا») على ذاكرة
جلسة سابقة أو على وصف لم يُتحقق منه.

**القاعدة المُطبَّقة هي قاعدة المشروع نفسه:** رقم بلا الأمر الذي أنتجه إعلان
وليس دليلًا. لذلك كل سطر أدناه مقرون بالأمر ونصّ مُخرَجه، وما لم يُتحقق منه
مكتوب صراحةً أنه **غير مُثبت**.

| الحقل | القيمة |
|---|---|
| وقت التحقق (UTC) | `2026-10-19T09:16:45Z` |
| رأس `origin/main` | `9adbdb72bd3a348c4d481e3a60a6e8d8f8e01d33` |
| فرع العمل في هذا الـsandbox | `arena/12a908dd-open-source` (متفرّع من رأس `main` نفسه) |
| نظافة الشجرة قبل تعديلات هذه الجلسة | `git status --porcelain` → 0 سطر |

## الخلاصة في أربع نقاط

1. **PR #56 لم يعد draft ولم يعد مفتوحًا — دُمج في `main`.** أي وصف له بأنه
   «PR للمراجعة فقط بلا دمج» لم يعد صحيحًا منذ `2026-10-19T08:57:17Z`.
2. **إعادة التحقق المحلية كلها خضراء على رأس `main`:** المجموعة الكاملة
   `1209 passed, 2 deselected`، و`tests/test_deploy_config.py` = `26 passed`،
   و`ruff check .` نظيف، و`update_evidence.py --check` يطابق، وفاحص
   `deploy_doctor` يتصرّف كما هو موثّق (تحذير بلا سر، خطأ تحت `--strict`، صمت
   خارج `VERCEL=1`).
3. **الحاجز الحقيقي أمام الاختبار الحي ليس المتغيرات بل حماية نشر Vercel.**
   رابطا Preview و`main` يردّان بصفحة `Protected Deployment – Vercel`
   (تحويل إلى `vercel.com/login?next=%2Fsso-api…`)، أي أن الطلب **لا يصل إلى
   `app/api/*` إطلاقًا**. لا يمكن التمييز — من خارج جلسة Vercel — بين «السر
   غائب» و«السر موجود» و«البوابة تعمل» حتى تُضبط **Settings → Deployment
   Protection**.
4. **موصّل Vercel المتاح في هذه الجلسة لا يرى المشروع،** فلا يمكن إضافة/قراءة
   متغيرات البيئة برمجيًا من هنا؛ المسار هو Dashboard بيد المشغّل، أو إعادة
   ربط Vercel بمجال (scope) يغطي الفريق الصحيح.

## 1) PR #56 — دُمج

```text
$ gh pr view 56 --json number,state,isDraft,mergedAt,headRefOid,mergeCommit \
    --jq '{number,state,isDraft,mergedAt,headRefOid,merge:.mergeCommit.oid}'
{"headRefOid":"d52cfb700e1ce3186cb58df7c72aa5390a606419","isDraft":false,
 "merge":"9adbdb72bd3a348c4d481e3a60a6e8d8f8e01d33",
 "mergedAt":"2026-10-19T08:57:17Z","number":56,"state":"MERGED"}
```

رأس فرع الـPR كان `d52cfb700e1ce3186cb58df7c72aa5390a606419` (المشار إليه
اختصارًا `d52cfb7`)، وcommit الدمج هو رأس `main` الحالي `9adbdb7`. هذه الجلسة
**لم** تنفّذ الدمج؛ وُجد منفَّذًا.

## 2) CI أخضر على `main` بعد الدمج

```text
$ gh api repos/sayedelazameydesign-crypto/open_source-/actions/runs \
    --jq '.workflow_runs[:3][] | [.name,.head_branch,.head_sha[0:7],.conclusion]|@tsv'
CodeQL Advanced	main	9adbdb7	success
CI              main	9adbdb7	success
Web CI          main	9adbdb7	success
```

## 3) إعادة التحقق المحلية (رأس `main` = `9adbdb7`)

```text
$ python -m pytest -q
1209 passed, 2 deselected in 36.76s

$ python -m pytest tests/test_deploy_config.py -q
26 passed in 0.40s

$ ruff check .
All checks passed!

$ python scripts/update_evidence.py --check
docs/EVIDENCE.md: test count is current
```

فاحص `deploy_doctor` حيًا (نفس السلوك الموثّق في §3.5.3):

```text
$ python scripts/deploy_doctor.py
{"ready": true, "strict_production": false,
 "files": {"render_yaml": true, "env_example": true,
           "cloudflare_bff_proxy": true, "next_bff_proxy": true},
 "errors": [], "warnings": []}                      # خارج VERCEL=1 → صمت

$ VERCEL=1 python scripts/deploy_doctor.py
… "warnings": [vercel_backend_url_unset,
               vercel_web_access_secret_unset,
               vercel_gate_required_unset]           # ready:true (تحذير فقط)

$ VERCEL=1 python scripts/deploy_doctor.py --strict
ready False
errors   [missing_api_token, missing_private_api_token,
          vercel_web_access_secret_unset, vercel_gate_required_unset]
warnings [vercel_backend_url_unset]

$ VERCEL=1 CELA_BACKEND_URL=https://… CELA_API_TOKEN=… CELA_PRIVATE_API_TOKEN=… \
  CELA_WEB_ACCESS_SECRET=… CELA_WEB_GATE_REQUIRED=1 \
  python scripts/deploy_doctor.py --strict
ready True · next_bff_proxy True · errors [] · warnings []
```

## 4) مسار النشر الفعلي: موصّل Vercel مع GitHub (لا Actions)

```text
$ ls .github/workflows
ci.yml  codeql.yml  eval-real-model.yml  launcher-ci.yml  web-ci.yml
# لا يوجد workflow ينفّذ `vercel deploy`

$ gh api repos/sayedelazameydesign-crypto/open_source-/commits/9adbdb7…/status --jq …
{"state":"success","statuses":[{"context":"Vercel","state":"success",
 "target_url":"https://vercel.com/celia-fashions-projects/open-source/A3cEbh3XkmZsEFNSFeqCF8NArRys"}]}

$ gh api repos/sayedelazameydesign-crypto/open_source-/commits/d52cfb7…/status --jq …
{"context":"Vercel","state":"success","description":"Deployment has completed",
 "target_url":"https://vercel.com/celia-fashions-projects/open-source/7xwkqUuHb36oq7NUzUatbNtC9ikD"}
```

ومن تعليق `vercel[bot]` على PR #56 (حمولة `[vc]` المشفّرة base64):

```json
{"isMonorepo": true, "type": "github", "projects": [{
  "name": "open-source",
  "projectId": "prj_FkLk1oSgnAVxkRBrBVro8SI3r9pc",
  "rootDirectory": "packages/web",
  "previewUrl": "open-source-git-arena-aca47f2a-o-55d753-celia-fashions-projects.vercel.app",
  "nextCommitStatus": "DEPLOYED"}]}
```

وتأكّد ذلك حيًا في هذه الجلسة: دفع commit توثيق Vercel هذا إلى فرع العمل
`arena/12a908dd-open-source` أنشأ نشر Vercel تلقائيًا بلا أي workflow (الـhash
أدناه هو النسخة المدفوعة الأولى؛ عُدّلت بعدها هذه الفقرة فصار hash الـcommit
الحامل لها مختلفًا، والرابط يبقى دليلًا على آلية النشر لا على hash بعينه):

```text
$ gh api repos/sayedelazameydesign-crypto/open_source-/commits/9889d43/status --jq …
{"state":"success","statuses":[{"context":"Vercel","state":"success",
 "target_url":"https://vercel.com/celia-fashions-projects/open-source/F4NwyK2nN2y8NTe2roVx64QvaCou"}]}
```

**النتيجة:** متغيرات وقت التشغيل تُضبط في **Dashboard → Settings → Environment
Variables** (Production **و** Preview)، لا عبر أسرار Actions. التوثيق في
`docs/DEPLOY_FREE_TIER_RUNBOOK.md` §3.5 و`docs/publish-runbook.md`
و`packages/web/README.md` كان يقول العكس («النشر عبر Actions، لا عبر موصّل
Vercel») وقد صُحّح في هذه الجلسة مع إبقاء مسار CLI/Actions بديلاً موثَّقًا.

## 5) الحاجز: حماية نشر Vercel تقطع الطلب قبل التطبيق

```text
$ GET https://open-source-git-arena-aca47f2a-o-55d753-celia-fashions-projects.vercel.app/api/access
→ 302/HTML: عنوان الصفحة "Protected Deployment – Vercel"
  والمحتوى صفحة "Log in to Vercel" على
  vercel.com/login?next=%2Fsso-api%3Furl%3D…%252Fapi%252Faccess…

$ GET <الرابط نفسه>/api/backend/health           → نفس صفحة الدخول
$ GET https://open-source-git-main-celia-fashions-projects.vercel.app/api/access
→ نفس صفحة الدخول
```

أي أن **Vercel Authentication** (Settings → Deployment Protection) مفعّلة،
فالطلب يُعترض عند حافة Vercel ولا يصل إلى Next.js. لذلك:

* لا يمكن إثبات `mode:"enforced"` ولا `401 access_required` ولا كوكي
  `HttpOnly; Secure; SameSite=Lax; Max-Age=2592000` ولا `502/503` الوكيل —
  كل هذه تحتاج وصولًا فعليًا إلى التطبيق.
* النتيجة المُشاهَدة **ليست** عطلًا في كود البوابة، وليست دليلًا على غياب
  `CELA_WEB_ACCESS_SECRET`. هي طبقة حماية أعلى من التطبيق.

**علاجها (قرار المشغّل، خارج الكود):** Vercel → `open-source` → **Settings →
Deployment Protection**:

1. إيقاف **Vercel Authentication** — بوابة `CELA_WEB_ACCESS_SECRET` هي الحماية
   المقصودة للواجهة المنشورة، فيكفي طبقة واحدة؛ **أو**
2. إبقاؤها وتوليد **Protection Bypass** secret ثم تمريره في الاختبار:
   `curl -H "x-vercel-protection-bypass: $BYPASS" $SITE/api/access`
   (يبقى الاختبار الحي مقصورًا على من يملك السرّين).

## 6) موصّل Vercel في هذه الجلسة لا يصل إلى المشروع

```text
$ vercel.get_authenticated_user
{"id":"AhxOBw2CFYGe3l2FY84MANwG","email":"sayedelazameydesign@gmail.com",
 "name":"celia-fashion","username":"sayedelazameydesign-7328",
 "defaultTeamId":"team_KaDefEE8HBoecXfntoJUw2TZ","limited":true}

$ vercel.list_projects            → {"projects": []}
$ vercel.list_teams               → "Access denied. Your token may not have the required permissions."
$ vercel.get_project prj_FkLk1oSgnAVxkRBrBVro8SI3r9pc
  → 404 "Project not found"
$ vercel.list_deployments (team celia-fashions-projects) → {"deployments": []}
```

ملاحظة دقيقة: `teamId` الظاهر في حمولة `vercel[bot]`
(`team_KaDefEE8HBoecXfntoJUw2TZ`) هو نفسه `defaultTeamId` للتوكن، ومع ذلك
`list_projects` فارغ و`get_project` يعيد 404 و`limited:true`. القراءة العملية:
**التوكن الحالي لا يملك صلاحية المشروع/الفريق** (أو أن الموصّل لا يمرّر
`teamId` في الطلب — رسالة الخطأ حملت `api.vercel.com/v9/projects/open-source`
بلا `teamId`). ما لم يُثبت: لم تُقرأ سجلات متغيرات البيئة، فلم يُتحقق من وجود
`CELA_WEB_ACCESS_SECRET` في Production أو Preview في هذه الجلسة.

### finding خارج النطاق ظهر أثناء `git push`

```text
remote: GitHub found 1 vulnerability on sayedelazameydesign-crypto/open_source-'s
        default branch (1 moderate). … /security/dependabot/1
$ gh api repos/…/dependabot/alerts/1
{"message":"Resource not accessible by integration","status":"403"}
```

التنبيه **موجود** (قاله `remote` نفسه)، أما تفاصيله **فغير مُثبتة هنا**: توكن
التكامل لا يملك صلاحية `dependabot:alerts`. يُقرأ من Dashboard → Security →
Dependabot alerts. لا علاقة له بمسار Vercel، وسُجّل حتى لا يُنسى.

## 7) ما هو مُثبت وما ليس مُثبتًا

| الادّعاء | الحالة |
|---|---|
| PR #56 دُمج في `main` (`9adbdb7`) | **مُثبت** — `gh pr view` |
| CI (3.11/3.12/3.13، web، CodeQL) أخضر على `main` | **مُثبت** — `actions/runs` |
| `1209 passed, 2 deselected` محليًا | **مُثبت** — `pytest -q` |
| `26/26` في `test_deploy_config.py` | **مُثبت** — `pytest tests/test_deploy_config.py -q` |
| `ruff` نظيف و`evidence --check` يطابق | **مُثبت** |
| سلوك `deploy_doctor` في الحالات الأربع | **مُثبت** — تشغيل فعلي أعلاه |
| النشر يتم عبر موصّل Vercel مع GitHub لا Actions | **مُثبت** — `ls .github/workflows` + سياق `Vercel` على الـcommits |
| Preview و`main` محميّان بـVercel Authentication | **مُثبت** — استجابة `Protected Deployment – Vercel` |
| `CELA_WEB_ACCESS_SECRET` موجود في Production و/أو غائب في Preview | **غير مُثبت في هذه الجلسة** — التوكن لا يقرأ env vars؛ التدقيق السابق (`Production` فقط) يبقى بلا تحقق مستقل هنا |
| رابط Production النهائي (`*.vercel.app` أو نطاق مخصص) | **غير مُثبت** — `open-source.vercel.app` يعيد `404: NOT_FOUND` من Vercel؛ الرابط الصحيح يؤخذ من Dashboard → Deployments |
| أن الاختبارات الأربعة (`enforced` / `401` / الكوكي / `200 أو 502`) ستنجح بعد ضبط المتغيرات | **غير مُثبت** — يحتاج أولًا رفع حماية النشر، ثم نشر جديد |
| تنبيه Dependabot واحد (moderate) على الفرع الافتراضي | **موجود** بحسب رسالة `remote` عند الدفع؛ **تفاصيله غير مُثبتة** — `403 Resource not accessible by integration` |

## 8) الترتيب العملي المقترح (خارج الكود)

1. Vercel → `open-source` → **Settings → Deployment Protection**: أوقف
   **Vercel Authentication** (أو ولّد Protection Bypass secret وقرّر الاختبار به).
2. **Settings → Environment Variables**: تأكد أن الخمسة موجودة في
   **Production وPreview معاً** كنوع *Environment Variable* (تشغيل):
   `CELA_BACKEND_URL`, `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`,
   `CELA_WEB_ACCESS_SECRET`, `CELA_WEB_GATE_REQUIRED=1`.
3. **Redeploy** لآخر commit على `main` (وللـPreview) — المتغيرات لا تُطبَّق
   بأثر رجعي.
4. نفّذ §3.5.4 من `docs/DEPLOY_FREE_TIER_RUNBOOK.md` بدءًا من الفحص صفر
   (`%{http_code} %{redirect_url}`) ثم الاختبارات الأربعة، وثبّت الناتج هنا.

## 9) ملفات ذات صلة

* `docs/DEPLOY_FREE_TIER_RUNBOOK.md` §3.5 — مسار Vercel، التنبيهات، والفحص صفر.
* `docs/publish-runbook.md` — جدول متغيرات Vercel ومسار CLI البديل.
* `packages/web/README.md` — جدول الضبط على Vercel وأوامر التحقق.
* `scripts/deploy_doctor.py`, `tests/test_deploy_config.py` — الفاحص واختباراته.
* `docs/verification/github-state-2026-10-18.md` — اللقطة السابقة بنفس المنهج.

## 10) إعادة فحص «الفحص صفر» بعد دمج PR #57 — 2026-10-19T09:44Z

أُعيد تنفيذ الخطوة صفر من §3.5.4 (وهي نفسها في `docs/publish-runbook.md`
«التحقق بعد النشر») على رأس `main` الجديد، بطلب صريح بصيغة الأمرين:

```bash
SITE=https://<رابط النشر من Dashboard>
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $SITE/api/access   # المتوقع 200 وبلا redirect
curl -sS $SITE/api/access                                                    # mode:"enforced"
```

**النتيجة باختصار: الفحص صفر لم ينجح حيًا بعد.** الروابط الصالحة كلها ما زالت
تُحوَّل إلى صفحة دخول Vercel، فلا `200` ولا `mode:"enforced"` من الإنترنت
العام. وسلوك الكود نفسه مُثبت محليًا بنفس الأمرين (أدناه)، فالحاجز ما زال
**Deployment Protection** كما في §5 — لم يتغيّر بدمج PR #57.

### 10.1) رأس `main` ونشره

```text
$ gh pr view 57 --json state,mergedAt,mergeCommit
{"state":"MERGED","mergedAt":"2026-10-19T09:32:34Z",
 "mergeCommit":{"oid":"149b45923553fef9cde1c7155e9c6fd42a6eb60a"}}

$ gh api repos/…/commits/149b459…/status --jq …
{"state":"success","statuses":[{"context":"Vercel","state":"success",
  "description":"Deployment has completed",
  "target_url":"https://vercel.com/celia-fashions-projects/open-source/DQYmcB89m9WqrmZTgrdYdFTpjF5F"}]}
```

### 10.2) قيم `SITE` المرشّحة (لا قيمة من Dashboard وصلت هذه الجلسة)

استُخرجت من تعليق `vercel[bot]` على PR #57 ومن نمط أسماء Vercel، وفُحصت كلها.
موصّل Vercel في الجلسة ما زال كما في §6 بلا أي تغيّر (`limited:true`)، فلا
يُقرأ رابط Production منه رسميًا:

```text
$ vercel.get_authenticated_user   → "limited":true, defaultTeamId team_KaDefEE8HBoecXfntoJUw2TZ
$ vercel.list_projects            → {"projects":[]}   (وبـteam_id نفسه: [])
$ vercel.list_deployments target=production → {"deployments":[]}
$ vercel.get_project prj_FkLk1oSgnAVxkRBrBVro8SI3r9pc (بـteam_id)
  → 404 …/v9/projects/prj_… (بلا teamId في الرابط) "Project not found"
$ vercel.list_domains             → "Access denied"
```

### 10.3) الفحص الحي: ما زال تحويلًا إلى `vercel.com/login`

قيد بيئة أولًا: `curl` من هذا الـsandbox لا يصل إلى `*.vercel.app` أصلًا
(قائمة خروج مقصورة على GitHub/npm/PyPI)، فمُخرَجه الحرفي ليس دليلًا عن النشر:

```text
$ curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $SITE/api/access
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to …vercel.app:443
000
```

لذلك نُفّذ الطلب نفسه بأداة جلب تتّبع التحويلات وتعرض الرابط النهائي
والعنوان. النتيجة على الروابط الثلاثة الصالحة واحدة:

```text
$ GET https://open-source-celia-fashions-projects.vercel.app/api/access
$ GET https://open-source-git-main-celia-fashions-projects.vercel.app/api/access
$ GET https://open-source-git-arena-12a908dd-o-23da05-celia-fashions-projects.vercel.app/api/access
→ النهائي: https://vercel.com/login?next=%2Fsso-api%3Furl%3Dhttps%253A%252F%252F<host>%252Fapi%252Faccess%26nonce%3D…
→ العنوان: "Protected Deployment – Vercel" — صفحة "Log in to Vercel"
```

ومعايرة حتى لا تُقرأ النتيجة خطأً: رابط وهمي بالنمط نفسه يردّ
`404 DEPLOYMENT_NOT_FOUND` بلا تحويل، و`open-source.vercel.app` يردّ
`404: NOT_FOUND` (كما في §7). أي أن الروابط الثلاثة أعلاه **موجودة ومحمية**،
وأقواها مرشّحًا لرابط Production هو الأول بنمط `<project>-<team>.vercel.app` —
لكنه يبقى **غير مُثبت المحتوى** ما دام الطلب لا يصل إلى التطبيق. لا نطاق
مخصّص مذكور في المستودع.

### 10.4) نفس الأمران محليًا: `200` و`enforced` كما هو متوقع

```text
$ cd packages/web && npm ci && npm run test:gate-parity
# tests 6 · pass 6 · fail 0

$ CELA_WEB_ACCESS_SECRET=<سر محلي وهمي> CELA_WEB_GATE_REQUIRED=1 npx next dev -p 3000
$ SITE=http://127.0.0.1:3000
$ curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $SITE/api/access
200
$ curl -sS $SITE/api/access
{"required":true,"authorized":false,"configured":true,"mode":"enforced"}
$ curl -sS -w '\n%{http_code}\n' $SITE/api/backend/health        # بلا إثبات
{"detail":"access requires the operator secret","code":"access_required","retryable":false}
401
$ curl -sS -i -X POST -H "Content-Type: application/json" -d '{"secret":"…"}' $SITE/api/access
HTTP/1.1 200 OK
set-cookie: celia_access=v1.…; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000
```

أي: متى وصل الطلب إلى Next.js وكان السر مضبوطًا، فالناتج هو المتوقع حرفيًا.
غياب `200/enforced` حيًا ليس عطل كود، ولا يثبت شيئًا عن وجود
`CELA_WEB_ACCESS_SECRET` في Vercel من عدمه.

### 10.5) ما يتبقّى (بيد المشغّل في Dashboard، لم يتغيّر عن §8)

1. **Settings → Deployment Protection**: إيقاف **Vercel Authentication**
   (أو قصرها على Preview بحيث يبقى Production عامًا)، أو توليد
   **Protection Bypass for Automation** والفحص بـ
   `curl -H "x-vercel-protection-bypass: $BYPASS" $SITE/api/access`.
   لا هذا ولا ذاك قابل للتنفيذ من الكود أو من موصّل هذه الجلسة (§6، §10.2).
2. التأكد أن الخمسة في **Production وPreview** ثم **Redeploy** (§8 خطوتان 2–3).
3. إعادة الفحص من جهاز يصل إلى `*.vercel.app`، بـ`SITE` مأخوذ من
   Dashboard → Deployments:

```bash
SITE=https://open-source-celia-fashions-projects.vercel.app   # أو الرابط من Dashboard حرفيًا
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' $SITE/api/access   # المتوقع: 200 وبلا redirect
curl -sS $SITE/api/access                                                    # {"mode":"enforced",…}
```
