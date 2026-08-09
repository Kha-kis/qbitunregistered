# Bulk Tracker Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ordinary torrent acquisition plus one exact tracker request per torrent with one operation-aware bulk tracker snapshot while preserving compatibility and fail-closed behavior.

**Architecture:** `cli.py` selects the initial torrent-list transport from the requested operations. A tracker-focused cache API in `seeding_management.py` validates and preloads embedded mapping data into the same execution-local, client-scoped cache used by exact fallback, so existing preview and execution consumers remain transport-agnostic.

**Tech Stack:** Python 3.11+, `qbittorrent-api`, pytest, `unittest.mock`, BasedPyright CLI and language server, mypy, Black, Flake8, repository tracker gauntlet.

## Global Constraints

- Read and follow `/home/khak1s/projects/qbitunregistered/AGENTS.md` before changing code.
- Every Python implementation and review task uses a `python_pro` agent.
- Use test-driven development: add one focused failing test, run it and record the expected failure, then write the minimum production change and rerun it.
- Use BasedPyright for navigation and diagnostics. Run `uv run basedpyright` and exercise `uv run basedpyright-langserver --stdio` through an LSP client for changed Python files.
- Preserve Python 3.11 through 3.14 support, CLI flags, JSON fields, exit codes, installed commands, and the 2.x compatibility wrappers.
- Do not add dependencies, persistent storage, configuration fields, or CLI flags.
- Dry-run must not mutate qBittorrent or the filesystem. Uncertain tracker identity or metadata must fail closed.
- Embedded trackers must be read through the mapping interface; never access a torrent's `.trackers` attribute while preloading.
- Cache entries remain in-memory, execution-scoped, and scoped by `id(client)`.
- A rejected optional bulk request and an omitted `trackers` key may use exact compatibility fallback. A present malformed `trackers` value must not use exact fallback.
- Keep fresh disappearance and pre-mutation safety snapshots intact; do not cache them away.
- Do not access a live qBittorrent instance or media filesystem during implementation or verification.

---

### Task 1: Shared Tracker Cache Priming and Matching Compatibility

**Files:**
- Modify: `qbitunregistered/operations/seeding_management.py`
- Modify: `qbitunregistered/operations/unregistered_checks.py`
- Modify: `tests/test_tag_operations.py`
- Modify: `tests/test_unregistered_checks.py`

**Interfaces:**
- Consumes: `qbitunregistered.cache.get_cache()` and its `get(..., namespace="torrent_trackers")` / `set_for_execution(...)` methods.
- Produces: `prime_torrent_trackers(client: QBittorrentClient, torrents: Sequence[Any]) -> None`.
- Produces: `MalformedEmbeddedTrackerMetadataError`, which distinguishes a rejected successful bulk payload from an unavailable exact read.
- Preserves: `fetch_torrent_trackers(client, torrent_hash, *, cache_scope)` and `find_tracker_config(...)` public signatures.

- [ ] **Step 1: Add failing tests for valid, omitted, malformed, duplicate, and client-isolated priming**

Add focused tests that call the real cache functions. Use literal embedded mappings and make the qBittorrent exact endpoint the only mock because it is the external boundary:

