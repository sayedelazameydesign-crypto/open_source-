#!/usr/bin/env bash
# Strip the ArtCraft Marks from the vendored launcher snapshot — for FORKS only.
#
# WHY THIS EXISTS
#   packages/craft-launcher hosts storytold/craft-launcher verbatim. That is
#   permitted because it is an unmodified copy: docs/brand/LICENSE-brand.txt
#   grants use of the ArtCraft Marks only "as part of copies of this repository",
#   and requires that any modified or derived distribution remove them. So the
#   obligation lands on whoever forks *this* repository and changes the tree —
#   not on us, while our copy stays pristine. This script makes that obligation
#   one command instead of a treasure hunt.
#
# WHY IT REFUSES TO RUN HERE
#   In this repository the tree is a pinned, byte-verified snapshot
#   (tests/test_vendored_integrity.py). Removing the marks would make it a
#   modified version, break packaging/icons.sh, and gain nothing — we do not
#   build or ship the launcher. Hence the origin guard below, and the explicit
#   consent flag.
#
# WHAT IT DOES
#   1. deletes the marks: docs/brand/, assets/app-icon/, assets/social/
#   2. writes DEFORKED.md inside the tree listing every file that still
#      references a deleted asset — your replacement TODO, computed live by
#      grep rather than hardcoded, so it cannot go stale
#   3. tells you the build is now broken until you supply your own artwork
#
# USAGE
#   scripts/defork_launcher.sh --i-am-forking [--dry-run]
#   make defork-launcher ARGS="--i-am-forking --dry-run"
#
set -euo pipefail

TREE="packages/craft-launcher"
CONSENT_FLAG="--i-am-forking"
DRY_RUN=0
CONSENT=0
FORCE=0

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --i-am-forking   required. States that this checkout is a fork or derived
                   work that will be distributed, and that the ArtCraft Marks
                   must therefore be removed.
  --dry-run        report what would be deleted and what would break; change nothing.
  --force          also allow running in a checkout whose origin is the upstream
                   open_source- repository (strongly discouraged).
  -h, --help       this text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    "$CONSENT_FLAG") CONSENT=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --force)         FORCE=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# --- locate the repository root ---------------------------------------------
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT" ]]; then
  echo "FAIL not inside a git repository." >&2
  exit 2
fi
cd "$ROOT"

if [[ ! -d "$TREE" ]]; then
  echo "FAIL no $TREE here — run this from the root of a fork of open_source-." >&2
  exit 2
fi

# --- guard 1: consent --------------------------------------------------------
if [[ "$CONSENT" -ne 1 ]]; then
  cat >&2 <<EOF
REFUSED: pass $CONSENT_FLAG to confirm this checkout is a fork that will be
distributed under its own branding.

The ArtCraft Marks in $TREE are trademarks that are NOT open source
($TREE/docs/brand/LICENSE-brand.txt). They may stay only while this tree is an
unmodified copy of storytold/craft-launcher. In *this* repository it is — see
docs/VENDORED-craft-launcher.md — so nothing here needs stripping.

Run with --dry-run first to see exactly what would change:
    scripts/defork_launcher.sh $CONSENT_FLAG --dry-run
EOF
  exit 1
fi

# --- guard 2: are you sure you are not in the upstream repo? -----------------
ORIGIN="$(git remote get-url origin 2>/dev/null || echo '')"
if [[ "$ORIGIN" == *"/open_source-"* || "$ORIGIN" == *"/open_source-.git" ]]; then
  if [[ "$FORCE" -ne 1 ]]; then
    cat >&2 <<EOF
REFUSED: origin is $ORIGIN — the repository that *hosts* the pristine snapshot.

Stripping the marks here would (a) make our copy a "modified version" under the
brand licence, (b) break packaging/icons.sh, which renders from
docs/brand/artcraft-mark.svg, and (c) fail tests/test_vendored_integrity.py.

If you are preparing a fork, do it in the fork's checkout, not here.
To override this guard anyway: add --force.
EOF
    exit 1
  fi
  echo "WARN --force given: continuing in the hosting repository." >&2
fi

# --- what will be removed ----------------------------------------------------
# Marks per LICENSE-brand.txt, plus assets/social/, which ATTRIBUTION.md records
# as third-party logos used only to link to ArtCraft's own accounts.
MARK_DIRS=(
  "docs/brand"
  "assets/app-icon"
  "assets/social"
)

