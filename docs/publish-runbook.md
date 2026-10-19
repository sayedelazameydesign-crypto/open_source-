# Publish Runbook — Integrations Portal

Ordered from the Drive change-list doc
(`02 — قائمة التغييرات للنشر (Vercel · Render · Drive).md`, updated 8 Oct 2026),
with verification criteria from `01 — بوابة التكاملات · خريطة الأزرار الثمانية.md`.

**Order is mandatory: env vars first → deploy → verify.**
All reads resolve from `backend/integrations/config.py::load()`; if anything is
missing there, stop before deploying.

---

## Phase 0 — Environment variables (before ANY deploy)

### Auth & access control
| Variable | Value | Notes |
|---|---|---|
| `WAHA_OWNER_TOKEN` | `openssl rand -hex 32` | Guards `/api/owner/login`; must never reach clients |
| `WAHA_ALLOWED_ORIGINS` | Public origin(s) | Public CORS list |
| `WAHA_OWNER_ALLOWED_ORIGINS` | Strict admin origin | Configured from the *public* origin's subdomain |
| `WAHA_TRUSTED_HOSTS` | Hosts **without** `https://`, **no wildcards** | On Render/Vercel include `RENDER_EXTERNAL_HOSTNAME` / `VERCEL_URL` — blocks DNS rebinding |

### GitHub
| Variable | Value |
|---|---|
| `GITHUB_TOKEN` | PAT with `actions:read` + `actions:write` |
| `GITHUB_REPO` | `owner/name` |
| `GITHUB_WORKFLOW_ID` | e.g. `ci.yml` (exact name — button depends on it) |

### Vercel
| Variable | Value |
|---|---|
| `VERCEL_TOKEN` | Read scope is sufficient for reading |
| `VERCEL_PROJECT_ID` | `prj_…` (Project → Settings → Project ID) |
| `VERCEL_TEAM_ID` | `team_…` — slug alone fails with 403 |
| `DEPLOY_HOOK_URL` | 🔒 **Must stay secret — the URL IS the publish button** |

> From `DEPLOY-VERCEL.md`: write secrets **only** in GitHub Secrets, and do **NOT**
> enable the `Sync production keys to Vercel` workflow (marked for removal).

### Render
| Variable | Value |
|---|---|
| `RENDER_API_KEY` | Render → Settings → API Keys (read is enough) |
| `RENDER_SERVICE_ID` | `srv-…` |
| `RENDER_DEPLOY_HOOK_URL` | Service → Settings → Deploy Hooks |

> The hook key rides in `?key=` and appears in proxies as path — that's why the
> hook must live only in `config.secrets()`; otherwise it lands in any log.

### Google Drive
| Variable | Value |
|---|---|
| `GOOGLE_DRIVE_REFRESH_TOKEN` + `GOOGLE_DRIVE_CLIENT_ID` + `GOOGLE_DRIVE_CLIENT_SECRET` | **Required together.** |
| `GOOGLE_DRIVE_FOLDER_ID` | Optional; needed for upload target |
| `GOOGLE_DRIVE_ACCESS_TOKEN` | ⚠️ **NOT a credential** — expires in 1h. Pasting it yields a brief "غير مهيأ" then `invalid_grant`. Treat "غير مهيأ" as "not configured"; don't chase it. |

**Gate 0:** `config.load()` reports nothing missing → proceed.

---

## Phase 1 — Deploy

1. Deploy backend to the target platform(s) — only after Gate 0.

---

## Phase 2 — Verify

```bash
# Before publishing — sanity only, no secrets, no requests
python scripts/deploy_doctor.py --target=render
python scripts/deploy_doctor.py --target=vercel

# After publishing — is Render actually working?
# (needs real credentials; may trip rate limits)
WAHA_LIVE_INTEGRATIONS=1 python scripts/integrations_live_check.py
```

Then open `/integrations`.

**Gate 1 — acceptance criteria:**
- All **8 buttons green** for configured integrations:
  GitHub runs/dispatch, Vercel deployments/deploy-hook,
  Render deploys/deploy-hook, Drive files/upload.
- `not_configured` 503 responses include correct `missing[]`.
- **No** button shows `configured` while its name shows `غير مهيأ`
  (that mismatch = a stale/false config flag).
- Missing-credential path renders the portal (`missing[]`), it does **not**
  throw `٥٠٣ الكود` / "undefined" errors.
