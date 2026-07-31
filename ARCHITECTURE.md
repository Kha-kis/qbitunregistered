# qbitunregistered Codebase Architecture

## Overview

**qbitunregistered** is a comprehensive Python automation tool for managing torrents in qBittorrent. It provides a modular, extensible architecture for handling torrent lifecycle management including orphaned file detection, unregistered torrent identification, intelligent tagging, and seeding management.

The installable package has a centralized CLI coordinator that orchestrates specialized operation modules.

The root `qbitunregistered.py` and `scheduler.py` files are deprecated
compatibility boundaries, not application modules. They preserve commands
documented for source checkouts before packaging, including the scheduler's
adjacent-`config.json` lookup and checkout-based child execution. They contain
no business logic, remain supported throughout 2.x, and are planned for removal
in 3.0. New integrations should use the installed console commands or
`python -m qbitunregistered`.

## Architecture Principles

1. **Separation of Concerns**: Each module handles a specific domain (tagging, orphaned files, seeding limits)
2. **Batched API Operations**: Optimized to reduce qBittorrent API calls from O(N) to O(K) where K is number of unique configurations
3. **Type Safety**: Protocol-based type hints decouple from qbittorrent-api implementation details
4. **In-Memory Caching**: Single-execution caching reduces redundant API calls within one script run
5. **Configuration-Driven**: All behavior controlled via JSON config with CLI overrides
6. **Fail-Closed Safety**: Preview and file-ownership uncertainty aborts destructive work
7. **Dry-Run Support**: All operations support dry-run mode for safe testing

## Core Components

### 1. Main Orchestrator: `qbitunregistered/cli.py`

**Responsibility**: Central coordinator that orchestrates all operations
**Key Patterns**:
- Single entry point that loads config and establishes qBittorrent connection
- Argument parser supporting 15+ CLI flags for feature selection
- Sequential operation execution with per-operation error handling
- Result tracking and summary reporting

**Flow**:
```
1. Parse CLI arguments
2. Load and validate config.json
3. Setup logging (console + optional file)
4. Connect to qBittorrent API
5. Fetch all torrents once (reused by all modules)
6. Build a complete impact preview for every selected operation unless `--yes` explicitly bypasses it
7. Require confirmation for non-dry-run execution and reuse confirmed filesystem plans
8. Execute enabled operations. Orphan scanning precedes hard-link creation;
   when hard links and unregistered file cleanup are both planned, hard links
   must then succeed before the dependent deletion or recycle step can run.
   Every destructive completed-torrent source is verified against its distinct
   destination inode, including destinations that already existed.
9. Log phase timing and cache statistics, report the summary, and clean up
```

