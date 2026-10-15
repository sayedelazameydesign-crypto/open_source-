.PHONY: install test lint format demo-react demo-flow serve check clean launcher-test launcher-build \
        vendored-verify vendored-verify-upstream vendored-write defork-launcher

install:
	pip install -e ".[dev,server]"

test:
	pytest -q

lint:
	ruff check .

format:
	ruff format .

check: lint test

demo-react:
	python -m os_server.cli run --demo "Sum the integers from 0 to 100"

demo-flow:
	python -m os_server.cli flow --demo "Plan and execute a 3-step research task"

serve:
	python -m os_server.cli serve --demo --host 0.0.0.0 --port 8080

# Vendored ArtCraft Launcher workspace (packages/craft-launcher, ADR-0015).
# Independent of the Python toolchain: needs Rust 1.90+ on PATH.
launcher-test:
	cd packages/craft-launcher && cargo test --workspace --locked

launcher-build:
	cd packages/craft-launcher && cargo build --workspace --locked

# --- Vendored-snapshot compliance (ADR-0015, docs/THIRD_PARTY_POLICY.md T-3) --
# The hosted ArtCraft Launcher tree may only stay because it is an *unmodified*
# copy: that is the condition the trademark permission in
# packages/craft-launcher/docs/brand/LICENSE-brand.txt is granted on. These
# targets make the condition checkable instead of a claim in a README.

# Offline: working tree vs the recorded manifest. Runs inside `make test` too.
vendored-verify:
	python scripts/vendored_manifest.py verify

# Network: also re-derive the manifest from the upstream tarball at the pinned
# commit, proving the record itself matches upstream. CI runs this.
vendored-verify-upstream:
	python scripts/vendored_manifest.py verify --upstream

# Only after a deliberate snapshot refresh. Requires a full 40-char sha.
vendored-write:
	@test -n "$(COMMIT)" || { echo "usage: make vendored-write COMMIT=<40-char-sha>"; exit 2; }
	python scripts/vendored_manifest.py write --commit $(COMMIT)

# FORKS ONLY — strips the ArtCraft Marks, as the brand licence requires of any
# modified or derived distribution. Refuses to run in this repository: our copy
# is pristine and we neither build nor ship the launcher. Always dry-run first:
#   make defork-launcher ARGS="--i-am-forking --dry-run"
defork-launcher:
	./scripts/defork_launcher.sh $(ARGS)

clean:
	rm -rf .pytest_cache .ruff_cache .coverage **/__pycache__
