# Contributing

Thanks for your interest. Issues and pull requests are welcome; for anything bigger than a fix, please open an issue
first so we can agree on the approach.

## Development setup

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/RetentionSoftware/agentbus
cd agentbus
uv sync
uv run pytest
```

Before sending a pull request, all of these must pass (CI runs them on Linux, macOS and Windows):

```bash
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

## Ground rules

- **The MCP tool set is a public API.** Agents' protocols in other projects depend on tool names, parameters and
  results. Additive changes are fine; anything else needs a strong reason, a CHANGELOG entry and a major version.
- **The hook stays fast and dependency-free.** `src/agentbus/hook.py` runs after every tool call of every agent and
  is copied into projects as a single file: standard library only, no imports from the package, never a non-zero
  exit code.
- **Schema changes go through a migration** in `src/agentbus/schema.py` (`MIGRATIONS` and `SCHEMA_VERSION`), with a
  test that upgrades a database of the previous version.
- **Business rules live in `store.py`**, which uses only the standard library; `server.py` only exposes them.
- Every change comes with tests, including the error cases.
- Run the bus only on loopback. Do not add features that expose it to a network: it has no authentication.
