# g1a — self-contained FastAPI Cloud deploy bundle

`g1a` is a ready-to-deploy bundle for hosting the **Celia.sh Mission OS** FastAPI
service (`os_server.app:create_app`) on a cloud platform, with **code execution
disabled**. It is *self-contained*: the entrypoint, dependency manifest, deploy
configs and environment template all live here, so an operator can ship the
service without assembling pieces from across the monorepo.

## Why code execution is disabled

The platform can run untrusted model-generated code
(`os_tools.code_exec`). Its `local` backend executes in a host subprocess and
relies on `unshare --net` for network isolation — privileges that a managed
cloud container (Render free tier, a PaaS dyno, an unprivileged pod) does not
have, and where running untrusted code on shared infrastructure is unsafe.

So this bundle sets:

```
CELA_CODE_EXEC_MODE=disabled
```

That selects `os_tools.code_exec.DisabledBackend`, which **never executes
anything** and fails closed: every code-exec request returns
`ToolResult(status=DENIED, executed=False)`. There is no local fallback and no
remote microVM to dial. (See ADR-0005 for the surrounding fail-closed design.)

To run code in a hosted deployment you must opt into an isolated backend
instead — never the local one:

```
CELA_CODE_EXEC_MODE=production
CELA_CODE_EXEC_BACKEND=e2b         # or: firecracker
```

`production` without a backend refuses to boot rather than silently executing
untrusted code on the host.

## Layout

| File | Purpose |
|---|---|
| `main.py` | ASGI entrypoint: `app = create_app()`. Portable across ASGI servers. |
| `requirements.txt` | Third-party runtime closure (mirrors `[server]` + `[distributed]`). |
| `render.yaml` | Render web-service blueprint (Python runtime), code-exec disabled. |
| `Dockerfile` | Container build for Docker-based clouds (Fly, Railway, Cloud Run, …). |
| `.env.example` | Environment/secret template. Never commit real values. |

## Deploy

All commands run from the **repository root** (the service is installed with
`pip install -e ".[server,distributed]"`).

### Render

Point a Render Blueprint at `deploy/g1a/render.yaml` (or copy the service block
into your own blueprint). Set the `sync: false` secrets in the Render dashboard:
`CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`, and — recommended — `CELA_RUN_STORE_URL`
pointing at Supabase Postgres. The health check is `GET /health`.

### Any Docker host

```bash
docker build -f deploy/g1a/Dockerfile -t celia-g1a .
docker run --rm -e CELA_API_TOKEN=... -e CELA_RUN_STORE_URL=... -p 8080:8080 celia-g1a
```

### Local smoke test

```bash
pip install -e ".[server,distributed]"
CELA_API_TOKEN=dev-token CELA_CODE_EXEC_MODE=disabled \
    uvicorn --app-dir deploy/g1a main:app --host 0.0.0.0 --port 8080
# then: curl -s localhost:8080/health
```

## Notes

- Without `CELA_API_TOKEN` the app still starts; guarded endpoints answer `503`
  until the token is provided.
- This bundle does not change application behaviour beyond selecting the
  `disabled` code-exec backend; it is configuration plus a portable entrypoint.