```python
def test_primed_tracker_metadata_replaces_exact_reads() -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import (
        fetch_torrent_trackers,
        prime_torrent_trackers,
    )

    clear_cache()
    client = Mock()
    embedded = [{"url": "https://tracker.example/announce", "status": 2, "msg": ""}]

    prime_torrent_trackers(client, [{"hash": "embedded-hash", "trackers": embedded}])

    assert fetch_torrent_trackers(client, "embedded-hash", cache_scope=id(client)) == embedded
    client.torrents_trackers.assert_not_called()


def test_omitted_tracker_metadata_falls_back_only_for_that_torrent() -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import (
        fetch_torrent_trackers,
        prime_torrent_trackers,
    )

    clear_cache()
    client = Mock()
    client.torrents_trackers.return_value = [{"url": "https://legacy.example/announce"}]
    embedded = [{"url": "https://bulk.example/announce"}]
    prime_torrent_trackers(
        client,
        [
            {"hash": "bulk-hash", "trackers": embedded},
            {"hash": "legacy-hash"},
        ],
    )

    assert fetch_torrent_trackers(client, "bulk-hash", cache_scope=id(client)) == embedded
    assert fetch_torrent_trackers(client, "legacy-hash", cache_scope=id(client)) == [
        {"url": "https://legacy.example/announce"}
    ]
    client.torrents_trackers.assert_called_once_with(torrent_hash="legacy-hash")


@pytest.mark.parametrize("malformed", [None, "not-a-list", b"not-a-list", {"url": "wrong-shape"}])
def test_present_malformed_embedded_trackers_fail_without_exact_fallback(malformed: object) -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import (
        fetch_torrent_trackers,
        prime_torrent_trackers,
    )

    clear_cache()
    client = Mock()
    prime_torrent_trackers(client, [{"hash": "bad-hash", "trackers": malformed}])

    with pytest.raises(RuntimeError, match="malformed tracker metadata.*bad-hash"):
        fetch_torrent_trackers(client, "bad-hash", cache_scope=id(client))
    client.torrents_trackers.assert_not_called()


def test_duplicate_bulk_hashes_reject_before_any_entry_is_primed() -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import (
        fetch_torrent_trackers,
        prime_torrent_trackers,
    )

    clear_cache()
    client = Mock()
    client.torrents_trackers.return_value = [{"url": "https://exact.example/announce"}]
    torrents = [
        {"hash": "duplicate", "trackers": [{"url": "https://first.example/announce"}]},
        {"hash": "duplicate", "trackers": [{"url": "https://second.example/announce"}]},
    ]

    with pytest.raises(RuntimeError, match="missing or duplicate torrent hash"):
        prime_torrent_trackers(client, torrents)

    assert fetch_torrent_trackers(client, "duplicate", cache_scope=id(client)) == [
        {"url": "https://exact.example/announce"}
    ]
    client.torrents_trackers.assert_called_once_with(torrent_hash="duplicate")


@pytest.mark.parametrize("invalid_hash", [None, "", 42])
def test_invalid_bulk_hashes_are_rejected(invalid_hash: object) -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import prime_torrent_trackers

    clear_cache()
    with pytest.raises(RuntimeError, match="missing or duplicate torrent hash"):
        prime_torrent_trackers(
            Mock(),
            [{"hash": invalid_hash, "trackers": [{"url": "https://tracker.example/announce"}]}],
        )
```

Extend the existing client-isolation test so one client is primed and a second
client with the same torrent hash still performs its own exact read.

In `tests/test_unregistered_checks.py`, add a test that primes
`{"hash": "bad-hash", "trackers": None}`, calls the real
`_fetch_available_torrent_trackers_batch(client, ["bad-hash"])`, and asserts
`MalformedEmbeddedTrackerMetadataError`. Assert both
`client.torrents_trackers` and `client.torrents.info` were not called. This
proves malformed successful bulk data cannot enter the exact-failure
disappearance-refresh path.

- [ ] **Step 2: Run the new cache tests and record the expected red state**

Run:

```bash
uv run pytest \
  tests/test_tag_operations.py::TestTrackerTagging::test_primed_tracker_metadata_replaces_exact_reads \
  tests/test_tag_operations.py::TestTrackerTagging::test_omitted_tracker_metadata_falls_back_only_for_that_torrent \
  tests/test_tag_operations.py::TestTrackerTagging::test_present_malformed_embedded_trackers_fail_without_exact_fallback \
  tests/test_tag_operations.py::TestTrackerTagging::test_duplicate_bulk_hashes_reject_before_any_entry_is_primed -vv
```

Expected: FAIL because `prime_torrent_trackers` does not exist. Record that
failure in the task report before modifying production code.

- [ ] **Step 3: Implement the dedicated execution-scoped tracker cache**

Replace the decorator-backed internals of `fetch_torrent_trackers` with a
focused cache key and two private sentinels while preserving its signature.
Implement these exact interfaces:

```python
_TRACKER_CACHE_MISS = object()
_MALFORMED_TRACKER_METADATA = object()


class MalformedEmbeddedTrackerMetadataError(RuntimeError):
    """Raised when a successful bulk response contains malformed trackers."""


def _tracker_cache_key(torrent_hash: str, cache_scope: int) -> str:
    return f"torrent_trackers:{cache_scope}:{torrent_hash}"


def _store_tracker_metadata(
    torrent_hash: str,
    cache_scope: int,
    trackers: list[Any] | object,
) -> None:
    get_cache().set_for_execution(_tracker_cache_key(torrent_hash, cache_scope), trackers)


def prime_torrent_trackers(client: QBittorrentClient, torrents: Sequence[Any]) -> None:
    """Preload embedded tracker metadata for one authoritative snapshot."""
```

