# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.1] - 2026-07-25

### Added

- Completion timing for impact analysis, selected operations, and the complete
  CLI execution
- Per-execution tracker and torrent-file cache statistics, including API fetch
  attempts
- Regression coverage for long-lived metadata reuse and torrents added,
  removed, or re-added during orphan scans

### Changed

- Torrent tracker and file metadata now remain cached for one complete CLI
  execution, even when a filesystem scan takes longer than the general
  five-minute cache lifetime
- Unregistered impact analysis, unregistered execution, tracker-based tagging,
  and seeding management now share one canonical tracker metadata fetch
- Orphan discovery now uses the shared torrent-file metadata fetch and
  rebuilds ownership from a refreshed torrent snapshot and current file
  mappings after the filesystem walk. The refreshed mappings replace earlier
  execution-cache entries for later consumers.

### Fixed

- Files claimed by a torrent added during a long orphan scan are no longer
  reported or processed as orphaned
- Same-hash torrent re-adds, qBittorrent file renames, and save-path changes
  during a scan no longer leave currently owned files in the orphan plan
- A torrent metadata failure is accepted as a concurrent removal only after a
  validated fresh qBittorrent snapshot proves that its hash is absent. Active,
  malformed, or uncertain ownership continues to fail closed.

## [2.1.0] - 2026-07-24

### Added
- Automated, tag-driven GitHub Release workflow with wheel and source
  distribution artifacts
- Windows test and installed-wheel smoke coverage on Python 3.11 and 3.14
- Package metadata, MIT license file, private vulnerability reporting policy,
  Dependabot configuration, and a `--version` command
- Validated `scheduled_operations` configuration so the built-in scheduler
  forwards an explicit maintenance operation set and rejects scheduled hard-link
  jobs without an absolute target directory
- Installable `qbitunregistered` and `qbitunregistered-scheduler` console commands
- Package-build smoke testing in CI
- Release artifact upload/download round-trip testing in pull-request CI
- Disposable qBittorrent 5.2.3 acceptance coverage that verifies live
  authentication and non-mutating pause behavior in dry-run mode
- Codex-compatible `AGENTS.md` guidance for Python implementation and review
- BasedPyright development dependency, project configuration, language-server
  instructions, and required CI type analysis
- **qBittorrent 5.2 API-key authentication** as an alternative to username/password credentials
- Tracker error status support for qBittorrent WebAPI v2.13+
- **Dry-Run Impact Preview**: New impact analysis system that shows what will happen before executing operations
  - Interactive confirmation prompt for non-dry-run operations
  - Comprehensive preview showing torrents to delete, tag, pause/resume, and orphaned files
  - Disk space calculation showing how much will be freed
  - Automatic warnings for large operations (>50GB or >20 torrents)
  - Detailed operation summaries with affected torrent counts
  - New `--yes` / `-y` flag to skip confirmation prompt (for automation/cron)
  - Non-interactive environment detection (prevents hangs in CI/CD)
  - New module `qbitunregistered.impact` with `ImpactSummary` class
  - 26 comprehensive tests for impact analysis

### Changed
- Application modules now live in the installable `qbitunregistered` package
- Auto-remove batches all completed torrent hashes into one qBittorrent API
  call and reports batch failures to the operation summary
- `pyproject.toml` is the single source for runtime and development dependencies
- Added a generated `uv.lock` for reproducible development environments
- GitHub Actions use the current Node 24 action releases
- Removed Claude-specific workflows and configuration; Codex review now uses
  repository guidance from `AGENTS.md`
- **Python 3.11+ required** to align with supported Python releases and current `qbittorrent-api`
- **qbittorrent-api 2026.5.3+ required** for native API-key authentication
- **Configuration Validation**:
  - Refactored `qbitunregistered.config.validate_config` into focused helper functions for easier maintenance and testing
  - Added stricter validation for Notifiarr settings:
    - `notifiarr_key` and `notifiarr_channel` must be provided together
    - `notifiarr_channel` must be a numeric Discord channel ID string (17–20 digits)
  - Added validation for `recycle_bin` configuration (absolute path requirement, directory and writability checks)

