"""Third-party hosting compliance: the vendored tree stays pristine and unshipped.

``packages/craft-launcher`` is an unmodified snapshot of
``storytold/craft-launcher`` (ADR-0015). Hosting it is permitted *because* it is
a copy: the ArtCraft marks inside it are not open source, and
``docs/brand/LICENSE-brand.txt`` grants their use only "as part of copies of
this repository", ending "automatically for anyone who breaks these terms". A
modified copy that still carries the marks is the infringement — and it happens
silently, one well-meaning edit at a time.

These tests turn that from prose into a gate. Everything here is **offline**:
the recorded manifest in ``docs/vendored/craft-launcher.sha256`` is compared
against the working tree, never against the network (CI re-derives the manifest
from the upstream tarball separately, in ``launcher-ci.yml``).

Covered:
  * the tree matches the pinned manifest, file for file;
  * the manifest is self-consistent and pins a full sha, not a moving branch;
  * the trademark terms and the marks they govern are both still present;
  * the tree is not redistributed in our ``sdist`` or ``wheel``;
  * GitHub is told the tree is vendored, so the language bar stays ours;
  * no first-party code touches the tree *on disk* — while the upstream repository
    slug it needs for release URLs stays (the distinction is the point);
  * the fork-only brand-stripping script cannot fire by accident.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TREE = REPO / "packages" / "craft-launcher"
MANIFEST = REPO / "docs" / "vendored" / "craft-launcher.sha256"
SCRIPT = REPO / "scripts" / "vendored_manifest.py"
DEFORK = REPO / "scripts" / "defork_launcher.sh"

#: Paths whose presence *is* the legal permission. If the tree ever diverges we
#: must remove the marks; while it does not diverge we must keep them.
BRAND_PATHS = (
    "docs/brand/LICENSE-brand.txt",
    "docs/brand/artcraft-mark.svg",
    "assets/app-icon/LICENSE.txt",
)

#: Attribution that must survive any refresh of the snapshot.
ATTRIBUTION_PATHS = ("LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md")


def _load_tool():
    """Import scripts/vendored_manifest.py without adding a package to sys.path.

    Registering in ``sys.modules`` *before* exec is not optional: the module
    defines a ``@dataclass``, and dataclasses resolves the defining module
    through ``sys.modules[cls.__module__]`` while processing annotations.
    Without the entry that lookup returns ``None`` and collection explodes with
    an AttributeError that says nothing about the real cause.
    """
    spec = importlib.util.spec_from_file_location("vendored_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


tool = _load_tool()

#: These tests assert on a *source checkout*: they read the vendored tree, run
#: ``git ls-files``, and inspect repository metadata. An unpacked sdist has no
#: ``.git`` and — by design (rule T-4) — no ``packages/craft-launcher``, so there
#: is nothing meaningful to assert and the module skips rather than fails.
#:
#: The condition is deliberately ``manifest AND .git``, not ``tree exists``: a
#: checkout where the manifest is present but the tree has been deleted is a real
#: violation and must fail loudly, not skip.
_IS_SOURCE_CHECKOUT = MANIFEST.is_file() and (REPO / ".git").exists()

pytestmark = pytest.mark.skipif(
    not _IS_SOURCE_CHECKOUT,
    reason="not a full source checkout — the sdist excludes the vendored tree by design",
)


@pytest.fixture(scope="module")
def manifest() -> tool.Manifest:
    assert MANIFEST.is_file(), (
        f"missing {MANIFEST.relative_to(REPO)} — run: python scripts/vendored_manifest.py write"
    )
    return tool.parse(MANIFEST)


# ---------------------------------------------------------------------------
# 1. The snapshot is what it claims to be
# ---------------------------------------------------------------------------


def test_the_vendored_tree_matches_the_pinned_manifest(manifest) -> None:
    """No file added, removed or edited since the pin was recorded."""
    actual = tool.collect(REPO, manifest.tree)
    delta = tool.diff(manifest.records, actual)

    assert not delta["added"], f"files added to the snapshot: {delta['added'][:10]}"
    assert not delta["removed"], f"files removed from the snapshot: {delta['removed'][:10]}"
    assert not delta["changed"], f"files modified in the snapshot: {delta['changed'][:10]}"
    assert actual, "the snapshot resolved to zero files — the check would pass vacuously"


def test_the_manifest_agrees_with_itself(manifest) -> None:
    """A hand-edited manifest cannot smuggle drift past the per-file compare."""
    assert manifest.digest == tool.aggregate(manifest.records)
    assert manifest.declared_files == len(manifest.records), (
        f"header claims {manifest.declared_files} files, {len(manifest.records)} are recorded"
    )
    assert manifest.tree == "packages/craft-launcher"
    assert manifest.source == "https://github.com/storytold/craft-launcher"


def test_the_pin_is_a_full_commit_sha(manifest) -> None:
    """A branch or tag pin can move under us; only a sha freezes the copy."""
    assert re.fullmatch(r"[0-9a-f]{40}", manifest.commit), (
        f"commit {manifest.commit!r} is not a full 40-char sha"
    )


def test_the_snapshot_carries_no_local_debris(manifest) -> None:
    """Untracked files inside the tree mean we are distributing more than upstream."""
    stray = tool.untracked_files(REPO, manifest.tree)
    assert not stray, f"untracked files inside the vendored tree: {stray[:10]}"


def test_the_upstream_attribution_survived(manifest) -> None:
    """The licences and notices upstream requires us to keep, still there."""
    for rel in ATTRIBUTION_PATHS:
        assert (TREE / rel).is_file(), f"missing required upstream notice: {rel}"
        assert manifest.records.get(rel), f"{rel} is not in the integrity manifest"


def test_the_trademark_terms_and_the_marks_they_govern_are_both_present(manifest) -> None:
    """Marks stay because the copy is unmodified; the terms travel with them."""
    for rel in BRAND_PATHS:
        assert (TREE / rel).is_file(), f"missing brand path: {rel}"
        assert manifest.records.get(rel), f"{rel} is not in the integrity manifest"

    terms = (TREE / "docs/brand/LICENSE-brand.txt").read_text(encoding="utf-8")
    # The two clauses our hosting rests on. If upstream ever rewords them the
    # whole integration must be re-evaluated (VENDORED.md, refresh step 2).
    assert "NOT open source" in terms
    assert "as part of copies of this repository" in terms
    assert "ends automatically" in terms


def test_no_first_party_file_lives_inside_the_vendored_tree() -> None:
    """Our own documentation must sit outside, or the copy stops being a copy."""
    tracked = tool.tracked_files(REPO, "packages/craft-launcher")
    ours = ("VENDORED.md", "README.open_source.md", "PATCHES.md", "LOCAL.md")
    for rel in tracked:
        assert Path(rel).name not in ours, (
            f"{rel} is a first-party file inside the vendored tree; "
            "move it to docs/ so the snapshot stays byte-identical to upstream"
        )


# ---------------------------------------------------------------------------
# 2. We do not redistribute it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_sdist_excludes_the_vendored_tree(pyproject) -> None:
    """Hatchling's sdist defaults to every tracked file — which would ship the
    ArtCraft marks to a package index under our MIT metadata. Excluded explicitly.
    """
    sdist = pyproject["tool"]["hatch"]["build"]["targets"].get("sdist", {})
    excluded = sdist.get("exclude", [])
    assert "packages/craft-launcher" in excluded, (
        "pyproject.toml must exclude packages/craft-launcher from the sdist; "
        "see docs/THIRD_PARTY_POLICY.md rule T-4"
    )


def test_the_wheel_ships_first_party_packages_only(pyproject) -> None:
    """The wheel lists exactly the os_* packages — never the vendored tree."""
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages, "an empty wheel target would fall back to including everything"
    for entry in packages:
        assert entry.startswith("packages/os_"), f"unexpected wheel member: {entry}"
    assert not any("craft-launcher" in entry for entry in packages)


def test_our_licence_does_not_claim_the_vendored_tree(pyproject) -> None:
    """Root metadata is MIT; the tree is MIT *or* Apache-2.0 plus non-free marks.
    A reader must be pointed at the distinction rather than left to assume it.
    """
    assert pyproject["project"]["license"] == {"file": "LICENSE"}
    assert (REPO / "THIRD-PARTY-NOTICES.md").is_file(), (
        "root THIRD-PARTY-NOTICES.md is what scopes the MIT claim"
    )
    notices = (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    for needle in ("packages/craft-launcher", "Apache-2.0", "LICENSE-brand.txt", "NOT open source"):
        assert needle in notices, f"THIRD-PARTY-NOTICES.md must state {needle!r}"


def test_ruff_does_not_lint_the_vendored_tree(pyproject) -> None:
    """Linting it invites editing it; and its style is not ours to enforce."""
    assert "packages/craft-launcher" in pyproject["tool"]["ruff"]["extend-exclude"]


# ---------------------------------------------------------------------------
# 3. Discovery and attribution stay honest
# ---------------------------------------------------------------------------


def test_github_is_told_the_tree_is_vendored() -> None:
    """Without linguist-vendored the language bar credits us with upstream's Rust."""
    attributes = REPO / ".gitattributes"
    assert attributes.is_file(), "missing .gitattributes"
    body = attributes.read_text(encoding="utf-8")
    assert re.search(r"^packages/craft-launcher/\*\*\s+.*linguist-vendored", body, re.MULTILINE), (
        ".gitattributes must mark packages/craft-launcher/** as linguist-vendored"
    )