`prime_torrent_trackers` first validates every identity without mutating the
cache. For mappings, read `torrent.get("hash")`; for compatibility test doubles,
use `getattr(torrent, "hash", None)`. Reject a missing, empty, non-string, or
duplicate hash with `RuntimeError("qBittorrent returned a missing or duplicate torrent hash while preloading tracker metadata")`.

After the complete identity pass, process only mapping objects. An absent
`trackers` key leaves the entry uncached. A present non-string `Sequence`
becomes a copied list. `None`, `str`, `bytes`, `bytearray`, and other
non-sequences store `_MALFORMED_TRACKER_METADATA`. Do not read `.trackers`.

`fetch_torrent_trackers` must:

1. Continue rejecting `cache_scope=None`.
2. Read the focused key with `namespace="torrent_trackers"` and
   `_TRACKER_CACHE_MISS` as the default.
3. Raise `MalformedEmbeddedTrackerMetadataError(f"qBittorrent returned malformed tracker metadata for torrent {torrent_hash}")`
   when the cached marker is present.
4. Return the cached list for a valid entry, preserving the existing shared
   execution-snapshot behavior.
5. On a miss, call `client.torrents_trackers(torrent_hash=torrent_hash)`, apply
   the same outer-sequence validation, store a list for the execution, and
   return that list.

In `find_tracker_config`, re-raise
`MalformedEmbeddedTrackerMetadataError` regardless of `raise_on_error`; retain
the existing optional handling for other exact-endpoint exceptions. In
`_fetch_available_torrent_trackers_batch`, catch and re-raise
`MalformedEmbeddedTrackerMetadataError` before its broad exception collection.
This gives a malformed successful bulk payload zero exact reads and zero
ordinary refreshes, while unavailable exact metadata retains the existing
fresh disappearance proof.

- [ ] **Step 4: Verify cache tests are green and existing reuse/isolation stays green**

Run:

```bash
uv run pytest tests/test_tag_operations.py tests/test_unregistered_checks.py -vv
```

Expected: all selected tests PASS, including the existing preview/execution
reuse and two-client isolation tests.

- [ ] **Step 5: Add a failing pseudo-tracker priority test**

Add this behavior test to `TestTrackerTagging`. It proves the embedded transport
retains the exact endpoint's pseudo-first matching order:

```python
def test_primed_trackers_preserve_pseudo_tracker_matching_priority(self) -> None:
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.operations.seeding_management import (
        find_tracker_config,
        prime_torrent_trackers,
    )

    clear_cache()
    client = Mock()
    torrent = Mock(hash="hash")
    prime_torrent_trackers(
        client,
        [{"hash": "hash", "trackers": [{"url": "https://real.example/announce"}]}],
    )
    config = {
        "tracker_tags": {
            "real.example": {"tag": "real"},
            "dht": {"tag": "pseudo"},
        }
    }

    assert find_tracker_config(client, torrent, config) == {"tag": "pseudo"}
    client.torrents_trackers.assert_not_called()
```

- [ ] **Step 6: Run the priority test and record the expected red state**

Run:

```bash
uv run pytest tests/test_tag_operations.py::TestTrackerTagging::test_primed_trackers_preserve_pseudo_tracker_matching_priority -vv
```

Expected: FAIL because `find_tracker_config` currently sees only real embedded
trackers. Record the literal expected/actual mismatch.

- [ ] **Step 7: Implement pseudo-first matching without synthesizing status records**

Add this ordered constant beside the tracker cache helpers:

```python
_PSEUDO_TRACKER_URLS = ("** [DHT] **", "** [PeX] **", "** [LSD] **")
```

After `fetch_torrent_trackers` succeeds and `tracker_tags_config` is resolved,
have `find_tracker_config` call `match_tracker_url` for those three strings
before iterating the returned tracker mappings. Return the first matching
configuration. Do not add pseudo records to the cached list, because
unregistered status evaluation must consume only metadata supplied by
qBittorrent.

- [ ] **Step 8: Run focused formatting, tests, and type diagnostics**

Run:

