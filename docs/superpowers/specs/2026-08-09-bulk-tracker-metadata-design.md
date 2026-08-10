# Bulk Tracker Metadata Design

## Context

Tracker-dependent runs currently acquire one ordinary torrent snapshot and then
call qBittorrent's exact tracker endpoint once for every torrent. The merged
tracker gauntlet establishes the safe replacement: qBittorrent Web API 2.15.1
can embed tracker metadata in a filtered torrent response through
`include_trackers=True`. The bounded design retains the ordinary authoritative
snapshot and requests embedded metadata for consecutive groups of at most 100
hashes, reducing the normal request shape from `N` exact reads to
`ceil(N / 100)` filtered reads without materializing one second full snapshot.

This optimization is safety-sensitive. Tracker metadata participates in
unregistered detection, impact previews, tracker tagging, seeding limits, and
pre-mutation checks. The implementation must retain the existing fail-closed
behavior and keep preview and execution bound to the same accepted snapshot.

## Goals

- Use bounded tracker batches for runs selecting `unregistered`,
  `tag_by_tracker`, or `seeding_management`.
- Reuse validated embedded metadata through the existing execution-scoped,
  client-scoped tracker cache across preview and execution.
- Preserve compatibility with qBittorrent versions or wrappers that reject the
  optional bulk argument or omit the optional field.
- Preserve existing tracker matching, disappearance reconciliation, dry-run,
  confirmation, and mutation-safety behavior.
- Demonstrate the endpoint reduction and acceptable CPU and memory behavior
  with the merged paired tracker gauntlet.

## Chosen approach

The CLI always acquires its authoritative torrent snapshot with ordinary
`client.torrents.info()`. If any selected operation depends on trackers, it
validates the snapshot's stable ordered unique hashes and requests
`client.torrents.info(torrent_hashes=batch_hashes, include_trackers=True)` for
each consecutive group of at most 100. Other runs stop after the ordinary
snapshot.

After every batch succeeds, a focused tracker-metadata helper atomically
preloads the existing tracker cache. Cache entries use the same client
identity and torrent-hash key as `fetch_torrent_trackers`, so all current
consumers receive the embedded data without learning about the transport.
There is no persistent cache, database, new dependency, configuration field,
or CLI flag.

The helper reads embedded data only through the torrent object's mapping
interface. It must not use the `.trackers` attribute because
`qbittorrent-api` implements that attribute by calling the exact tracker
endpoint, which would recreate the `N`-request bottleneck.

### Alternatives considered

1. Always request embedded trackers. This is simpler, but it increases payload
   and memory for operations that never inspect tracker metadata.
2. Let each tracker operation request its own bulk snapshot. This duplicates
   full torrent responses and risks preview/execution drift.
3. Add a persistent cache or database. Tracker state can change during a run,
   persistent invalidation would be difficult to prove safe, and the bulk API
   already removes the network bottleneck without durable state.

The ordinary snapshot plus bounded filtered batches is therefore the smallest
design that materially reduces requests and peak memory without weakening the
current execution boundary. A 100-hash batch is about 6.5 KB of pipe-delimited
64-character qBittorrent hashes before the request envelope.

## Acquisition and cache contract

The ordinary response remains the sole authoritative initial torrent list.
Before requesting or preloading entries, the helper validates every torrent
hash as a non-empty string and rejects duplicate hashes. Each batch response
must contain exactly its requested hashes once, in a shape whose identity fields
still match the authoritative snapshot. This prevents malformed or reordered
data from being attached to the wrong cache entry.

For each torrent:

- If the mapping contains a `trackers` key with a non-string sequence value,
  the helper copies that sequence into the execution-scoped cache.
- If every entry in the first batch omits the key, the optional transport is
  treated as unsupported and no bulk cache is published. Consumers use the
  existing exact endpoint.
- A partially omitted batch, or any omission after support is established,
  fails closed rather than publishing a partial cache.
- If the key is present but its value is `None`, a string, bytes, or any other
  non-sequence value, the helper stores a failure marker or equivalent rejected
  state. A consumer must fail closed without attempting an exact fallback.
  Successful-but-malformed bulk data is not evidence that the optional feature
  is unsupported.

If the first batch request itself raises, the already-acquired ordinary
snapshot is retained and tracker entries remain uncached. Existing exact reads
then provide the compatibility path. A later request failure after support is
established fails closed. Control-flow exceptions remain unswallowed.

The cache is still cleared at execution start and end and is still scoped by
`id(client)`. Preloaded data therefore cannot cross client instances or CLI
executions. Cache preloading must use a supported cache interface rather than
reconstructing decorator internals at call sites.

