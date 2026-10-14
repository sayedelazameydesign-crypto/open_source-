.PHONY: install test lint format demo-react demo-flow serve check clean launcher-test launcher-build

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

clean:
	rm -rf .pytest_cache .ruff_cache .coverage **/__pycache__