```bash
uv run black --check qbitunregistered/operations/seeding_management.py qbitunregistered/operations/unregistered_checks.py tests/test_tag_operations.py tests/test_unregistered_checks.py
uv run flake8 qbitunregistered/operations/seeding_management.py qbitunregistered/operations/unregistered_checks.py tests/test_tag_operations.py tests/test_unregistered_checks.py
uv run pytest tests/test_tag_operations.py tests/test_unregistered_checks.py tests/test_impact_analyzer.py -vv
uv run basedpyright
uv run mypy qbitunregistered/operations/seeding_management.py --ignore-missing-imports
```

Expected: all commands exit zero. Use the actual BasedPyright language server
through an LSP client to request definitions/references for
`prime_torrent_trackers` and diagnostics for the changed Python files; record
zero error diagnostics or the exact findings in the task report.

- [ ] **Step 9: Commit Task 1**

```bash
git add qbitunregistered/operations/seeding_management.py qbitunregistered/operations/unregistered_checks.py tests/test_tag_operations.py tests/test_unregistered_checks.py
git commit -m "feat: preload execution tracker metadata"
```

---

### Task 2: Operation-Aware CLI Acquisition and Compatibility Fallback

**Files:**
- Modify: `qbitunregistered/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `prime_torrent_trackers(client, torrents)` from Task 1 and `_selected_operations(args)`.
- Produces: `_fetch_initial_torrents(client: QBittorrentClient, operations: Sequence[str]) -> list[TorrentInfo]`.
- Preserves: `main(argv)` acquisition errors remain `EXIT_CONNECTION_ERROR`; `KeyboardInterrupt` and `SystemExit` propagate.

- [ ] **Step 1: Add failing tests for bulk selection, ordinary selection, and rejected-parameter fallback**

Import `_fetch_initial_torrents` in `tests/test_cli.py` and add:

```python
@pytest.mark.parametrize(
    "operation",
    ["unregistered", "tag_by_tracker", "seeding_management"],
)
def test_tracker_operations_use_one_bulk_initial_snapshot(operation: str) -> None:
    client = Mock()
    embedded = [{"url": "https://tracker.example/announce"}]
    torrent = {"hash": "hash", "trackers": embedded}
    client.torrents.info.return_value = [torrent]

    assert _fetch_initial_torrents(client, [operation]) == [torrent]

    client.torrents.info.assert_called_once_with(include_trackers=True)
    client.torrents_trackers.assert_not_called()


def test_non_tracker_operations_keep_ordinary_initial_snapshot() -> None:
    client = Mock()
    torrent = Mock(hash="hash")
    client.torrents.info.return_value = [torrent]

    assert _fetch_initial_torrents(client, ["pause", "orphaned"]) == [torrent]

    client.torrents.info.assert_called_once_with()


def test_rejected_bulk_request_retries_ordinary_snapshot() -> None:
    client = Mock()
    torrent = Mock(hash="legacy-hash")
    client.torrents.info.side_effect = [TypeError("include_trackers is unsupported"), [torrent]]

    assert _fetch_initial_torrents(client, ["unregistered"]) == [torrent]

    assert client.torrents.info.call_args_list == [
        call(include_trackers=True),
        call(),
    ]
```

Add a control-flow test that gives the bulk call `KeyboardInterrupt()` and
asserts it propagates without an ordinary retry.

Also add a real-`main` malformed-metadata test before production changes. Use
a small `dict` subclass whose `__getattr__` delegates to mapping keys, with
literal `hash`, `name`, `save_path`, `content_path`, `category`, `tags`, and
`trackers=None` values. Run `main` with a temporary dry-run config and
`--unregistered` without `--yes`, patching only `create_client` and
notifications. Assert:

```python
assert result == EXIT_GENERAL_ERROR
client.torrents.info.assert_called_once_with(include_trackers=True)
client.torrents_trackers.assert_not_called()
client.torrents_delete.assert_not_called()
client.torrents_add_tags.assert_not_called()
```

- [ ] **Step 2: Run the acquisition tests and record the expected red state**

Run:

```bash
uv run pytest \
  tests/test_cli.py::test_tracker_operations_use_one_bulk_initial_snapshot \
  tests/test_cli.py::test_non_tracker_operations_keep_ordinary_initial_snapshot \
  tests/test_cli.py::test_rejected_bulk_request_retries_ordinary_snapshot \
  tests/test_cli.py::test_main_fails_closed_for_present_malformed_embedded_trackers -vv
