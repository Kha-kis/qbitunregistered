# qbitunregistered Codebase Architecture

## Overview

**qbitunregistered** is a comprehensive Python automation tool for managing torrents in qBittorrent. It provides a modular, extensible architecture for handling torrent lifecycle management including orphaned file detection, unregistered torrent identification, intelligent tagging, and seeding management.

The application follows a **plugin-like pattern** with a centralized coordinator (main script) that orchestrates specialized modules for different operational concerns.

## Architecture Principles

1. **Separation of Concerns**: Each module handles a specific domain (tagging, orphaned files, seeding limits)
2. **Batched API Operations**: Optimized to reduce qBittorrent API calls from O(N) to O(K) where K is number of unique configurations
3. **Type Safety**: Protocol-based type hints decouple from qbittorrent-api implementation details
4. **In-Memory Caching**: Single-execution caching reduces redundant API calls within one script run
5. **Configuration-Driven**: All behavior controlled via JSON config with CLI overrides
6. **Error Resilience**: Operations fail gracefully with comprehensive logging; one module failure doesn't block others
7. **Dry-Run Support**: All operations support dry-run mode for safe testing

## Core Components

### 1. Main Orchestrator: `qbitunregistered.py`

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
6. Execute enabled operations (orphaned check, unregistered check, tagging, etc.)
7. Log cache statistics
8. Report operation summary
9. Logout and cleanup
```

**Key Features**:
- Exit codes for CI/CD integration (0=success, 1=general error, 2=config error, 3=connection error)
- Graceful exception handling per operation (failure in one doesn't block others)
- Torrents fetched once and passed to all modules (avoid redundant API calls)
- Operation results tracked for summary reporting

### 2. Utility Modules

#### `utils/types.py` - Protocol Definitions
**Purpose**: Type hints without coupling to qbittorrent-api implementation

**Key Protocols**:
- `TorrentInfo`: Expected interface for torrent objects
- `TorrentFile`: File information protocol
- `QBittorrentClient`: Client interface defining all used methods
- `TorrentStateEnum`: State enumeration protocol

**Design Pattern**: Runtime checkable protocols allow type checking while remaining agnostic to concrete implementations.

#### `utils/config_validator.py` - Configuration Management
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

#### `utils/cache.py` - API Call Caching
**Purpose**: In-memory caching with TTL to reduce redundant API calls

**Design**:
- Simple TTL-based cache (default 300 seconds)
- Global singleton instance accessible to all modules
- Decorator pattern for easy application to functions
- Sentinel object to distinguish cache misses from cached None values
- Automatic periodic cleanup triggered after threshold accesses or time

**Cache Key Generation**:
- Function qualified name + arguments + kwargs
- JSON serialization for deterministic keys (falls back to pickle hash)
- `skip_first_arg` flag for methods (skips 'self' or 'client')

**Use Cases**:
- Tracker information (cached per torrent)
- Default save paths (cached globally)
- Category information (cached globally)

**Statistics Tracking**:
- Hit/miss counts
- Cache size
- Hit rate percentage

#### `utils/tracker_matcher.py` - Tracker Matching
**Purpose**: Match tracker URLs against configured patterns

**Matching Strategy**:
1. Parse URL and extract domain
2. Case-insensitive domain matching (prevents false positives)
3. Fallback to substring matching for non-standard URLs (DHT, PeX, LSD)

**Design Pattern**: Decouples tracker identification from seeding management and tagging logic.

#### `utils/rate_limiter.py` - Rate Limiting (Unused)
**Purpose**: Token bucket rate limiter for API calls

**Current Status**: Not used in production code due to aggressive batching optimization.
**Future Use Cases**: Long-running daemon mode, rate-limited external services, environments with strict API quotas.

### 3. Script Modules (Operations)

#### `scripts/unregistered_checks.py` - Identify & Handle Unregistered Torrents

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

#### `scripts/orphaned.py` - Detect & Delete Orphaned Files

**Core Responsibility**: Find files on disk not associated with any torrent

**Optimization Strategy** (4000+ → 15-20 API calls):
- Fetch all torrent metadata once
- Build set of files in-memory (fast)
- Scan disk against in-memory set (fast)
- Cache resolved paths per unique save_path (reduces syscalls 1M+ → ~1K)
- Filter save paths to only needed directories

**Scanning**:
1. Get default save path and category save paths (cached)
2. Remove redundant subdirectories (keep only top-level paths)
3. Build set of all files referenced by torrents
4. Scan disk directories
5. Identify orphaned files using glob pattern exclusions
6. Delete or report based on dry-run flag

**Exclude Patterns**:
- File patterns: glob syntax (e.g., `*.tmp`, `*.part`, `*.!qB`)
- Directory patterns: exact paths (for performance, must be absolute)

#### `scripts/tag_by_tracker.py` - Tagging by Tracker

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

**Share Limits Integration**: Applies seed_time_limit and seed_ratio_limit from tracker config.

#### `scripts/seeding_management.py` - Apply Seed Limits

**Core Responsibility**: Enforce seed time and ratio limits per tracker

**Architecture**:
- `find_tracker_config()`: Locate matching tracker in config (uses cached tracker fetching)
- `apply_seed_limits()`: Consolidated batched application of both time and ratio limits
- `_fetch_trackers()`: Cached tracker API calls

**Batching**: Groups torrents by (time_limit, ratio_limit) tuple
- Reduces API calls for 1000 torrents from 1000 to number of unique configurations

**Limit Values**:
- `-2`: Use global client defaults
- `-1`: No limit
- `0+`: Specific limit (minutes for time, ratio for ratio)

#### `scripts/tag_by_age.py` - Tagging by Torrent Age

**Core Responsibility**: Tag torrents based on completion age

**Time Buckets**: Configurable age thresholds for categorizing torrents

#### `scripts/tag_cross_seeding.py` - Cross-Seeding Detection

**Core Responsibility**: Identify and tag torrents seeding on multiple trackers

**Detection**: Analyzes tracker count and status to identify cross-seeding patterns

#### `scripts/auto_remove.py` - Automatic Removal

**Core Responsibility**: Remove completed torrents matching criteria

**Criteria**: Based on completion status and optional tag filters

#### `scripts/auto_tmm.py` - Automatic Torrent Management

**Core Responsibility**: Enable Auto TMM for torrents with category changes

**Configuration**:
- `auto_tmm_enabled`: Global enable/disable
- `torrent_changed_tmm_enabled`: When torrent contents change
- `save_path_changed_tmm_enabled`: When save path changes
- `category_changed_tmm_enabled`: When category changes

#### `scripts/create_hardlinks.py` - Hard Link Creation

**Core Responsibility**: Create hard links for completed torrents in target directory

**Use Cases**: Organize completed downloads without duplicating storage

#### `scripts/torrent_management.py` - Basic Control

**Core Responsibility**: Pause/resume operations

**Functions**:
- `pause_torrents()`: Pause all torrents
- `resume_torrents()`: Resume all torrents

### 4. Scheduler: `scheduler.py`

**Purpose**: Run qbitunregistered.py on a schedule

**Architecture**:
- Loads scheduled_times from config.json
- Uses `schedule` library for cron-like scheduling
- Executes qbitunregistered.py as subprocess with 1-hour timeout
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
  "delete_tags": ["unregistered"],     // Tags to delete
  "delete_files": {
    "unregistered": false              // Whether to delete files too
  },

  "exclude_files": ["*.tmp", "*.part"],    // Orphaned check exclusions
  "exclude_dirs": ["/path/exclude/*"],     // Directory exclusions

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

  "scheduled_times": ["09:00", "15:00"]    // For scheduler.py
}
```