to_delete=()
for dir in "${MARK_DIRS[@]}"; do
  if [[ -d "$TREE/$dir" ]]; then
    while IFS= read -r -d '' file; do
      to_delete+=("${file#./}")
    done < <(find "$TREE/$dir" -type f -print0 | sort -z)
  fi
done

if [[ "${#to_delete[@]}" -eq 0 ]]; then
  echo "Nothing to do: no mark directories found under $TREE."
  exit 0
fi

# --- what will break (computed live, so it cannot go stale) ------------------
# Any reference to a deleted path, outside the directories being deleted.
broken=()
while IFS= read -r hit; do
  [[ -n "$hit" ]] && broken+=("$hit")
done < <(
  {
    grep -rIl -E 'docs/brand|assets/app-icon|assets/social|artcraft-mark|artcraft-logo' "$TREE" 2>/dev/null || true
  } | while IFS= read -r file; do
    rel="${file#$TREE/}"
    skip=0
    for dir in "${MARK_DIRS[@]}"; do
      [[ "$rel" == "$dir/"* ]] && skip=1 && break
    done
    [[ "$skip" -eq 0 ]] && printf '%s\n' "$rel"
  done | sort -u
)

# --- report ------------------------------------------------------------------
cat <<EOF
ArtCraft Marks — removal plan for $TREE

  delete ${#to_delete[@]} file(s) in ${#MARK_DIRS[@]} directories:
EOF
for dir in "${MARK_DIRS[@]}"; do
  [[ -d "$TREE/$dir" ]] && printf '    %s/%s/\n' "$TREE" "$dir"
done

cat <<EOF

  ${#broken[@]} file(s) still reference them and WILL BREAK the build:
EOF
for file in "${broken[@]}"; do
  printf '    %s\n' "$file"
done

cat <<EOF

  These are not cosmetic. packaging/icons.sh renders the .icns/.ico from
  docs/brand/artcraft-mark.svg, and crates/ui-egui/src/icons.rs,
  apps/artcraft-launcher/build.rs and src/tray.rs embed the assets at compile
  time. Supply your own artwork at the same paths, or edit these files.

  The brand licence also forbids calling a derived work "ArtCraft" or presenting
  it as an ArtCraft product. You may say, in plain text, that your work is based
  on ArtCraft Launcher by the ArtCraft team.
EOF

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "DRY RUN — nothing was changed."
  exit 0
fi

echo
read -r -p "Proceed with deletion? type 'yes' to continue: " answer
if [[ "$answer" != "yes" ]]; then
  echo "Aborted; nothing was changed."
  exit 1
fi

# --- do it -------------------------------------------------------------------
for dir in "${MARK_DIRS[@]}"; do
  [[ -d "$TREE/$dir" ]] && rm -rf "$TREE/$dir"
done

cat > "$TREE/DEFORKED.md" <<EOF
# De-forked: ArtCraft Marks removed

Generated by \`scripts/defork_launcher.sh\` on $(date -u +%Y-%m-%dT%H:%M:%SZ).

This tree is **no longer** a copy of \`storytold/craft-launcher\`. The ArtCraft
name, wordmark and mark — and the third-party social logos that linked to
ArtCraft's accounts — were removed as
\`docs/brand/LICENSE-brand.txt\` (upstream) requires of any modified or derived
distribution. Removed directories: $(printf '%s ' "${MARK_DIRS[@]}").

This distribution **must not** be called ArtCraft, use the ArtCraft marks, or
present itself as an ArtCraft product. It may state, in plain text, that it is
based on ArtCraft Launcher by the ArtCraft team.

Upstream's code licence is unchanged and still applies: MIT OR Apache-2.0, with
\`LICENSE-MIT\`, \`LICENSE-APACHE\`, \`NOTICE\` and \`ATTRIBUTION.md\` retained.

## Before this builds again

Replace the removed artwork with your own at the same paths, or edit these
files that referenced it:

$(for file in "${broken[@]}"; do printf -- '- `%s`\n' "$file"; done)
EOF

echo
echo "Done. Removed ${#to_delete[@]} file(s); wrote $TREE/DEFORKED.md."
echo "Next: supply replacement artwork, rename the product, then build and test."
echo
echo "NOTE: the integrity manifest in docs/vendored/craft-launcher.sha256 no longer"
echo "      matches this tree. In a fork, delete the manifest and its test, or"
echo "      regenerate it against your own pinned source:"
echo "          python scripts/vendored_manifest.py write --commit <your-sha>"