def test_the_readme_points_at_the_third_party_notices() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "THIRD-PARTY-NOTICES.md" in readme, (
        "README must link THIRD-PARTY-NOTICES.md so the licence scope is one click away"
    )


def test_the_hosting_terms_live_outside_the_tree() -> None:
    """VENDORED.md inside the tree would itself be a local modification."""
    assert (REPO / "docs" / "VENDORED-craft-launcher.md").is_file()
    assert not (TREE / "VENDORED.md").exists()


# ---------------------------------------------------------------------------
# 4. The adapter stays decoupled from the snapshot
# ---------------------------------------------------------------------------


def test_no_first_party_code_touches_the_vendored_tree_on_disk() -> None:
    """ADR-0015: the adapter mirrors the contract, it does not consume the tree.

    The distinction that matters is *filesystem* vs *network identity*:

      ``LAUNCHER_ID = "artcraft-launcher"`` and ``f"{GITHUB_OWNER}/craft-launcher"``
      are a GitHub repository slug used to build release-API URLs. That is the
      adapter doing its job, and it must stay.

      ``packages/craft-launcher`` is a **path on disk**. First-party code reading
      it would couple our execution path to third-party files, turn a snapshot we
      merely host into a runtime dependency, and make the tree's contents part of
      our behaviour — which is exactly what "we do not redistribute it" denies.

    So this asserts on the path, not on the name. Docstrings and comments are
    stripped first: they legitimately point maintainers at the tree as the
    contract's source of truth, and a comment cannot open a file.
    """
    packages = sorted((REPO / "packages").glob("os_*"))
    assert packages, "no first-party packages found — the check would pass vacuously"

    sources = [p for pkg in packages for p in pkg.rglob("*.py")]
    assert sources, "first-party packages have no sources to check"

    for path in sources:
        text = path.read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", text)
        code = re.sub(r"'''[\s\S]*?'''", "", code)
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        assert "packages/craft-launcher" not in code, (
            f"{path.relative_to(REPO)} references the vendored tree by path in executable "
            "code. The tree is hosted for reference and built by its own CI; it must not be "
            "read at runtime (ADR-0015, docs/THIRD_PARTY_POLICY.md rule T-3)."
        )