- Read endpoints return from `GET /api/owner/integrations` only; POSTs stay
  run-only (`dispatch-ci`, `deploy`, `deploy-render`, `upload-drive`).

---

## Phase 3 — Post-publish spot-check (Google Drive)

- Upload accepts a single file **≤ 512KB**, flat names (no `/`, no nested folders).
- Response returns `id`, `name`, `webViewLink` only — no private data, no tokens.

---

## Celia.sh — Mission OS: Next.js on Vercel (بوابة الوصول)

> هذا القسم يخص **`packages/web`** (واجهة Celia.sh)، لا بوابة التكاملات.
> `docs/publish-runbook.md` أعلاه يوثّق بوابة تكاملات منفصلة (مجلد `backend/integrations/` غير موجود هنا).
> هنا: كيف لا ينتهي نشر Next.js على Vercel بفشل مغلق على كل `/api/backend/*`.

### لماذا Vercel خاص

Next.js على Vercel يعمل دائماً بـ `NODE_ENV=production`. بلا `CELA_WEB_ACCESS_SECRET`
يعمل الوكيل `app/api/backend/[...path]/route.ts` (وكذلك توأمه في
`functions/api/backend/[[path]].ts`) بفشل مغلق: كل طلب يعيد `503 access_gate_unconfigured`
والواجهة المنشورة لا تصل إلى الـbackend إطلاقاً. آمن لكنه مكسور — و`deploy_doctor`
لم يكن ينبّه عنه حتى هذا التحديث.

### متغيرات Vercel (Production وPreview — كلاهما مطلوب)

> **تدقيق 2026-10-19:** `open-source` في Vercel مربوط بـ GitHub. `Production` على `main` (`83088b5`) و`Preview` على `9e7c207` كان `READY` لكن الفحص وجد السجل في `Production` فقط وغائب في `Preview`. متغيرات Dashboard لا تُطبّق على عمليات النشر السابقة؛ يلزم نشر جديد بعد إضافة السر في Preview.

| المتغير | النوع | القيمة |
|---|---|---|
| `CELA_BACKEND_URL` | Secret | `https://<service>.onrender.com` (**https إلزامي** — `http://` يُرفض) |
| `CELA_API_TOKEN` | Secret | الرمز العام |
| `CELA_PRIVATE_API_TOKEN` | Secret | الرمز الخاص (مختلف عن العام) |
| `CELA_WEB_ACCESS_SECRET` | Secret | **إلزامي على Vercel** — سر المشغّل (32+ حرف، `secrets.token_urlsafe(32)`) |
| `CELA_WEB_GATE_REQUIRED` | Plain | `1` صراحةً (توثيق أن البوابة مطلوبة؛ `0` = فتح مقصود فقط) |

### أين يُوضع السر عند النشر عبر GitHub Actions

النشر عندك **عبر Actions secrets**، لا عبر موصّل Vercel. الحساب قد يكون فارغاً
من المشاريع، فاختلاق مشروع كان سينشئ نشراً في غير مكانه. الأسرار إذاً:

1. **GitHub → Settings → Secrets and variables → Actions**: أضف
   `CELA_BACKEND_URL`, `CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`,
   `CELA_WEB_ACCESS_SECRET`, `VERCEL_TOKEN`, `VERCEL_PROJECT_ID`, `VERCEL_ORG_ID`.
2. **مرّرها كـ `--env` (وقت التشغيل)** عند الاستدعاء — لا في نصّ الـworkflow ولا في `NEXT_PUBLIC_*`.
   `--build-env` للبناء فقط، أما `app/api/*` فيقرأ `process.env` وقت الطلب، لذا يجب أن يكون السر متاحًا في بيئة التشغيل (Dashboard أو `--env`):

```yaml
- run: npx vercel deploy --prod --env CELA_BACKEND_URL=$CELA_BACKEND_URL --env CELA_API_TOKEN=$CELA_API_TOKEN --env CELA_PRIVATE_API_TOKEN=$CELA_PRIVATE_API_TOKEN --env CELA_WEB_ACCESS_SECRET=$CELA_WEB_ACCESS_SECRET --env CELA_WEB_GATE_REQUIRED=$CELA_WEB_GATE_REQUIRED
  env:
    CELA_BACKEND_URL: ${{ secrets.CELA_BACKEND_URL }}
    CELA_API_TOKEN: ${{ secrets.CELA_API_TOKEN }}
    CELA_PRIVATE_API_TOKEN: ${{ secrets.CELA_PRIVATE_API_TOKEN }}
    CELA_WEB_ACCESS_SECRET: ${{ secrets.CELA_WEB_ACCESS_SECRET }}
    CELA_WEB_GATE_REQUIRED: "1"
    VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
```

