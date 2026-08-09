# Bulk Tracker Metadata Design

## Context

Tracker-dependent runs currently acquire one ordinary torrent snapshot and then
call qBittorrent's exact tracker endpoint once for every torrent. The merged
tracker gauntlet establishes the safe replacement: qBittorrent Web API 2.15.1
can embed tracker metadata in the initial torrent snapshot through
`include_trackers=True`, reducing the normal request shape from one ordinary
snapshot plus `N` exact reads to one bulk snapshot.

This optimization is safety-sensitive. Tracker metadata participates in
unregistered detection, impact previews, tracker tagging, seeding limits, and
pre-mutation checks. The implementation must retain the existing fail-closed
behavior and keep preview and execution bound to the same accepted snapshot.

## Goals

- Use one bulk tracker snapshot for runs selecting `unregistered`,
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

The CLI determines selected operations before acquiring its authoritative
torrent snapshot. If any selected operation depends on trackers, it requests
`client.torrents.info(include_trackers=True)` once. Otherwise it retains the
existing ordinary `client.torrents.info()` request, avoiding a larger response
for unrelated operations.

After a successful bulk response, a focused tracker-metadata helper validates
and preloads the existing tracker cache. Cache entries use the same client
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

The operation-aware single snapshot is therefore the smallest design that
improves performance without weakening the current execution boundary.

## Acquisition and cache contract

The bulk response remains the sole authoritative initial torrent list. Before
preloading any entries, the helper validates every torrent hash as a non-empty
string and rejects duplicate hashes. This prevents malformed identity data from
being attached to the wrong cache entry.

For each torrent:

- If the mapping contains a `trackers` key with a non-string sequence value,
  the helper copies that sequence into the execution-scoped cache.
- If the key is absent, the helper leaves that torrent uncached. Its first
  consumer uses the existing exact endpoint, preserving compatibility with a
  server that accepted the query but omitted optional metadata.
- If the key is present but its value is `None`, a string, bytes, or any other
  non-sequence value, the helper stores a failure marker or equivalent rejected
  state. A consumer must fail closed without attempting an exact fallback.
  Successful-but-malformed bulk data is not evidence that the optional feature
  is unsupported.

If the bulk request itself raises, the CLI retries once with the ordinary
snapshot and leaves tracker entries uncached. Existing exact reads then provide
the compatibility path. If that ordinary acquisition also fails, the CLI keeps
the existing connection-error exit behavior. Control-flow exceptions remain
unswallowed.

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

- `qbitunregistered/cli.py` owns operation-aware initial acquisition and bulk
  request fallback.
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

- tracker operations request exactly one bulk snapshot when supported;
- non-tracker operations retain exactly one ordinary snapshot;
- a rejected bulk request retries ordinary acquisition and uses exact reads;
- an omitted per-torrent field falls back only for that torrent;
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
  transport `(ordinary=0, bulk=1, exact=0)` for complete embedded metadata;
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
