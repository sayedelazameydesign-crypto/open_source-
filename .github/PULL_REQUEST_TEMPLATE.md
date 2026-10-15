<!--
This template exists because the mistakes it guards against are invisible in a
diff. A one-line fix inside packages/craft-launcher/ silently lapses a trademark
permission; a tidy pyproject.toml that configures the wheel but not the sdist
silently ships third-party brand assets to a package index. Neither shows up in
review as suspicious. See docs/THIRD_PARTY_POLICY.md.
-->

## What changed

<!-- One or two sentences: the behaviour, not the diff. -->

## Why

<!-- The problem, or the decision this implements. Link an ADR if there is one. -->

## How it was verified

<!--
  Commands you actually ran and what they showed. "Tests pass" is not evidence;
  `make check` → `934 passed, 1 skipped` is. If a claim in the PR description
  comes from running the code, paste the output.
-->

```
make check
```

## Third-party & ownership — `docs/THIRD_PARTY_POLICY.md`

**Tick the first line, or fill in the rest.**

- [ ] This PR adds **no** third-party code, assets, data, fonts, icons, names or logos.

If it does:

- [ ] **Classified** (rule T-1) — A / B / C / D  ·  licence: `__________`  ·  owner: `__________`  ·  source: `__________`
- [ ] **Noticed in the same PR** (rule T-2) — entry added to `THIRD-PARTY-NOTICES.md`; the licence text sits beside the material, not only as a link
- [ ] **Vendored tree untouched** (rule T-3) — nothing under `packages/craft-launcher/` was added, removed or edited. If the snapshot was deliberately refreshed: new full 40-char sha, `make vendored-write COMMIT=<sha>`, and `docs/VENDORED-craft-launcher.md` updated
- [ ] **Not redistributed** (rule T-4) — if the path is third-party, it is excluded from the `sdist` and absent from the `wheel` target in `pyproject.toml`
- [ ] **Provenance visible** (rule T-5) — new vendored paths carry `linguist-vendored` in `.gitattributes`
- [ ] **Copyleft decided, not discovered** (rule T-6) — no GPL/AGPL added; LGPL/MPL only as an imported dependency, with the reasoning written down
- [ ] **No marks** (rule T-7) — no third-party product name, logo, icon or brand colour in the project's name, icon, docs headings or marketing. Describing a dependency by its real name is fine; implying endorsement is not
- [ ] **Copied snippets attributed** (rule T-8) — anything taken from a blog, Stack Overflow (CC BY-SA — attribution required) or a model's output is rewritten in our own style or credited above the code

## Checklist

- [ ] `make check` is green (lint + the full suite + the compliance tests, no network)
- [ ] New behaviour ships with tests; a fixed bug ships with the test that reproduces it
- [ ] Docs updated where behaviour changed — README, `docs/`, and an ADR if this reverses or amends a recorded decision
- [ ] No secrets, tokens or credentials in code, tests, fixtures or docs
- [ ] Public API stays typed; `__all__` updated if something was exported
- [ ] Commit messages use conventional prefixes (`feat:`, `fix:`, `docs:`)