def test_the_slug_stays_distinct_from_the_path() -> None:
    """Guard the guard: the assertion above must not be trivially satisfiable.

    `os_launcher` *should* know the upstream repository slug — that is how it
    reads releases. If that ever disappeared the previous test would still pass
    while the adapter had silently lost its purpose, so pin both halves of the
    distinction.
    """
    catalog = (REPO / "packages" / "os_launcher" / "os_launcher" / "catalog.py").read_text(
        encoding="utf-8"
    )
    assert "craft-launcher" in catalog, "the upstream repo slug is gone from catalog.py"
    assert "packages/craft-launcher" in catalog, (
        "the docstring should still point maintainers at the vendored contract source"
    )


def test_the_defork_script_is_fork_only_and_needs_explicit_consent() -> None:
    """Stripping the marks here would make *our* copy a modified version and break
    packaging/icons.sh. The script must refuse unless the caller states intent.
    """
    assert DEFORK.is_file(), "missing scripts/defork_launcher.sh"
    body = DEFORK.read_text(encoding="utf-8")
    assert "--i-am-forking" in body, "the script must require an explicit confirmation flag"
    assert "docs/brand" in body and "assets/app-icon" in body

    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "defork-launcher" in makefile


def test_the_policy_that_governs_all_this_is_written_down() -> None:
    """The rules are a document, not tribal knowledge — see docs/THIRD_PARTY_POLICY.md."""
    policy = REPO / "docs" / "THIRD_PARTY_POLICY.md"
    assert policy.is_file(), "missing docs/THIRD_PARTY_POLICY.md"
    body = policy.read_text(encoding="utf-8")
    # The rule ids the manifest, the notices and CONTRIBUTING all point at.
    for rule in ("T-1", "T-2", "T-3", "T-4", "T-5"):
        assert rule in body, f"policy is missing rule {rule}"
    assert "packages/craft-launcher" in body, "the policy must carry the worked example"
