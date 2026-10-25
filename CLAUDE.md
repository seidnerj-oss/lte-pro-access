# lte-pro-access

- Run tests: `uv run pytest`
- Lint: `uv run ruff check` (format: `uv run ruff format`)
- Never install anything on the LTE device except `/tmp/socat`.
- Commands are derived from `Config` fields; never hardcode ports or addresses in builders.