> لا تُمكّن `Sync production keys to Vercel` إن وُجد؛ المرجع هو سرّ Actions
> مع `--env` (وقت التشغيل) — انظر `vercel.com/docs/cli` (`--env` vs `--build-env`).

### أثر غياب السر ومتى يفشل الفحص

| الحالة | ما يراه المستخدم |
|---|---|
| `CELA_WEB_ACCESS_SECRET` غائب على Vercel | كل `/api/backend/*` → `503 access_gate_unconfigured` (قرار البوابة قبل الشبكة) |
| سر مضبوط بلا إثبات | `GET /api/backend/*` → `401 access_required`، و`GET /api/access` → `{mode: "enforced", required:true, authorized:false}` |
| سر خاطئ بلا صدى، كوكي مزوّرة | `401` بلا تسريب (مقارنة زمن ثابت) |
| `CELA_BACKEND_URL=http://…` (خارج loopback) | `503 backend_not_configured` + "must use https" — الوكيل والطبيب يرفضان `http://` معاً |

الفحص:

```bash
# يحاكي بيئة بناء Vercel (VERCEL=1) — تحذير افتراضياً، خطأ تحت --strict
VERCEL=1 CELA_BACKEND_URL=https://... CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... python scripts/deploy_doctor.py
# → warnings: vercel_web_access_secret_unset, vercel_gate_required_unset
VERCEL=1 CELA_BACKEND_URL=https://... CELA_API_TOKEN=... CELA_PRIVATE_API_TOKEN=... CELA_WEB_ACCESS_SECRET=... CELA_WEB_GATE_REQUIRED=1 python scripts/deploy_doctor.py --strict
# → ready:true, next_bff_proxy:true
```

`deploy_doctor` يميّز صراحةً بين «تحذير» و«فشل مغلق»: التحذير افتراضياً لأن
السر قد يقيم في إعدادات Vercel التي لا يراها الفحص، والخطأ تحت `--strict`
عندما تريد إثباتاً صلباً قبل النشر. خارج بناء Vercel (بلا `VERCEL=1`) يصمت
الفرع تماماً.

### التحقق بعد النشر

```bash
SITE=https://<project>.vercel.app
curl -sS $SITE/api/access  # {"mode":"enforced","required":true,"authorized":false}
# كوكي HttpOnly; Secure; SameSite=Lax; Max-Age=2592000
curl -sS -X POST -H "Content-Type: application/json" -d '{"secret":"..."}' $SITE/api/access
curl -sS $SITE/api/backend/health  # {"status":"ok"} عبر الكوكي
curl -sS -H "X-Celia-Access: $CELA_WEB_ACCESS_SECRET" $SITE/api/backend/health  # للأتمتة
# Max-Age=0 عند DELETE، و502 backend_unreachable بترويسة الأتمتة لو الـbackend نائم
```

---

## Troubleshooting quick refs

| Symptom | Likely cause |
|---|---|
| `invalid_grant` from Drive | Access token pasted instead of refresh-token trio |
| `403` on Vercel reads | `VERCEL_TEAM_ID` missing (slug doesn't work) |
| Render hook leaks to logs | Hook URL not routed through `config.secrets()` |
| Portal renders `503 not_configured` + `missing[]` | Expected — set the named env vars, client must not retry blind |
| Button green but name shows "غير مهيأ" | False-positive config detection — re-check `config.load()` names |
| `503 access_gate_unconfigured` على كل `/api/backend/*` على Vercel | `CELA_WEB_ACCESS_SECRET` غائب و`NODE_ENV=production` يفشل مغلقاً | أضف `CELA_WEB_ACCESS_SECRET` + `CELA_WEB_GATE_REQUIRED=1` في Vercel (Production وPreview) **كمتغيرات تشغيل (`--env` أو Dashboard → Environment Variables → Production+Preview)** — لا `--build-env` — ثم أعد النشر |
| `401 access_required` بلا كوكي على Vercel | البوابة `enforced` بلا إثبات | افتح `/api/access` وأدخل السر، أو استخدم `X-Celia-Access` للأتمتة |