### Deprecated

- The root `qbitunregistered.py` and `scheduler.py` source-checkout commands
  remain supported throughout 2.x for existing automation but are planned for
  removal in 3.0. New automation should use the installed
  `qbitunregistered` and `qbitunregistered-scheduler` commands.

### Fixed
- Windows execution no longer assumes descriptor-based permission changes are
  available during cross-filesystem recycle moves, and the example
  configuration no longer embeds a Unix-only recycle-bin path
- Combined hard-link and unregistered file-cleanup runs now create the confirmed
  hard links before deleting or recycling source files. Failed or stale-source
  hard-link plans, uncovered completed sources, and unrelated files occupying
  required destinations block the dependent cleanup and produce a truthful
  failed summary, notification, and exit status.
- Invalid non-boolean `dry_run` configuration now fails before connecting or
  mutating, while explicit `--dry-run` and `--no-dry-run` still take precedence
- Impact analysis now covers every mutating flag, shows concrete orphaned,
  auto-remove, and hard-link targets, reuses confirmed filesystem plans, and
  aborts on analyzer failure or conflicting hard-link destinations
- Orphan impact previews now distinguish permanent deletion from recycle-bin
  moves. Recycled bytes are reported as data to move rather than disk space to
  free, and operation notifications use the same action-specific wording.
- Cross-seed impact previews now show contradictory tag removals before
  confirmation
- Orphan cleanup now binds previewed paths to immutable file identities and
  refuses missing, modified, substituted, non-regular, or symlinked targets.
  Immediately before mutation, it also refreshes qBittorrent ownership without
  cache and aborts the entire confirmed plan if a target is now owned or
  ownership cannot be established. Canonical default, category, and current
  torrent save roots are preserved during empty-directory pruning, while
  nested empty parents below those roots are removed. Any incomplete file
  cleanup now fails the operation instead of logging success: recycle batches
  roll prior moves back, while permanent runtime partials report exact
  completed and planned counts.
- Unregistered preview and execution now share one ownership/deletion plan,
  report the exact file action, build one per-run ownership index, and refresh
  qBittorrent ownership state before file mutation. Current delete tags are
  revalidated before deletion, and uncertainty or tag removal preserves the
  torrent and rolls back any pending recycle move
- Fully selected cross-seed owner groups now delete or recycle each canonical
  shared path once instead of treating another planned deletion as a surviving
  owner. Torrent-only, ineligible, and unselected owners still preserve shared
  content, and grouped recycle failures roll every completed move back.
- Unregistered deletion now matches comma-separated tags exactly, validates
  every `delete_files` value, and honors the global `use_delete_files` gate
- Permanent deletion now performs the same fail-closed file discovery and
  cross-seed ownership scan as recycle-bin deletion
- Recycle-bin moves refuse destination overwrites, reject non-regular sources,
  and roll back earlier files when an unregistered torrent cannot be moved
  completely; incomplete moves now fail the operation and produce a non-zero
  CLI result instead of being reported as successful. Files are also restored
  if the subsequent torrent deletion fails.
  Source removal now uses atomic capture into a private staging directory before
  identity verification, so same- and cross-filesystem moves do not unlink a
  concurrently inserted replacement; restoration conflicts report a preserved
  recovery path. Cleanup also compares complete file state before removing a
  verified destination, preventing inode reuse from discarding the remaining
  copy. Internal staging and recovery directories are automatically excluded
  from later orphan discovery and pruning.
  A source-directory durability error after unlink is logged without losing the
  completed move's rollback record
- Hard-link planning rejects symlinked files that resolve outside the torrent
  content directory
- API caches are cleared at the start of each CLI execution and isolated by
  client within that execution; an explicitly blank CLI API key correctly
  selects username/password authentication
- Empty or whitespace-only `--recycle-bin` overrides are rejected before
  connection so they cannot disable a configured recycle bin and select
  permanent deletion
- Scheduler runs now forward the selected configuration path to the application
- The root scheduler compatibility wrapper again defaults to its adjacent
  `config.json` and runs scheduled children from the source checkout, preserving
  absolute-path cron and systemd setups without requiring package installation
