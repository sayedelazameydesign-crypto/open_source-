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

## Troubleshooting quick refs

| Symptom | Likely cause |
|---|---|
| `invalid_grant` from Drive | Access token pasted instead of refresh-token trio |
| `403` on Vercel reads | `VERCEL_TEAM_ID` missing (slug doesn't work) |
| Render hook leaks to logs | Hook URL not routed through `config.secrets()` |
| Portal renders `503 not_configured` + `missing[]` | Expected — set the named env vars, client must not retry blind |
| Button green but name shows "غير مهيأ" | False-positive config detection — re-check `config.load()` names |