**Key Features**:
- Exit codes for CI/CD integration (0=success, 1=general error, 2=config error, 3=connection error)
- Graceful exception handling per operation (failure in one doesn't block others)
- A hard-link failure blocks only dependent unregistered file cleanup; unrelated
  selected operations continue and both failures appear in the final summary
- Torrents fetched once and passed to all modules (avoid redundant API calls)
- Operation results tracked for summary reporting

### 2. Application Modules

#### `qbitunregistered/types.py` - Protocol Definitions
**Purpose**: Type hints without coupling to qbittorrent-api implementation

**Key Protocols**:
- `TorrentInfo`: Expected interface for torrent objects
- `TorrentFile`: File information protocol
- `QBittorrentClient`: Client interface defining all used methods
- `TorrentStateEnum`: State enumeration protocol

**Design Pattern**: Runtime checkable protocols allow type checking while remaining agnostic to concrete implementations.

#### `qbitunregistered/config_validator.py` - Configuration Management
**Purpose**: Validates all configuration before execution

**Validation Scope**:
- Required fields: host, username, password
- Field types: strings, booleans, lists, dictionaries
- Host format: Supports both `hostname:port` and full URLs (`http://host:port/path`)
- Port validation: 1-65535 range
- Log levels: DEBUG, INFO, WARNING, ERROR
- Tracker configuration: seed_time_limit (-2 to 525600 minutes), seed_ratio_limit (-2 to 100.0)
- Scheduled times: HH:MM or HH:MM:SS format
- Directory paths: Must be absolute for security

**Error Handling**: Collects all errors and reports them together for better user experience.

#### `qbitunregistered/cache.py` - API Call Caching
**Purpose**: Execution-local caching to reduce redundant API calls

**Design**:
- General cached values use a 300-second TTL
- Torrent tracker and file metadata remain cached for the complete execution,
  including scans that take longer than 300 seconds
- Global singleton instance accessible to all modules
- Entries and statistics are reset when each CLI execution begins
- Decorator pattern for easy application to functions
- Sentinel object to distinguish cache misses from cached None values
- Automatic periodic cleanup triggered after threshold accesses or time

**Cache Key Generation**:
- Function qualified name + arguments + kwargs
- JSON serialization for deterministic keys (falls back to pickle hash)
- `skip_first_arg` flag for methods (skips 'self' or 'client')

**Use Cases**:
- Tracker information shared by impact, unregistered, tagging, and seeding
  operations
- Torrent file information shared by orphan discovery and ownership checks
- Default save paths (cached globally)
- Category information (cached globally)

**Statistics Tracking**:
- Hit/miss counts
- Cache size
- Hit rate percentage
- Per-namespace tracker/file hits and API fetch attempts

#### `qbitunregistered/tracker_matcher.py` - Tracker Matching
**Purpose**: Match tracker URLs against configured patterns

**Matching Strategy**:
1. Parse URL and extract domain
2. Case-insensitive domain matching (prevents false positives)
3. Fallback to substring matching for non-standard URLs (DHT, PeX, LSD)

**Design Pattern**: Decouples tracker identification from seeding management and tagging logic.

**Future Use Cases**: Long-running daemon mode, rate-limited external services, environments with strict API quotas.

### 3. Script Modules (Operations)

#### `qbitunregistered/operations/unregistered_checks.py` - Identify & Handle Unregistered Torrents

**Core Responsibility**: Detect and manage torrents with unregistered tracker messages

**Pattern Matching**:
- Exact matches: Direct string comparison (O(1) lookup)
- Starts-with patterns: Prefix matching with `starts_with:` syntax
- Security: Validates prefixes to prevent empty/universal matches

**Processing**:
1. Compile patterns into two sets (exact + starts_with) for efficient lookup
2. Iterate torrents and their trackers
3. Match tracker messages against compiled patterns
4. Tag torrents with configurable tags
5. Optionally delete torrents and files based on configuration

**Batching**: Groups deletions by tag for batch API calls.

**Key Functions**:
- `compile_patterns()`: Pre-compile config into efficient lookup structures
- `check_unregistered_message()`: O(1) message matching
- `process_torrent()`: Count unregistered trackers per torrent
- `delete_torrents_and_files()`: Batch delete with tag-based filtering

Deletion impact and execution share an immutable plan. One complete torrent
snapshot and one file-list read per torrent build a path-to-owner index, so
cross-seed checks do not rescan every torrent for every deletion candidate.
The plan records torrent-only, shared-file preservation, recycle, or permanent
deletion explicitly. Owners outside the file-deletion-eligible candidate set
protect shared content; when every owner is eligible, canonical shared paths
are claimed once across the plan. Permanent owners are deleted in one
`delete_files=True` batch. Recycle paths are moved once before all owning
torrents are deleted without files, and every completed move is rolled back if
any later move, final tag check, or group deletion fails. File-mutating
execution validates planned file identities and refreshes the full ownership
snapshot without cache before mutation. Every planned torrent's current
matching delete tag is also revalidated before its deletion request. An
incomplete recycle move preserves the torrent and raises an operation failure
so CLI summaries, notifications, and scheduled exit codes remain truthful.
Tracker reads shared by impact preview and execution batch unavailable
per-torrent metadata and tolerate it only when one fresh, validated torrent
snapshot proves every failed hash absent. Confirmed removals are omitted from
path counts and new deletion plans and carried in the immutable preview plan
so execution cannot process a same-hash re-add through the stale bulk snapshot.
An execution-time removal that conflicts with a supplied deletion plan aborts
before tagging or deletion. Active or uncertain state also fails closed before
tagging.

#### `qbitunregistered/operations/orphaned.py` - Detect & Delete Orphaned Files

**Core Responsibility**: Find files on disk not associated with any torrent

**Optimization Strategy**:
- Fetch the bulk torrent snapshot once
- Treat a validated existing regular single-file `content_path` as its exact
  owned pathname without a per-torrent file-list request
- Fetch exact file lists for multi-file boundaries only when they can overlap
  filesystem candidates; uncertain bulk metadata falls back to exact metadata
- Scan disk against in-memory set (fast)
- Cache resolved paths per unique save_path (reduces syscalls 1M+ → ~1K)
- Filter save paths to only needed directories

**Scanning**:
1. Get default save path and category save paths (cached), then add validated
   operator-supplied `orphan_scan_roots`
2. Remove redundant subdirectories (keep only top-level paths)
3. Scan disk directories and collect eligible file candidates using the
   configured exclusions
4. Refresh the torrent snapshot and rebuild exact ownership with the bulk
   single-file shortcut and candidate-targeted multi-file metadata. Invalidate
   every refreshed hash's earlier file-cache entry, then repopulate entries for
   hashes whose exact file lists are fetched
5. Remove currently owned paths from the candidate set
6. Capture device, inode, type, size, and modification time for each target
7. Delete or report from that same immutable plan

This post-walk rebuild covers added or re-added hashes, local file renames, and
save-path changes. Torrents absent from the refreshed snapshot no longer claim
ownership. If a per-torrent metadata request fails, the torrent is considered
gone only when a validated fresh snapshot proves its hash is absent. Active or
uncertain ownership raises `SafetyCheckError`.

The scan captures each filesystem candidate's identity at discovery and carries
that immutable evidence into plan construction. A candidate already absent at
plan capture is logged and omitted because the plan cannot mutate it. A
candidate that is inaccessible, non-regular, replaced after discovery, or
changes during capture still fails the complete plan closed.

Bulk boundary trust requires an absolute canonical path beneath the torrent
save path, an accessible regular file or directory, and no symlink in any
component below the canonical save root. Missing, malformed, inaccessible, or
symlinked boundaries use live exact file metadata. Directory boundaries never
protect a whole tree in exact mode. Final real-run validation uses the same
builder, limited to boundaries that can overlap the immutable candidate plan.
The supported qBittorrent client has shared mutable HTTP session and application
cache state without a thread-safety guarantee, so ownership requests remain
sequential.

Traversal authority and ownership are deliberately separate. Only the default,
category, and explicit roots are walked. A per-torrent save path outside those
roots does not expand traversal, but its exact current file paths still
participate in ownership reconciliation. Ownership compares canonical
pathnames, not inode identity, so an unregistered hardlink alias inside a
managed root remains an orphan candidate.

The impact summary carries the configured orphan action with the plan.
Permanent deletion contributes target bytes to estimated freed space; recycle
mode reports the same bytes as data to move and uses matching confirmation,
dry-run, execution-summary, and notification wording.

Before a real orphan cleanup, every planned identity is preflighted without
mutation. The current default and category roots are refreshed separately from
per-torrent ownership, and every planned path must remain beneath one of those
roots or an unchanged explicit root. Recycle execution is all-or-nothing and
rolls prior moves back if a later path fails. A permanent unlink failure cannot
restore already deleted files, so it aborts remaining cleanup with explicit
completed/planned counts. Neither path emits a success summary when a planned
file action is incomplete; the exception flows through CLI results,
notifications, and exit status.

`orphan_min_age_seconds` filters discovery by modification age and defaults to
zero. `orphan_max_candidates` is optional; a real run over the limit fails
before any file move or unlink, while dry-run preserves and warns about the
complete age-eligible target plan.

Empty-directory pruning simulates already queued child-directory removals while
walking upward, which removes nested empty parents but stops at canonical active
save roots and explicit orphan scan roots in both dry-run and mutating modes.

**Exclude Patterns**:
- File patterns: glob syntax (e.g., `*.tmp`, `*.part`, `*.!qB`)
- Directory patterns: exact paths (for performance, must be absolute)

#### `qbitunregistered/operations/tag_by_tracker.py` - Tagging by Tracker

**Core Responsibility**: Apply tags based on torrent tracker source

**Batching Strategy**: Groups torrents by tag and share limit configuration
- One API call per unique tag
- One API call per unique share limit configuration
- Performance: For 1000 torrents with 5 trackers = 5 tag calls + N limit calls (vs 1000 per-torrent calls)

**Process**:
1. For each torrent, find matching tracker in config
2. Extract tag and optional seed limits
3. Group torrents by tag for batch tagging
4. Group torrents by limits for batch limit application

**Share Limits Integration**: Applies seed_time_limit and seed_ratio_limit from
tracker configs that also define a tag. Limit-only configs are handled by the
separate seeding-management operation.

#### `qbitunregistered/operations/seeding_management.py` - Apply Seed Limits

**Core Responsibility**: Enforce seed time and ratio limits per tracker

**Architecture**:
- `find_tracker_config()`: Locate matching tracker in config (uses cached tracker fetching)
- `apply_seed_limits()`: Consolidated batched application of both time and ratio limits
- `fetch_torrent_trackers()`: Execution-scoped tracker API calls shared with
  impact and unregistered checks

**Batching**: Groups torrents by (time_limit, ratio_limit) tuple
- Reduces API calls for 1000 torrents from 1000 to number of unique configurations

**Limit Values**:
- `-2`: Use global client defaults
- `-1`: No limit
- `0+`: Specific limit (minutes for time, ratio for ratio)

#### `qbitunregistered/operations/tag_by_age.py` - Tagging by Torrent Age

**Core Responsibility**: Tag torrents based on completion age

**Time Buckets**: Configurable age thresholds for categorizing torrents

#### `qbitunregistered/operations/tag_cross_seeding.py` - Cross-Seeding Detection

**Core Responsibility**: Identify and tag torrents seeding on multiple trackers

**Detection**: Analyzes tracker count and status to identify cross-seeding patterns

Impact analysis uses the same file-structure grouping and includes removal of
an existing contradictory tag before the batched add operation.

#### `qbitunregistered/operations/auto_remove.py` - Automatic Removal

**Core Responsibility**: Remove completed torrents matching criteria

**Criteria**: Based on completion status and optional tag filters

#### `qbitunregistered/operations/auto_tmm.py` - Automatic Torrent Management

**Core Responsibility**: Enable Auto TMM for torrents with category changes

**Configuration**:
- `auto_tmm_enabled`: Global enable/disable
- `torrent_changed_tmm_enabled`: When torrent contents change
- `save_path_changed_tmm_enabled`: When save path changes
- `category_changed_tmm_enabled`: When category changes

#### `qbitunregistered/operations/create_hardlinks.py` - Hard Link Creation

**Core Responsibility**: Create hard links for completed torrents in target directory

**Use Cases**: Organize completed downloads without duplicating storage

Impact analysis builds the exact source-to-destination plan without filesystem
mutation. Execution reuses that confirmed plan and fails closed if source
inspection is uncertain or multiple sources map to one destination.

#### `qbitunregistered/operations/torrent_management.py` - Basic Control

**Core Responsibility**: Pause/resume operations

**Functions**:
- `pause_torrents()`: Pause all torrents
- `resume_torrents()`: Resume all torrents

### 4. Scheduler: `qbitunregistered/scheduler.py`

**Purpose**: Run the installed application on a schedule

**Architecture**:
- Loads and validates `scheduled_times` plus `scheduled_operations` from the selected configuration file
- Uses `schedule` library for cron-like scheduling
- Maps each configured operation to its CLI flag and executes
  `python -m qbitunregistered --config <path> <operation flags> --yes` with a
  1-hour timeout
- Accepts an optional child working directory used only by the deprecated root
  wrapper; the installed scheduler continues to inherit its process directory
- Captures and logs output
- Runs continuously until interrupted

## Configuration System

### Structure (`config.json`)

```json
{
  "host": "localhost:8080",           // qBittorrent Web UI address
  "username": "admin",                 // Credentials
  "password": "password",

  "dry_run": true,                     // Simulate without executing
  "log_level": "INFO",                 // Logging verbosity
  "log_file": "/path/to/log",          // Optional file logging

  "default_unregistered_tag": "unregistered",           // Unregistered tag
  "cross_seeding_tag": "unregistered:crossseeding",     // Cross-seeding tag
  "other_issues_tag": "issue",                          // General issue tag

  "use_delete_tags": false,            // Enable tag-based deletion
  "use_delete_files": false,           // Global gate for filesystem deletion
  "delete_tags": ["unregistered"],     // Tags to delete
  "delete_files": {
    "unregistered": false              // Whether to delete files too
  },

  "exclude_files": ["*.tmp", "*.part"],    // Orphaned check exclusions
  "exclude_dirs": ["/path/exclude/*"],     // Directory exclusions
  "orphan_scan_roots": ["/path/extra"],    // Additional absolute managed roots

  "unregistered": [                        // Patterns for unregistered detection
    "This torrent does not exist",
    "starts_with:Trump"                    // Prefix matching
  ],

  "target_dir": "/path/target",            // Hard link target
  "auto_tmm_enabled": true,                // Auto TMM settings
  "torrent_changed_tmm_enabled": true,

  "tracker_tags": {                        // Tracker-specific configuration
    "aither": {
      "tag": "AITHER",
      "seed_time_limit": 100,              // Minutes
      "seed_ratio_limit": 1.0              // Upload/download ratio
    }
  },

  "scheduled_times": ["09:00", "15:00"],   // For scheduler.py
  "scheduled_operations": ["unregistered", "orphaned"]
}
```

### CLI Override System

- Command-line arguments override config.json values
- Explicitly provided CLI values replace configuration values, including blank
  values where blank has defined behavior (such as API-key credential fallback)
- Supports:
  - Host, username, password
  - Target directory
  - Log level and log file
  - Exclude patterns
  - Additional orphan scan roots
  - All operation flags

### Configuration Precedence

1. CLI arguments (highest priority)
2. config.json settings
3. Hardcoded defaults (lowest priority)

## Data Flow Patterns

### Typical Operation Flow

```
Main Script
├─ Load config → Validate
├─ Connect to qBittorrent
├─ Fetch torrents once (cached, reused)
├─ For each enabled operation:
│  ├─ Load operation-specific config
│  ├─ Process torrents
│  ├─ Group for batching
│  ├─ Apply batch API calls
│  └─ Log results
└─ Report summary
```

### API Call Optimization Examples

**Naive Approach** (pre-optimization):
- Tag by tracker: 1000 torrents × 1000 API calls = 1000+ calls
- Seeding management: 1000 torrents × 2 API calls = 2000+ calls
- Orphaned files: 1000 torrents × 1000+ syscalls = 1000+ calls

**Optimized Approach** (current):
- Tag by tracker: Group by tag → 5 tags = 5 calls
- Seeding management: Group by limits → 3 configs = 3 calls  
- Orphaned files: one bulk snapshot plus exact file-list requests only for
  uncertain or candidate-overlapping multi-file torrents; existing regular
  single-file torrents require no file-list request

### Caching Strategy

**Cache Scope**: In-memory, cleared between script runs
**Lifetime**: 300 seconds by default; tracker and torrent-file metadata lasts
until the execution cache is cleared
**Keys Generated**: `(prefix, function_name, args, kwargs)` → JSON/pickle hash

**Cached Operations**:
1. Tracker fetching: `fetch_torrent_trackers(client, torrent_hash)` → one API
   fetch per client/torrent/execution
2. Torrent-file fetching: `fetch_torrent_files(client, torrent_hash)` → one API
   fetch per client/torrent/execution, except an explicit post-orphan-scan
   refresh that replaces a possibly older entry
3. Default save path: `_get_default_save_path(client)` → O(N) to O(1)
4. Categories: `_get_categories(client)` → O(N) to O(1)

## Error Handling Patterns

### Design Principles

1. **Preflight Safety**: Incomplete impact analysis aborts all operations before mutation
2. **Comprehensive Logging**: All errors logged with context and recommendations
3. **Result Tracking**: Operations marked as succeeded/failed in summary
4. **Exit Codes**: CLI-friendly exit codes for scripting and CI/CD

### Exception Categories

- `ConfigValidationError`: Configuration issues (exit 2)
- `APIConnectionError`: qBittorrent connectivity (exit 3)
- General exceptions: Logged but operations continue (exit 1 if any failed)

### Error Recovery

- Configuration validation happens early (before connection)
- File discovery and cross-seed ownership scans raise a distinct safety failure;
  callers do not interpret that failure as an empty result
- Unregistered recycle-bin moves are all-or-nothing: a partial failure rolls
  prior files back before the torrent is preserved
- A qBittorrent deletion failure after a successful recycle move restores the
  moved files, refusing to overwrite a path created concurrently
- A source-directory fsync error after unlink is reported as durability
  uncertainty, while the completed recycle move remains recorded and available
  for rollback
- Same- and cross-filesystem recycle moves never unlink the public source
  pathname after a check. They atomically rename that entry into a random,
  mode-0700 staging directory on the source filesystem, verify the captured
  object, and unlink only its private name. A captured replacement is restored
  through an exclusive hard link or retained at a reported recovery path when
  restoration would overwrite a newer entry. This portable guarantee protects
  ordinary concurrent pathname replacement; hostile processes running as the
  same OS account remain outside the filesystem permission boundary. Failure
  cleanup requires the source to match its complete captured file state, not
  only its device and inode, so inode reuse cannot authorize deletion of the
  verified destination or recovery copy.
- The shared `.qbitunregistered-recycle-` directory prefix is reserved for
  internal recovery. Orphan discovery, immutable plan construction, execution,
  and empty-directory pruning all exclude paths beneath that prefix without
  relying on operator configuration.
- Final ownership revalidation closes the preview-to-execution interval, but
  qBittorrent provides no transaction spanning its final response and the
  subsequent deletion request; that short external race remains unavoidable
- Connection tested before operations begin
- Each operation wrapped in try-except with specific logging
- Cache cleanup failures are non-critical (debug-level logging)

## Performance Optimizations

### Major Optimizations

1. **Batched API Calls**: O(N) → O(K) reduction
   - Tag operations: Group by tag
   - Seed limits: Group by configuration
   - Deletions: Group by tag

2. **Path Resolution Caching**:
   - Cache `Path.resolve()` results per unique save_path
   - Reduces syscalls from 1M+ to ~1K for 1000 torrents

3. **In-Memory Set Operations**:
   - Build torrent file set once
   - Scan disk against set (O(1) lookup)
   - Identify orphaned files efficiently

4. **Pattern Compilation**:
   - Compile unregistered patterns once at startup
   - Two sets: exact matches (O(1)) + starts_with (O(n))
   - Avoid regex compilation overhead

5. **API Call Caching**:
   - Cache tracker and torrent-file metadata across operations for the complete
     execution
   - Cache category and save path info
   - Final destructive ownership checks bypass the cache

### Metrics

- **Before**: 4000+ API calls for typical 1000-torrent operation
- **After**: 15-20 calls for same operation
- **Result**: Sub-second execution vs multiple minutes

## Type Safety

### Protocol-Based Design

- Protocols define expected interfaces without concrete classes
- Decouples from qbittorrent-api library implementation
- Enables testing with mock objects
- Better IDE support and type checking

### Key Protocols

```python
@runtime_checkable
class TorrentInfo(Protocol):
    hash: str
    name: str
    save_path: str
    category: str
    tags: str
    state_enum: TorrentStateEnum
    added_on: int
    completion_on: int
    seeding_time: int
    ratio: float
    files: Optional[List[Any]]
```

## Important Design Patterns

### 1. Plugin Pattern
Each script module is independent and can be:
- Enabled/disabled via CLI flags
- Extended with new functionality
- Tested in isolation

### 2. Dependency Injection
- Client and config passed to functions
- Torrents list passed to avoid redundant fetching
- Enables testing and flexibility

### 3. Caching Decorator
```python
@cached(ttl=None, key_prefix="torrent_trackers", skip_first_arg=True)
def fetch_torrent_trackers(client, torrent_hash, *, cache_scope):
    return client.torrents_trackers(torrent_hash)
```

### 4. Batching Pattern
```python
torrents_by_tag = defaultdict(list)
for torrent in torrents:
    torrents_by_tag[tag].append(torrent.hash)
for tag, hashes in torrents_by_tag.items():
    client.torrents_add_tags(hashes, tags=[tag])  # One call per tag
```

### 5. Configuration Validation
- Early validation before operations
- Comprehensive error collection
- Type and value checking
- Clear error messages for debugging

## Key Dependencies

- **qbittorrentapi**: qBittorrent Web API client
- **schedule**: Task scheduling library
- **tqdm**: Progress bars and terminal utilities
- **pathlib**: Modern path handling (requires Python 3.9+)

## Security Considerations

1. **Config File Permissions**:
   - Contains username/password
   - Should be `chmod 600` on Linux/macOS
   - Never commit to version control

2. **Directory Validation**:
   - Exclude directories must be absolute paths (security requirement)
   - Prevents accidental relative path traversal

3. **Pattern Validation**:
   - Warns about dangerous patterns (`*`, `*.*`, `**/*`)
   - Prevents universal matching

4. **API Credentials**:
   - Can be overridden via CLI for CI/CD integration
   - Environment variables can be used with external wrappers

## Testing Architecture

- **Unit tests** in `/tests` directory
- **Test fixtures** in `conftest.py`
- **Coverage areas**:
  - Config validation
  - Orphaned file detection
  - Unregistered checks
  - Tagging operations
  - Cache operations

## Evaluator Isolation

The repository-local paired gauntlet is an evaluator-only boundary and never
contacts a live qBittorrent instance. Its source launcher binds first-party
imports to the selected clean worktree, rejects symbolic links and Windows
reparse points throughout protected package trees, and keeps installed
dependencies behind the standard library on `sys.path`. The coordinator checks
protected trees before and after the crossover; each isolated child closes the
preflight-to-import gap by checking immediately before imports and after
evaluation.

The paired coordinator accepts execution only from the validated import
bootstrap. A one-use process-local state binds the exact protected finder,
sanitized import path, source location, and site/safe-path interpreter flags
before coordinator imports proceed. Cache environment variables remain
operational inputs, not authentication markers, so direct module execution
cannot spoof the launcher boundary. Ordinary non-paired module execution is
unchanged.

One content-and-relative-path fingerprint binds the ordered installed
dependency directories for all eight crossover children. Each child verifies
that fingerprint immediately before and after evaluator execution, while the
coordinator independently rechecks it after every child. The paired artifact's
dependency digest combines that environment fingerprint with the identical
`pyproject.toml` and `uv.lock` inputs from all three worktrees. The coordinator
also requires evaluator sources, the quality bar, and both dependency inputs to
remain regular stage-0 index entries without skip-worktree or assume-unchanged
flags in every worktree.

## Extension Points

### Adding New Operations

1. Create new module in `qbitunregistered/operations/`:
```python
def new_operation(client, torrents, config, dry_run=False):
    # Implementation
    pass
```

2. Add to main orchestrator with error handling
3. Add CLI flag via argparse
4. Document in README and config example

### Adding New Tracker Config

1. Add entry to `tracker_tags` in config.json
2. Define tag and optional seed limits
3. Tracker URL matching handles the rest (domain-based)

### Adding New Exclude Pattern

1. Add to `exclude_files` or `exclude_dirs` in config.json
2. Pattern syntax:
   - Files: glob patterns (`*.tmp`)
   - Dirs: absolute paths (security)

## Monitoring and Logging

### Log Levels

- **DEBUG**: Cache operations, API calls, pattern matching
- **INFO**: Operation summaries, batch results, statistics
- **WARNING**: Configuration issues, pattern validation failures
- **ERROR**: Operation failures, connection errors

### Cache Statistics

Logged at script completion:
```
Cache stats - Hits: 45, Misses: 12, Size: 8, Hit rate: 78.95%
Metadata cache stats - trackers: 8 hits, 5 API fetches; files: 4 hits, 7 API fetches
```

The CLI also logs elapsed time for impact analysis, selected-operation
execution, and the complete command. Per-namespace misses represent metadata
API fetch attempts.

### Operation Summary

```
============================================================
OPERATION SUMMARY
============================================================
✓ Succeeded (5):
  - Orphaned file check
  - Unregistered checks
  - Tag by tracker
  - Tag cross-seeds
  - Seeding management
✗ Failed: None
============================================================
```

## Future Extensibility

1. **Long-Running Daemon**: Rate limiter available for continuous operation
2. **Custom Hooks**: Plugin system for external integrations
3. **Advanced Filtering**: Additional tag/filter combinations
4. **Performance Tuning**: Profile cache effectiveness and batch sizes
5. **Metrics Export**: Prometheus-style metrics output