```

Expected: FAIL because `_fetch_initial_torrents` does not exist. Record the
failure before production changes.

- [ ] **Step 3: Implement operation-aware acquisition**

Import `prime_torrent_trackers` with `apply_seed_limits`, define the immutable
operation set, and implement:

```python
_TRACKER_METADATA_OPERATIONS = frozenset(
    {"unregistered", "tag_by_tracker", "seeding_management"}
)


def _fetch_initial_torrents(
    client: QBittorrentClient,
    operations: Sequence[str],
) -> list[TorrentInfo]:
    """Fetch one authoritative snapshot using bulk trackers when useful."""
    if not _TRACKER_METADATA_OPERATIONS.intersection(operations):
        return cast(list[TorrentInfo], list(client.torrents.info()))

    try:
        response = client.torrents.info(include_trackers=True)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        logging.warning(
            "Bulk tracker metadata is unavailable; using compatible exact tracker reads (%s)",
            type(error).__name__,
        )
        return cast(list[TorrentInfo], list(client.torrents.info()))

    torrents = cast(list[TorrentInfo], list(response))
    prime_torrent_trackers(client, torrents)
    return torrents
```

Keep response materialization and priming outside the optional-request
exception handler so malformed successful responses do not masquerade as an
unsupported parameter.

In `main`, calculate `operations_to_run = _selected_operations(args)` after
configuration/logging resolution and before initial acquisition. Replace the
ordinary request with `_fetch_initial_torrents(client, operations_to_run)` and
remove the later duplicate assignment. Keep the existing outer acquisition
handler and exit code.

- [ ] **Step 4: Verify focused CLI behavior is green**

Run:

```bash
uv run pytest tests/test_cli.py -vv
```

Expected: all CLI tests PASS.

- [ ] **Step 5: Run focused formatting, tests, and type diagnostics**

Run:

```bash
uv run black --check qbitunregistered/cli.py tests/test_cli.py
uv run flake8 qbitunregistered/cli.py tests/test_cli.py
uv run pytest tests/test_cli.py tests/test_impact_analyzer.py tests/test_unregistered_checks.py -vv
uv run basedpyright
uv run mypy qbitunregistered/cli.py qbitunregistered/operations/seeding_management.py --ignore-missing-imports
```

Expected: all commands exit zero. Use the actual BasedPyright language server
through an LSP client to navigate `_fetch_initial_torrents` callers and publish
diagnostics for `cli.py`; record the result.

- [ ] **Step 6: Commit Task 2**

```bash
git add qbitunregistered/cli.py tests/test_cli.py
git commit -m "perf: batch initial tracker metadata"
```

---

### Task 3: User Documentation and Complete Static/Test Verification

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: final request and cache behavior from Tasks 1 and 2.
- Produces: operator-facing explanation of when bulk tracker metadata is used, how compatibility fallback works, and what remains execution-local.

- [ ] **Step 1: Update documentation to describe shipped behavior**

In `README.md`, change the tracker batching section from a future candidate
description to current behavior:

- tracker-dependent runs request one initial `includeTrackers` snapshot;
- supported servers use zero exact reads for complete embedded metadata;
- rejected optional requests or omitted per-torrent fields retain exact
  compatibility fallback;
- malformed present metadata fails closed;
- cache lifetime remains one execution and no live data is persisted.

In `ARCHITECTURE.md`, update the typical flow, cache design, cached-operation
list, and API optimization examples. State that pseudo tracker URLs are matched
before embedded real URLs for tagging/seeding compatibility but are not
synthesized into unregistered-status data.

In `CHANGELOG.md` under `[Unreleased]` / `Changed`, add the production endpoint
collapse and its compatibility/fail-closed behavior. Do not claim a wall-clock
improvement until the paired gauntlet provides measured evidence.

- [ ] **Step 2: Self-review documentation against the approved design**

Check each of these statements appears accurately and without contradiction:

1. only tracker-dependent operations request the larger response;
2. omitted/rejected optional metadata falls back;
3. malformed present metadata fails closed;
4. pseudo ordering remains compatible;
5. no persistent cache or live access was added.

Run:

```bash
git diff --check
```

Expected: exit zero.

- [ ] **Step 3: Run the complete repository verification gate**

Run exactly:

```bash
uv run black --check .
uv run flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
uv run pytest --cov=qbitunregistered --cov-report=term-missing --cov-fail-under=60
uv run basedpyright
uv run mypy qbitunregistered --ignore-missing-imports
```

Expected: all five commands exit zero, the full suite has zero failures, and
coverage remains at or above 60%. Record exact pass/skip/deselect counts and
coverage. Repeat actual LSP definition/reference/diagnostic requests across all
changed Python files and record the diagnostics.

- [ ] **Step 4: Commit Task 3**

```bash
git add README.md ARCHITECTURE.md CHANGELOG.md
git commit -m "docs: explain bulk tracker acquisition"
```

---

### Task 4: Paired Tracker Gauntlet and Branch Review Gate

**Files:**
- Do not modify repository files.
- Create gauntlet JSON only in a fresh system temporary directory.

**Interfaces:**
- Consumes: clean control revision `e90bcf1bfda9539105aff266295b061683870649` and the clean candidate branch.
- Produces: external `tracker-quick` and `tracker-full` paired artifacts containing the locked ABBA+BAAB evidence.

- [ ] **Step 1: Verify candidate cleanliness and create the detached control worktree**

Run:

```bash
git status --short
git worktree add --detach /home/khak1s/projects/qbitunregistered-bulk-tracker-control e90bcf1bfda9539105aff266295b061683870649
git -C /home/khak1s/projects/qbitunregistered-bulk-tracker-control status --short
```

Expected: both status commands print nothing. If the explicit control path
already exists, verify it is a registered clean worktree at the exact commit
instead of recreating it.

- [ ] **Step 2: Run the paired quick tracker gauntlet**

From `/home/khak1s/projects/qbitunregistered-bulk-trackers`, run:

```bash
tracker_results_dir=$(mktemp -d)
uv run python -I -S -B benchmarks/gauntlet/launcher.py \
  --profile tracker-quick \
  --paired-control /home/khak1s/projects/qbitunregistered-bulk-tracker-control \
  --paired-candidate /home/khak1s/projects/qbitunregistered-bulk-trackers \
  --output "$tracker_results_dir/tracker-quick.json"
