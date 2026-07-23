# qbitunregistered project guidance

`qbitunregistered` is a safety-sensitive Python CLI for qBittorrent
maintenance. Favor correctness, data preservation, and clear operator feedback
over cleverness or small performance gains.

## Environment setup

- Support Python 3.11 and newer; CI currently tests Python 3.11 through 3.14.
- Prefer the committed `uv.lock` for a reproducible development environment:

```bash
uv sync --extra dev
```

- Without uv, use a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip "setuptools>=83"
python -m pip install -e ".[dev]" build bandit pip-audit
```

- Prefer the standard library and existing dependencies. Add a third-party
  dependency only when it materially simplifies a required behavior. Put
  dependency metadata and tool configuration in `pyproject.toml`, then refresh
  and commit `uv.lock`.

## Repository map

- `qbitunregistered/cli.py` coordinates configuration, previews, operations,
  notifications, cleanup, and exit codes.
- `qbitunregistered/config.py` validates JSON configuration and resolves
  configuration behavior.
- `qbitunregistered/client.py` is the canonical authentication/client factory.
- `qbitunregistered/operations/` contains torrent and filesystem operations.
- `qbitunregistered/file_operations.py` contains shared, safety-critical file
  and recycle-bin behavior.
- `qbitunregistered/impact.py` builds the pre-operation impact preview.
- `qbitunregistered/scheduler.py` invokes the installed CLI on a schedule.
- `tests/` contains isolated pytest tests with mocked qBittorrent clients and
  temporary filesystems.
- `qbitunregistered.py` and `scheduler.py` are compatibility wrappers; keep
  business logic in the package.
- See `ARCHITECTURE.md` for execution flow and `CONTRIBUTING.md` for the human
  contribution workflow.

Do not edit generated output under `build/`, `dist/`, `*.egg-info/`,
`__pycache__/`, `.pytest_cache/`, or `.mypy_cache/`.

## Implementation rules

- Preserve CLI flags, JSON fields, exit codes, installed console commands, and
  root compatibility wrappers unless a task explicitly authorizes a breaking
  change.
- Make the smallest coherent change that solves the current task. Avoid
  speculative abstractions, configuration, hooks, and unrelated refactors.
- Ask before a major refactor or architectural shift that materially expands
  the requested scope.
- Keep orchestration in `cli.py` and focused behavior in the relevant package
  module. Keep functions single-purpose when practical, and split distinct
  responsibilities when doing so improves safety or testability. Reuse the
  protocols in `qbitunregistered/types.py` at API boundaries.
- Prefer `pathlib.Path`, context managers, and clear native Python constructs.
  Use Python 3.11 type syntax such as `list[str]` and `Value | None` on new or
  changed interfaces; do not mechanically rewrite untouched annotations.
- Use descriptive names and concise Google-style docstrings for new or changed
  public APIs. Comment on why non-obvious logic exists, not what each line does.
- Use structured logging for runtime diagnostics. Reserve `print()` for
  intentional CLI output, including failures before logging is configured.
- Do not add placeholders, `TODO`/`FIXME` comments, commented-out
  implementations, or deliberately unfinished paths unless the task explicitly
  requests scaffolding.
- Do not swallow `KeyboardInterrupt` or `SystemExit`. Handle expected failures
  narrowly and include actionable context without exposing secrets.
- Prefer batched qBittorrent calls. Cache data only within one execution and
  scope client-dependent cache entries by client.

## Configuration and authentication

- An API key is optional. A missing, blank, or whitespace-only `api_key` must
  fall back to `username` and `password`; a non-blank API key takes precedence.
- CLI values override configuration-file values. Preserve that precedence when
  adding or changing options.
- Keep validation, CLI resolution, `config.json.example`, README guidance, and
  tests aligned when configuration changes.
- Never commit a real `config.json` or include credentials in tests, examples,
  logs, exceptions, previews, notifications, or command output.

## Destructive-operation safety

- Dry-run mode must not mutate qBittorrent or the filesystem. It should report
  the same intended targets and actions as a real run.
- Keep impact previews, confirmation prompts, actual execution, notifications,
  and operation summaries consistent. Scheduled execution intentionally adds
  `--yes`, so configuration validation and dry-run coverage are critical.
- Fail closed when file ownership, cross-seeding, path safety, or API state
  cannot be established. A transient safety-check failure must not become
  permission to delete data.
- Preserve recycle-bin collision handling and original-path organization. Test
  both recycle-bin and permanent-deletion paths when either changes.

## Testing workflow

Start with the narrowest useful feedback:

```bash
uv run pytest tests/test_config_validator.py
uv run pytest tests/test_config_validator.py::TestConfigValidation::test_empty_api_key_uses_username_password
uv run black --check qbitunregistered/config.py tests/test_config_validator.py
uv run flake8 qbitunregistered/config.py tests/test_config_validator.py
uv run mypy qbitunregistered/config.py --ignore-missing-imports
```

Replace the example paths and test name with the files being changed. Tests
must not require a live qBittorrent instance, real credentials, or network
access. Use mocked clients and `tmp_path`. Add regression tests for defects and
cover success, failure, dry-run, and mutating paths as applicable.

Before completing substantive Python changes, run the full relevant CI checks:

```bash
uv run black --check .
uv run flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
uv run pytest --cov=qbitunregistered --cov-report=term-missing --cov-fail-under=60
uv run mypy qbitunregistered --ignore-missing-imports
```

Mypy is advisory in CI; report its findings honestly and improve changed
interfaces incrementally. For dependency, security, packaging, or entry-point
changes, also run:

```bash
uv run --with pip-audit pip-audit
uv run --with bandit bandit -q -r qbitunregistered -ll
uv build
```

Smoke-test the built wheel and both console commands from outside the source
checkout.

## Definition of done

- Relevant focused tests pass; the full suite passes for substantive code
  changes.
- User-visible behavior is reflected in `README.md` and `CHANGELOG.md`;
  architectural changes are reflected in `ARCHITECTURE.md`.
- Dependency changes include an updated `uv.lock`.
- Generated files and local credentials are absent from the diff.

## Code Review Rules

### Destructive behavior

- Flag any path where dry-run can mutate state, previewed targets can differ
  from executed targets, confirmation can be bypassed unintentionally, or a
  failed safety check can permit deletion. The safe path is no mutation in
  dry-run and fail-closed behavior when safety is uncertain.

### Authentication and configuration

- Flag regressions to optional API-key fallback, CLI-over-config precedence,
  secret redaction, or scheduler/config parity. A blank API key must continue
  to use validated username/password credentials.

### API and packaging boundaries

- Flag unbounded per-torrent API calls when equivalent batching is available,
  cache entries that can cross client instances, package modules missing from
  built artifacts, or installed commands that work only from the repository.
