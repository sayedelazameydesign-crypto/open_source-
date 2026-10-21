# g1a — self-contained FastAPI Cloud deploy bundle

`g1a` is the Docker/ASGI entrypoint for hosting the **Celia.sh Mission OS**
FastAPI service (`os_server.app:create_app`) on container platforms, with **code
execution disabled**. Render has one canonical Blueprint at the repository root
(`render.yaml`); this directory does not define a second Render artifact.

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
| `Dockerfile` | Local parity build input; production providers use the single GHCR image. |
| `.env.example` | Environment/secret template. Never commit real values. |

## Deploy

All commands run from the **repository root** (the service is installed with
`pip install -e ".[server,distributed]"`).

### Render

Use the single canonical Blueprint at the repository root, `render.yaml`; do not
create a second Render Blueprint from this bundle. It starts
`os_server.app:create_app` directly, pins Python 3.11.9, explicitly disables
code execution, and requires the operator-supplied `sync: false` secrets. Set
`CELA_API_TOKEN`, `CELA_PRIVATE_API_TOKEN`, and `CELA_RUN_STORE_URL` pointing at
Postgres. `deploy_doctor.py --strict` is a manual preflight, not a central deploy
gate; no provider deployment is permitted until that gate is wired into the
deployment orchestrator. The health check is `GET /health`.

### Docker-based hosts

For non-Render providers, deploy the image published by the repository's
`docker-image` workflow to GHCR (do not build a provider-specific artifact):

```bash
docker pull ghcr.io/sayedelazameydesign-crypto/open_source:latest
docker run --rm -e CELA_API_TOKEN=... -e CELA_PRIVATE_API_TOKEN=... \
  -e CELA_RUN_STORE_URL=... -p 8080:8080 \
  ghcr.io/sayedelazameydesign-crypto/open_source:latest
```

The image starts the `deploy/g1a` ASGI wrapper, which delegates to the canonical
`os_server.app:create_app` factory. The checked-in Dockerfiles are build inputs
for that one GHCR image, not separate provider deployments.

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
