# open_source

A small, well-tested Python package — a clean starting point for open-source work.

[![CI](https://github.com/YOUR-USERNAME/open_source/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR-USERNAME/open_source/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

## Features

- `greet(name, greeting=...)` — friendly, locale-agnostic greetings.
- `slugify(text, separator=...)` — URL-safe slugs, Unicode-normalised.
- Typed (`py.typed`), fully tested, zero runtime dependencies.

## Installation

```bash
pip install -e ".[dev]"
```

## Usage

```python
from open_source import greet, slugify

greet("World")
# 'Hello, World!'

greet("Ahmed", greeting="مرحبًا")
# 'مرحبًا, Ahmed!'

slugify("Hello, Open Source World!")
# 'hello-open-source-world'
```

## Development

```bash
pip install -e ".[dev]"
pytest              # tests + coverage
ruff check .        # lint
ruff format .       # format
```

## Project layout

```
open_source/
├── .github/workflows/ci.yml
├── LICENSE
├── README.md
├── pyproject.toml
├── src/
│   └── open_source/
│       ├── __init__.py
│       ├── core.py
│       └── py.typed
└── tests/
    └── test_core.py
```

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## License

Released under the [MIT License](LICENSE).
