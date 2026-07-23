# Codex project guidance

## Repository map

- `qbitunregistered/cli.py` coordinates configuration, connection, previews,
  operations, notifications, and exit codes.
- `qbitunregistered/operations/` contains torrent and filesystem operations.
- `qbitunregistered/config.py` validates JSON configuration and resolves CLI
  overrides.
- `qbitunregistered/scheduler.py` runs the installed module with the selected
  configuration.
- `tests/` mirrors application behavior with pytest.
- See `ARCHITECTURE.md` for execution flow and component details.

## Python-pro agent

For substantial Python implementation, modernization, packaging, performance,
security, or review work, use the project-scoped `python-pro` custom agent in
`.codex/agents/python-pro.toml` when an independent delegated pass will improve
the result. Give it a concrete, bounded task and reconcile its findings with the
main task before changing code. Do not delegate trivial edits.

## Engineering rules

- Support Python 3.11 and newer.
- Keep dependencies and tool configuration in `pyproject.toml`; refresh
  `uv.lock` after dependency metadata changes.
- Preserve both installed console commands and root compatibility wrappers.
- Treat dry-run behavior, confirmation prompts, file deletion, recycle-bin
  moves, credentials, and scheduled `--yes` execution as safety-critical.
- Never log API keys or passwords.
- Preserve CLI compatibility unless the task explicitly authorizes a breaking
  change.
- Add regression tests for defects and cover both mutating and dry-run paths.
- Prefer batched qBittorrent calls and avoid repeated filesystem/API work.
- Update user documentation and `CHANGELOG.md` for user-visible changes.

## Verification

Use the project virtual environment when available:

```bash
.venv/bin/black --check .
.venv/bin/flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
.venv/bin/pytest -q --cov=qbitunregistered --cov-report=term --cov-fail-under=60
.venv/bin/pip-audit
.venv/bin/bandit -q -r qbitunregistered -ll
.venv/bin/python -m build
```

Smoke-test the built wheel and both console commands from outside the source
checkout. Mypy is currently advisory; report its findings honestly and improve
changed interfaces incrementally.

## Code Review Rules

- Prioritize data-loss risks, dry-run/preview divergence, authentication
  regressions, scheduler/config mismatches, packaging omissions, unsafe paths,
  and missing negative tests.
- Confirm the wheel contains runtime modules and installed commands work outside
  the repository.
- Treat style-only observations as non-blocking unless they conceal a real bug.
- Cite concrete files and reproduction conditions for every blocking finding.