### CLI Override System

- Command-line arguments override config.json values
- Pattern: `arg.value or config.get('key')`
- Supports:
  - Host, username, password
  - Target directory
  - Log level and log file
  - Exclude patterns
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
- Orphaned files: Fetch all once, build set, scan disk = 15-20 calls

### Caching Strategy

**Cache Scope**: In-memory, cleared between script runs
**TTL**: 300 seconds default
**Keys Generated**: `(prefix, function_name, args, kwargs)` → JSON/pickle hash

**Cached Operations**:
1. Tracker fetching: `_fetch_trackers(client, torrent_hash)` → O(N) to O(1)
2. Default save path: `_get_default_save_path(client)` → O(N) to O(1)
3. Categories: `_get_categories(client)` → O(N) to O(1)

## Error Handling Patterns

### Design Principles

1. **Per-Operation Isolation**: One operation's failure doesn't block others
2. **Comprehensive Logging**: All errors logged with context and recommendations
3. **Result Tracking**: Operations marked as succeeded/failed in summary
4. **Exit Codes**: CLI-friendly exit codes for scripting and CI/CD

### Exception Categories

- `ConfigValidationError`: Configuration issues (exit 2)
- `APIConnectionError`: qBittorrent connectivity (exit 3)
- General exceptions: Logged but operations continue (exit 1 if any failed)

### Error Recovery

- Configuration validation happens early (before connection)
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
   - Cache tracker info across operations
   - Cache category and save path info
   - TTL prevents stale data while reusing results

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
@cached(ttl=300, key_prefix="tracker_config", skip_first_arg=True)
def _fetch_trackers(client, torrent_hash):
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

## Extension Points

### Adding New Operations

1. Create new module in `scripts/`:
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
```

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
4. **Performance Tuning**: Configurable cache TTLs and batch sizes
5. **Metrics Export**: Prometheus-style metrics output