- Tracker-tag impact preview now uses the same URL matcher and required-tag gate
  as the real operation
- Unregistered impact preview now requires the same tracker error statuses as the real operation
- Empty or omitted API keys now fall back to username/password authentication
- Configuration-based `dry_run` is no longer overridden by the CLI argument's default value
- **Notification Reliability & Security**:
  - Improved `NotificationManager` retry logic with an explicit `reraise` option
  - Ensured Notifiarr HTTP error responses are surfaced to logging while sanitizing API keys from error bodies
  - Added tests around retry behavior and credential redaction for Notifiarr notifications

### Changed
- Operations now show preview before execution unless `--yes` flag is used
- Improved user feedback with clearer understanding of pending changes

## [2.0.0] - 2025-11-06

### ⚠️ BREAKING CHANGES

- **Python 3.9+ Required**: Upgraded from Python 3.6+ to 3.9+ due to use of `Path.is_relative_to()` and modern type hints
  - See [Upgrading Guide](README.md#upgrading) for migration instructions
- **Exclude Directory Validation**: Relative paths in `exclude_dirs` now raise `ConfigValidationError` instead of logging warnings (security requirement)

### Added

#### Performance Optimizations (100-200x Improvement)
- **API Call Batching**: Reduced API calls from ~4,000 to ~15-20 per run through intelligent batching
  - Batched tag operations in `tag_by_tracker.py`
  - Batched torrent management operations
  - Single-pass seed limit application
- **Caching Layer**: Implemented TTL-based in-memory cache (`utils/cache.py`)
  - 5-minute TTL for API responses
  - Sentinel pattern for None value handling
  - Cache hit/miss statistics tracking
  - Per-client cache scoping to prevent contamination
- **Path Resolution Caching**: Reduced syscalls by 99.9% (1M+ → ~1K for large torrent sets)
  - Caches resolved save paths across torrents
  - Eliminates redundant `Path.resolve()` calls
- **Regex Pre-compilation**: Converts O(n) fnmatch operations to O(1) regex matching
  - Pre-compiles file and directory patterns
  - Significant speedup for pattern-heavy operations

#### Features
- **Configurable Logging**:
  - `log_level` config option (DEBUG/INFO/WARNING/ERROR/CRITICAL)
  - `log_file` config option for persistent logging (ideal for cron jobs)
  - CLI arguments `--log-level` and `--log-file` to override config
- **Operation Summary**: End-of-run summary showing succeeded/failed operations
- **Progress Indicators**: Added `tqdm` progress bars for long-running operations
  - Hard link creation
  - Auto-remove operations
  - Tag operations
- **Comprehensive Test Suite**: 65 passing tests across 7 test files
  - Unit tests for cache, config validation, and core operations
  - Fixtures and mocks for isolated testing
  - Edge case coverage (None caching, TTL expiry, path traversal)

#### Security
- **Path Traversal Protection**:
  - Sanitizes category names (removes `..`, replaces `/` and `\`)
  - Validates paths using `Path.is_relative_to()`
  - Resolves paths before validation
  - Applied to both directory and single-file torrents
- **Security Documentation**: Comprehensive guide in README
  - Config file permission recommendations (`chmod 600`)
  - Cron job setup with proper ownership
  - Credential management best practices

#### Documentation
- **Upgrading Guide**: Detailed Python 3.9+ migration instructions
- **Logging Configuration**: Complete examples for config.json and CLI
- **Security Section**: Best practices for production deployments
- **Performance Notes**: Documentation of caching strategy and limitations

### Changed

- **qbittorrent-api Dependency**: Updated to `>=2024.11.69` (aligns with Python 3.9+ requirement)
  - Tested with v2025.7.0
  - Supports qBittorrent v5.1.2 (Web API v2.11.4)
- **Config Validation**: Enhanced with better error messages and type safety
  - Host format validation supports both `hostname:port` and full URLs
  - Empty hostname detection
  - Seed limit type checking (integers for time, floats allowed for ratios)
- **Directory Pattern Handling**: Separated literal paths from wildcard patterns
  - Patterns compiled to regex for matching
  - Literals resolved for exact matching
  - Eliminates wasteful comparisons
- **Hard Link Creation**: Improved filesystem compatibility checks
  - Tracks all unique source devices (not just first torrent)
  - Comprehensive cross-filesystem warnings
  - Better error messages with device IDs

### Fixed

#### Critical Security Fixes
- **Path Traversal Vulnerability** (`scripts/create_hardlinks.py`):
  - Malicious torrent categories (e.g., `../../etc`) could write files outside target directory
  - Now sanitizes category names and validates resolved paths
- **Cache Key Contamination** (`scripts/orphaned.py`):
  - Cached functions dropping client parameter caused cache sharing across different qBittorrent instances
  - Added `cache_scope` parameter using `id(client)` for isolation
  - Prevents data loss when switching between servers

#### Critical Bug Fixes
- **Deletion Logic Bug** (`scripts/unregistered_checks.py`):
  - After deleting torrent, outer loop continued with stale object
  - Added deletion tracking flag and proper flow control
- **Directory Traversal Validation** (`utils/config_validator.py`):
  - Relative paths in `exclude_dirs` only logged warnings
  - Now raises `ConfigValidationError` (security requirement)

#### High-Priority Fixes
- **TOCTOU Race Condition** (`scripts/create_hardlinks.py`):
  - File creation between check and link creation caused failures
  - Added `FileExistsError` exception handling
  - Properly counts as skipped instead of error
- **Path Resolution Optimization** (`scripts/orphaned.py`):
  - Redundant `resolve()` calls created 1M+ syscalls for large torrent sets
  - Implemented save path caching (99.9% reduction)
- **Type Safety** (`utils/config_validator.py`):
  - `seed_time_limit` incorrectly allowed float values
  - Now enforces integer-only for time limits, allows floats for ratio limits

#### Code Quality Fixes
- **Mutable Default Arguments** (`scripts/orphaned.py`):
  - Used empty lists `[]` as default arguments (antipattern)
  - Changed to `None` with initialization inside function
- **Implicit Optional Type Hints**: Added `Optional` where parameters default to `None` (PEP 484 compliance)
- **Directory Pattern Logic** (`scripts/orphaned.py`):
  - Patterns were wastefully included in exact-match set
  - Separated patterns from literals for efficient handling
- **Test-Implementation Mismatch**: Fixed test expecting warning when implementation raises error

### Security

- **CVE Mitigation**: Updated `tqdm>=4.66.3` (fixes CLI injection vulnerability)
- **Path Validation**: All user-provided paths validated and sanitized
- **Config File Security**: Documentation emphasizes proper file permissions
- **Credential Handling**: No credentials logged, recommendations for secure storage

### Performance

#### Benchmarks (Typical 1,000 Torrent Setup)

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| API Calls | ~4,000 | 15-20 | **200x reduction** |
| Network Data | 600 MB | 400 MB | **33% reduction** |
| Path Resolution Syscalls | 1,000,000+ | ~1,000 | **99.9% reduction** |
| File Scanning Time | Baseline | 2x faster | **2x speedup** |
| Directory Checks | O(n²) | O(n) | **Algorithmic improvement** |

### Dependencies

- Added: `tqdm>=4.66.3` (progress bars + CVE fix)
- Updated: `qbittorrent-api>=2024.11.69` (Python 3.9+ compatibility)
- Python: `>=3.9` (was >=3.6)

### Migration Guide

See [Upgrading Guide](README.md#upgrading) for detailed migration instructions from Python 3.8 or earlier.

### Contributors

This release represents a comprehensive overhaul focused on performance, security, and reliability. Special thanks to all code reviewers who provided detailed feedback.

---

## [1.0.0] - Previous Release

*Note: This is the first release with a formal changelog. Previous versions are not documented here.*

[Unreleased]: https://github.com/Kha-kis/qbitunregistered/compare/v2.1.1...HEAD
[2.1.1]: https://github.com/Kha-kis/qbitunregistered/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/Kha-kis/qbitunregistered/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/Kha-kis/qbitunregistered/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/Kha-kis/qbitunregistered/releases/tag/v1.0.0