## Compatibility details

The exact qBittorrent tracker endpoint includes the pseudo URLs
`** [DHT] **`, `** [PeX] **`, and `** [LSD] **` before real trackers, while
embedded metadata includes only real trackers. These pseudo entries have
status zero and cannot qualify as unregistered. Unregistered evaluation can
therefore consume the validated embedded list directly.

Tracker configuration matching is order-sensitive, so it must continue to
consider those three pseudo URLs, in their current order, before matching real
embedded tracker URLs. This preserves unusual but currently valid
configurations that match a pseudo URL without manufacturing tracker-status
records for unregistered detection.

No public compatibility surface changes: CLI flags, JSON configuration,
installed commands, exit codes, deprecated 2.x source wrappers, and supported
Python versions remain unchanged.

## Safety and temporal behavior

- Dry-run continues to use the real orchestration path and performs no
  qBittorrent or filesystem mutation.
- Impact preview and execution reuse the same authoritative torrent objects and
  accepted execution-scoped tracker data.
- Cache publication is atomic: no batch can become permission to act until the
  entire snapshot has complete, validated tracker metadata.
- A missing or malformed exact response for an active torrent still fails
  closed.
- When an exact fallback fails, the existing fresh ordinary snapshot remains a
  safety barrier: it may prove disappearance, but malformed identities,
  duplicates, same-hash re-addition, or an active failed torrent still abort.
- Pre-mutation disappearance and deletion-tag checks remain fresh checks and
  are not cached away.
- Tracker metadata changing after preview does not silently substitute a new
  deletion plan; execution remains bound to the accepted plan and its existing
  preflight rules.

## Implementation boundaries

- `qbitunregistered/cli.py` owns ordinary initial acquisition and invokes
  operation-aware bounded tracker priming.
- `qbitunregistered/operations/seeding_management.py` owns validation and
  preloading for the shared tracker cache alongside
  `fetch_torrent_trackers`.
- `qbitunregistered/tracker_matcher.py` may expose a focused helper for the
  pseudo-first matching rule if keeping that rule inside
  `find_tracker_config` would duplicate it.
- Existing impact and operation modules continue to call
  `fetch_torrent_trackers` or `find_tracker_config`; they do not gain separate
  bulk-transport branches.

## Verification and acceptance

Implementation follows test-driven development through the required
PythonPro agent. Regression coverage must establish:

- tracker operations retain exactly one ordinary snapshot and request exactly
  `ceil(N / 100)` canonical tracker batches when supported;
- non-tracker operations retain exactly one ordinary snapshot;
- first-batch rejection or total omission uses exact reads without reacquiring
  the ordinary snapshot;
- later rejection, omission, partial coverage, reordering, or identity drift
  fails closed without publishing a partial cache;
- present malformed embedded data fails during preview with no exact fallback,
  execution, or mutation;
- exact failure, disappearance, re-addition, malformed refresh, deletion
  preflight, and dry-run contracts remain unchanged;
- tracker matching produces the same result for pseudo and real URLs;
- cache entries remain isolated by client and execution.

The full project test, Black, fatal Flake8, BasedPyright, and mypy checks must
pass. The PythonPro agent must also exercise the actual BasedPyright language
server for navigation and diagnostics.

The merged paired gauntlet is the performance and safety acceptance gate:

- run both `tracker-quick` and `tracker-full` in the prescribed ABBA and BAAB
  ordering;
- require control transport `(ordinary=1, bulk=0, exact=N)` and candidate
  transport `(ordinary=1, bulk=ceil(N/100), exact=0)` for complete embedded
  metadata: `(1,13,0)` quick and `(1,130,0)` full;
- retain the gauntlet's compatibility and malformed-data scenario endpoint
  contracts;
- require candidate CPU ratio no greater than `1.0` and peak-memory ratio no
  greater than `1.25`;
- require identical intended actions, zero mutation in dry-run, and all
  isolation checks to pass.

Documentation updates cover the optimized request flow in `README.md`,
`ARCHITECTURE.md`, and `CHANGELOG.md`. A protected live qBittorrent dry-run is
outside this implementation and remains a separate explicit approval gate.

## Non-goals

- Upgrading or reconfiguring qBittorrent.
- Accessing the operator's live qBittorrent instance or media filesystem.
- Bulk file-metadata changes for cross-seed or ownership workflows.
- Persisting qBittorrent metadata between executions.
- Changing deletion criteria, confirmation behavior, or release packaging.