```

Expected: exit zero. The artifact reports matching action/safety evidence,
control `(ordinary=1, bulk=0, exact=N)`, candidate
`(ordinary=0, bulk=1, exact=0)`, CPU ratio no greater than `1.0`, and
peak-memory ratio no greater than `1.25`.

- [ ] **Step 3: Run the paired full tracker gauntlet**

Reuse the same private temporary result directory in the same shell:

```bash
uv run python -I -S -B benchmarks/gauntlet/launcher.py \
  --profile tracker-full \
  --paired-control /home/khak1s/projects/qbitunregistered-bulk-tracker-control \
  --paired-candidate /home/khak1s/projects/qbitunregistered-bulk-trackers \
  --output "$tracker_results_dir/tracker-full.json"
```

Expected: exit zero with the same structural endpoint and safety gates at the
13,000-torrent workload. Record the measured CPU and peak-memory ratios from
both artifacts without copying sensitive or host-specific data into the repo.

- [ ] **Step 4: Run final PythonPro whole-branch review**

Provide the reviewer the diff from merge base
`e90bcf1bfda9539105aff266295b061683870649` through candidate `HEAD`, the
approved design, this plan, all test/type/LSP evidence, and both paired gauntlet
summaries. Require findings ordered by severity and explicit review of:

- malformed-success fail-closed behavior;
- fallback authorization boundaries;
- dry-run and preview/execution consistency;
- cache client/execution isolation;
- pseudo tracker ordering;
- endpoint counts and memory tradeoff;
- Python 3.11 compatibility.

Any Critical or Important finding receives one PythonPro fix wave, focused
tests, and one scoped re-review before the branch can proceed.

- [ ] **Step 5: Preserve results and report the merge decision**

Keep gauntlet artifacts outside the repository until the PR is merged or
abandoned. Confirm:

```bash
git status --short
git log --oneline e90bcf1bfda9539105aff266295b061683870649..HEAD
```

Expected: clean status and only intentional design, implementation, test, and
documentation commits. Do not access the live qBittorrent instance. Proceed to
the repository's PR/review workflow only after all gates are green.
