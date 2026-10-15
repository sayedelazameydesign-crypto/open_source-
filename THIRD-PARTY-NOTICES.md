# Third-party notices

This file scopes the licence of this repository. [`LICENSE`](LICENSE) is MIT and
covers **the code written here**. It does not cover everything that lives in this
checkout, and it does not cover what our dependencies do under their own terms.

Nothing in `LICENSE`, in this file, or anywhere else in this repository grants
any right to a third party's trademark.

The rules that keep this file true are in
[`docs/THIRD_PARTY_POLICY.md`](docs/THIRD_PARTY_POLICY.md). Compliance is
enforced mechanically by [`tests/test_vendored_integrity.py`](tests/test_vendored_integrity.py).

---

## 1. Third-party code hosted in this repository

One tree. Everything else in `packages/` is first-party.

| Path | Project | Upstream | Licence | Status |
|---|---|---|---|---|
| `packages/craft-launcher/` | ArtCraft Launcher | [storytold/craft-launcher](https://github.com/storytold/craft-launcher) @ `0f295d4b9c90d15c2e38e36c322bdbc0fa943a54` (v0.4.1) | **MIT OR Apache-2.0**, at your option | Unmodified snapshot, 198 files, byte-verified |

Hosted under [ADR-0015](adr/0015-craft-launcher-integration.md). Terms, refresh
procedure and the reason it must stay unmodified:
[`docs/VENDORED-craft-launcher.md`](docs/VENDORED-craft-launcher.md).

Upstream's own notices are retained unaltered inside the tree and are the
authoritative attribution for it:
`LICENSE-MIT` · `LICENSE-APACHE` · `NOTICE` · `ATTRIBUTION.md`.

**We do not redistribute this tree.** It is excluded from the `sdist`
(`[tool.hatch.build.targets.sdist]` in `pyproject.toml`) and is not a member of
the `wheel`. Nothing published as `cela-use-browser` contains it.

**Integrity.** Every file is recorded with a sha256 in
[`docs/vendored/craft-launcher.sha256`](docs/vendored/craft-launcher.sha256).
Aggregate digest of the pinned snapshot:

```
sha256:8e008dc770754f3765d0c8e545d9a16dc1224aa9bc6faff21b205719e1470039
```

`make vendored-verify` checks it offline; `make vendored-verify-upstream`
re-derives it from the upstream tarball.

---

## 2. Assets that are **NOT** open source

> ### The ArtCraft Marks
>
> The **ArtCraft name, wordmark and mark** — including every file in
> `packages/craft-launcher/docs/brand/` and
> `packages/craft-launcher/assets/app-icon/`, in every colour, size and format —
> are **trademarks of the ArtCraft Team. They are NOT open source.** They are not
> covered by the MIT OR Apache-2.0 licence above, and nothing in that licence, in
> `NOTICE`, or in any other file grants any right to them.
>
> Their terms: [`packages/craft-launcher/docs/brand/LICENSE-brand.txt`](packages/craft-launcher/docs/brand/LICENSE-brand.txt).

They are present here under the permission in that file for use "as part of
copies of this repository", which holds **only while this tree is an unmodified
copy of upstream**. That condition is what the integrity manifest enforces.

**If you fork this repository and modify that tree**, the permission no longer
applies and you must remove the marks (or replace them with your own), and you
must not call your work ArtCraft or present it as an ArtCraft product. You may
say, in plain text, that your work is based on ArtCraft Launcher by the ArtCraft
team. To do the removal correctly:

```bash
make defork-launcher ARGS="--i-am-forking --dry-run"   # see the plan first
make defork-launcher ARGS="--i-am-forking"             # then execute
```

The script reports which files will break — the marks are embedded at compile
time by `crates/ui-egui/src/icons.rs`, `apps/artcraft-launcher/build.rs` and
`src/tray.rs`, and rendered by `packaging/icons.sh`. Upstream's licence says
"remove them **or replace them with your own**"; replacement artwork is your
responsibility, and we deliberately do not attempt it here.

---

## 3. Third-party assets inside the hosted tree

Recorded per-file, with author and source, in
[`packages/craft-launcher/ATTRIBUTION.md`](packages/craft-launcher/ATTRIBUTION.md).
Retained unaltered. Summary:

| Material | Licence | Note |
|---|---|---|
| Social icons `assets/social/` — X, YouTube, TikTok, Instagram | **CC0 1.0** (Simple Icons) | |
| Social icon `assets/social/linkedin.svg` | **CC BY 4.0** (Font Awesome Free 6.7.2, Fonticons Inc.) | Attribution required; the LinkedIn logo is a trademark of LinkedIn Corporation |
| Fonts `assets/fonts/` — Archivo, Geist Mono, Inter | **SIL Open Font License 1.1** | Licence texts kept beside them: `OFL-Archivo.txt`, `OFL-GeistMono.txt`, `OFL-Inter.txt`. OFL forbids selling the fonts by themselves and restricts the Reserved Font Names |
| sRGB colour profile inside `packaging/macos/dmg/background.tiff` | LittleCMS built-in profile | "No copyright, use freely" |
| Rust crates in `Cargo.lock` | MIT (49) / Apache-2.0 (21) | Fetched by Cargo at build time; not redistributed by us |

The social icons link to ArtCraft's own accounts. Per their terms they are used
only for that, and they must not be repurposed to suggest endorsement of
anything else.

---

## 4. Python dependencies

Declared in `pyproject.toml`, installed by pip from PyPI, **not bundled or
redistributed by us**. Licences verified against PyPI on 2026-10-15.

### Core (always installed)

| Package | Licence |
|---|---|
| `pydantic` | MIT |
| `httpx` | BSD-3-Clause |

### Optional extras

| Extra | Package | Licence |
|---|---|---|
| `dev` | `pytest`, `pytest-cov` | MIT |
| | `pytest-asyncio` | Apache-2.0 |
| | `ruff` | MIT |
| `server` | `fastapi` | MIT |
| | `uvicorn` | BSD-3-Clause |
| | `websockets` | BSD-3-Clause |
| `browser` | `playwright` | Apache-2.0 |
| `sandbox` | `e2b-code-interpreter` | MIT |
| `distributed` | **`psycopg[binary]`** | **LGPL-3.0-only** ⚠️ |
| | `redis` | MIT |
| `ai` | `scikit-learn` | BSD-3-Clause |
| | `spacy` | MIT |
| | `sentence-transformers` | Apache-2.0 |
| | `faiss-cpu` | MIT |
| | `opencv-python-headless` | Apache-2.0 |
| | `paddleocr` | Apache-2.0 |
| | `faster-whisper` | MIT |

> ### ⚠️ The one copyleft dependency: `psycopg`
>
> `psycopg` is **LGPL-3.0-only**, not permissive. It sits behind the optional
> `distributed` extra and is imported at runtime as a normal Python module —
> which is the use LGPL contemplates, and it imposes no obligation on our own
> MIT-licensed code.
>
> It is called out here because it is the only dependency whose licence is not
> permissive, so the choice stays a conscious one. Two consequences worth
> knowing:
>
> - **Do not vendor, fork or statically bundle psycopg** into this repository or
>   into a published artefact. That would change the analysis from "using a
>   library" to "distributing a modified LGPL work" and trigger its source
>   obligations.
> - If a future deployment needs a strictly permissive stack, install without the
>   `distributed` extra; the platform's default storage path does not require it.

### Git-pinned dependency

| Extra | Package | Pin | Licence |
|---|---|---|---|
| `reach` | `agent-reach` | [Panniantong/agent-reach](https://github.com/Panniantong/agent-reach) @ `94f06c1969dfc1834001269d79d3ad0972d9dee6` | see that repository |

Not published on PyPI (its maintainers explicitly warn about an unrelated
package of that name there), so it is pinned by full commit sha. Installed only
when the `reach` extra is requested. pip fetches it directly; we do not mirror or
redistribute it.

---

## 5. Reporting a problem

If you believe something here is misattributed, mis-licensed, or infringes your
rights — including a trademark — please tell us before assuming bad faith:

- **Attribution or licence errors:** open an issue. These are treated as bugs and
  fixed as bugs, with the same priority.
- **Trademark concerns:** open an issue titled `trademark:`, or contact the
  repository owner through the address on their GitHub profile if you would
  rather not discuss it publicly. We will remove or replace the material pending
  resolution — the hosted tree is a convenience, not a product we ship.
- **Security vulnerabilities:** see [`CONTRIBUTING.md`](CONTRIBUTING.md). Do not
  open a public issue for an exploitable flaw.

Every third-party claim in this file is traceable: the hosted tree is
byte-verified against a pinned commit (§1), the asset attributions come from
upstream's own `ATTRIBUTION.md` (§3), and the dependency licences were read from
PyPI on the date stated (§4).
