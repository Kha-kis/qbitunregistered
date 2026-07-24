# Contributing to qbitunregistered

Thank you for your interest in contributing to qbitunregistered! This document provides guidelines and instructions for contributing.

## Development Setup

### Prerequisites

- Python 3.11 or newer
- Git
- Docker for the disposable qBittorrent acceptance test
- qBittorrent with Web UI access only for optional manual testing

### Setup Development Environment

1. **Clone the repository**:
```bash
git clone https://github.com/Kha-kis/qbitunregistered.git
cd qbitunregistered
```

2. **Create a virtual environment**:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. **Install dependencies**:
```bash
python -m pip install -e ".[dev]"
```

If you use [uv](https://docs.astral.sh/uv/), `uv sync --extra dev` installs the
same development environment from the committed cross-platform lockfile.

4. **Run tests to verify setup**:
```bash
pytest tests/ -v
```

## Development Workflow

### Branch Strategy

We use a feature branch workflow:

- **Main branch**: `main` - stable, production-ready code
- **Feature branches**: `feature/short-name` - for new features
- **Bugfix branches**: `fix/short-name` - for bug fixes

### Creating a Feature Branch

```bash
# Ensure you're on main and up to date
git checkout main
git pull origin main

# Create feature branch
git checkout -b feature/your-feature-name
```

### Making Changes

1. **Write code** following our style guidelines (see below)
2. **Add tests** for your changes
3. **Run tests locally** before pushing:
```bash
# Run all tests
pytest tests/ -v --cov

# Run linting
flake8 .
black --check .
basedpyright
mypy qbitunregistered/
```

BasedPyright is the project language-server and primary type-analysis engine.
Run `uv run basedpyright` for a non-interactive project check. Editors and
other LSP clients can start it with:

```bash
uv run basedpyright-langserver --stdio
```

The language-server command waits for LSP messages on standard input, so use it
through an editor or LSP client rather than as an interactive shell command.
Mypy remains an advisory compatibility check.

4. **Format code**:
```bash
black .
```

### Commit Messages

Use conventional commit format:

```
<type>: <description>

[optional body]

[optional footer]
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `test`: Test changes
- `refactor`: Code refactoring
- `perf`: Performance improvements
- `chore`: Maintenance tasks

**Example**:
```
feat: add dry-run impact preview

- Implemented ImpactSummary class
- Added preview before destructive operations
- Added --yes flag to skip confirmation
- Updated tests and documentation
```

### Submitting Changes

1. **Push your branch**:
```bash
git push origin feature/your-feature-name
```

2. **Create a Pull Request**:
   - Go to GitHub and create a PR from your branch to `main`
   - Fill out the PR template
   - Ensure all CI checks pass

3. **Address review feedback**:
   - Make requested changes
   - Push additional commits to the same branch
   - Respond to comments

4. **Merge**:
   - Once approved and CI passes, the PR will be merged
   - Delete your feature branch after merge

## Code Style Guidelines

### Python Style

We follow PEP 8 with some modifications:

- **Line length**: 127 characters (configured in black)
- **Formatting**: Use `black` for automatic formatting
- **Imports**: Organize with isort (stdlib, third-party, local)
- **Type hints**: Use type hints for function signatures

### Documentation

- **Docstrings**: Use Google-style docstrings for all public functions/classes
- **Comments**: Explain *why*, not *what* (code should be self-explanatory)
- **README**: Update for user-facing changes
- **ARCHITECTURE.md**: Update for architecture changes

### Example Function

```python
from typing import Dict, List, Any
from qbitunregistered.types import TorrentInfo, QBittorrentClient


def process_torrents(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    dry_run: bool = False
) -> Dict[str, int]:
    """Process torrents according to configuration.

    Args:
        client: qBittorrent API client instance
        torrents: List of torrent info objects to process
        config: Configuration dictionary
        dry_run: If True, simulate without making changes

    Returns:
        Dictionary with processing statistics:
            - processed: Number of torrents processed
            - tagged: Number of torrents tagged
            - errors: Number of errors encountered

    Raises:
        ValueError: If config is invalid
        APIError: If qBittorrent API call fails
    """
    stats = {"processed": 0, "tagged": 0, "errors": 0}

    # Implementation here

    return stats
```

## Testing Guidelines

### Test Structure

Tests are organized in the `tests/` directory:

```
tests/
├── conftest.py           # Pytest fixtures
├── test_cache.py         # Unit tests for cache module
├── test_config_validator.py
├── integration/          # Integration tests
└── smoke/                # Smoke tests
```

### Writing Tests

1. **Unit tests**: Test individual functions in isolation
2. **Integration tests**: Test module interactions
3. **Mock external dependencies**: Use pytest-mock for API calls
4. **Test both success and failure cases**

### Example Test

```python
import pytest
from qbitunregistered.cache import SimpleCache


class TestSimpleCache:
    """Tests for SimpleCache class."""

    def test_cache_set_and_get(self):
        """Test that cache stores and retrieves values correctly."""
        cache = SimpleCache(ttl=60)
        cache.set("key1", "value1")

        assert cache.has("key1")
        assert cache.get("key1") == "value1"

    def test_cache_expiry(self):
        """Test that cached values expire after TTL."""
        cache = SimpleCache(ttl=0.1)  # 100ms TTL
        cache.set("key1", "value1")

        import time
        time.sleep(0.2)  # Wait for expiry

        assert not cache.has("key1")
        assert cache.get("key1") is None
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_cache.py

# Run with coverage
pytest --cov=. --cov-report=html

# Run only unit tests
pytest -m unit

# Run only fast tests (exclude slow)
pytest -m "not slow"
```

### Running the qBittorrent Acceptance Test

The acceptance test starts a pinned qBittorrent 5.2.3 container, binds its Web
UI to a temporary loopback-only port, and adds a synthetic torrent. It runs the
pause operation with `--dry-run`, verifies that the torrent was not stopped,
and removes the container automatically.

Docker must be running. Use the locked development environment:

```bash
uv sync --extra dev
./scripts/run-qbittorrent-acceptance.sh
```

Set `QBITTORRENT_ACCEPTANCE_PORT` to an unused port from 1024 through 65535 only
when automatic port selection is unsuitable. Do not point this test at a real
qBittorrent instance; the script intentionally manages only the disposable
container it creates.

## Continuous Integration

### GitHub Actions

All PRs and pushes trigger the main CI pipeline:

1. **Linting**: flake8, black, BasedPyright, and advisory mypy
2. **Tests**: pytest with coverage
3. **Security**: pip-audit and Bandit scans
4. **Smoke tests**: package and installed-command checks

A separate acceptance workflow runs when qBittorrent-facing code or its test
harness changes, on manual dispatch, and weekly. It validates authentication
and dry-run behavior against the pinned qBittorrent container.

### CI Must Pass

All CI checks must pass before a PR can be merged. If CI fails:

1. Check the GitHub Actions logs
2. Fix the issues locally
3. Push the fixes
4. CI will re-run automatically

## Release Process

Releases are managed by maintainers:

1. Update the version in `pyproject.toml` and `qbitunregistered/__init__.py`.
2. Move the release notes from `Unreleased` into a dated section in
   `CHANGELOG.md`.
3. Run the full test, type, security, and package-build checks from
   [AGENTS.md](AGENTS.md).
4. Merge the release preparation PR into `main`.
5. Create and push an annotated tag that exactly matches the package version,
   for example:

   ```bash
   git tag -a v2.1.0rc1 -m "Release v2.1.0rc1"
   git push origin v2.1.0rc1
   ```

The release workflow verifies the tag, runs the tests and required type
analysis, builds and checks both distributions, creates a GitHub release, and
then publishes the same artifacts to PyPI through Trusted Publishing.
Pre-release version tags are marked as GitHub pre-releases automatically.

Before the first PyPI release, configure a protected GitHub environment named
`pypi` with required reviewers. Register the PyPI Trusted Publisher for owner
`Kha-kis`, repository `qbitunregistered`, workflow `release.yml`, and
environment `pypi`. The workflow intentionally does not use a long-lived PyPI
API token.

## Getting Help

- **Issues**: Check existing issues or create a new one
- **Discussions**: Use GitHub Discussions for questions
- **Documentation**: See README.md, AGENTS.md, and ARCHITECTURE.md

## Code of Conduct

Be respectful and constructive in all interactions. We follow the [Contributor Covenant](https://www.contributor-covenant.org/).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
