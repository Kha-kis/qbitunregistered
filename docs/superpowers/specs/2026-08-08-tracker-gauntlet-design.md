# Tracker Metadata Gauntlet Design

## Objective

Establish a deterministic, evaluator-only benchmark for qbitunregistered's
unregistered-torrent preview and dry-run execution path. The evaluator must make
the current per-torrent tracker bottleneck measurable before production code is
optimized, without contacting a live qBittorrent instance or changing production
behavior.

## Scope and separation

Add independent `tracker-quick` and `tracker-full` profiles to the existing
repository gauntlet. Do not combine tracker timing with the orphan profiles:
filesystem traversal would conceal the API-processing improvement and make the
result dependent on an unrelated workload.

This Establish-mode branch may change only:

- `benchmarks/gauntlet/` evaluator code and its quality bar;
- evaluator tests;
- evaluator, architecture, and changelog documentation.

It must not change `qbitunregistered/`, dependency metadata, CLI behavior, or
the 2.x compatibility wrappers. The private builder/critic orchestration remains
outside the repository.

## Locked workloads

| Metric | `tracker-quick` | `tracker-full` |
|---|---:|---:|
| Torrents | 1,300 | 13,000 |
| Tracker records | 3,900 | 39,000 |
| Save-path groups | 1,200 | 12,000 |
| Default-tag targets | 200 | 2,000 |
| Cross-seed-tag targets | 100 | 1,000 |
| Existing torrent-only delete targets | 13 | 130 |
| Exact/prefix-message targets | 150 / 150 | 1,500 / 1,500 |

Every torrent has three complete, sanitized tracker mappings with reserved
`.invalid` URLs. Records cover healthy status, exact-message status 4,
prefix-message status 5, and harmless inactive values. The payload mirrors the
Web API 2.15.1 fields used by a real `includeTrackers=true` response, including
endpoint data, so memory evidence reflects a realistic bulk response shape.
The exact `/torrents/trackers` fake additionally prepends qBittorrent's three
fixed DHT, PeX, and LSD pseudo records; the embedded response contains only the
three real records, matching qBittorrent 5.2.3 semantics.
File deletion is disabled; deletion candidates are torrent-only operations.

Fixture construction, manifest verification, and semantic safety scenarios are
outside measured runtime. Measurement remains one untraced warm-up, five
untraced timed samples, and one separately traced untimed memory pass, with the
application cache cleared before every pass.

## Production boundaries exercised

Each measured pass calls only public/stable production boundaries:

1. `analyze_impact(client, torrents, config, ["unregistered"])`;
2. the resulting immutable `summary.unregistered_deletion_plan`;
3. `unregistered_checks(..., dry_run=True, deletion_plan=plan)`.

The evaluator must not reproduce tracker classification, cache keys, or bulk
helper internals. Its blueprint independently derives the expected tag and
torrent-only deletion actions from fixture roles.

## Evidence and acceptance

Each pass emits and validates:

- fixture-manifest digest;
- exact preview action digest over action, tag, and torrent hash;
- dry-run reconciliation digest over returned per-path counts and sanitized
  operator-visible action counts;
- candidate counts for default tags, cross-seed tags, and torrent-only deletes;
- explicit zero values for every qBittorrent mutation counter and filesystem
  mutation;
- ordinary `torrents.info`, `torrents.info.include_trackers`, and
  `torrents_trackers` request counts.

The supported-response profile accepts the current control transport
`(include_trackers=0, exact=N)` and a future bulk transport
`(include_trackers=1, exact=0)`. It rejects more than one bulk request,
more than `N` exact requests, and redundant bulk plus `N` exact requests. The
paired optimization target is one combined tracker-metadata request. Runtime
must be at most 50% of the paired control median, peak memory at most 125%,
relative MAD at most 0.15, and relative range at most 0.50. Thresholds are not
relaxed to admit a candidate.

The exact execution-time tag hashes are not present in current dry-run log
messages. The evaluator therefore locks exact preview hashes and independently
locks execution per-path results plus tag-action counts. Production cache reuse
binds both phases to the same tracker snapshot. This observability limitation is
reported explicitly rather than hidden.

## Compatibility and failure semantics

Untimed deterministic scenarios cover:

- complete embedded tracker metadata;
- a legacy response omitting the `trackers` key, followed by exact fallback;
- rejection of `include_trackers` with a compatible exact fallback;
- a present malformed embedded tracker field, which must fail closed when an
  implementation consumes the bulk response; a control that never requests
  bulk metadata remains valid on its exact path;
- a missing or malformed exact response for an active torrent, which must fail
  closed;
- a failed exact read followed by a fresh snapshot that proves disappearance;
- same-hash removal and re-addition after preview;
- duplicate, missing, empty, and non-string hashes in refresh snapshots;
- disappearance or delete-tag change before mutating preflight, with zero fake
  mutation attempts;
- tracker registration metadata changing after preview while dry-run remains
  bound to the accepted execution-scoped snapshot.

Scenario artifacts normalize transport-specific safe outcomes to the same
strict `pass` evidence. For example, the current control safely ignores an
unrequested malformed optional field, while a bulk candidate must reject that
field; both are safe but their endpoint counts differ. The evaluator validates
the transport-specific evidence before normalization.

The primary evaluator and CLI acceptance scenario are always genuine dry-runs.
The two mutating-preflight tests may call the operation with `dry_run=False`
only against the in-memory fake and must raise before any mutation endpoint.

qBittorrent's standalone tracker endpoint prepends DHT, PeX, and LSD pseudo
records that are absent from `includeTrackers`. Their statuses are only 0 or 2,
so they can never satisfy unregistered detection, which requires status 4 or 5.
The later production optimization can therefore use validated embedded real
trackers for unregistered checks without synthesizing pseudo records. Tracker
URL tagging must preserve the pseudo-URL matching order separately.

## Other bulk opportunities

The later tracker optimization should preload the shared execution-scoped
tracker cache so unregistered checks, tracker tagging, seeding analysis, and
execution can reuse one validated snapshot. Tracker URL matching must virtually
consider `** [DHT] **`, `** [PeX] **`, and `** [LSD] **` before real trackers to
preserve existing first-match behavior.

Bulk `include_files` opportunities in cross-seed tagging and unregistered file
ownership are deliberately deferred. They have different payload and
destructive-safety requirements. Orphan ownership already uses validated bulk
file metadata. Repeated pre-mutation torrent snapshots remain temporal safety
barriers and must not be cached away.

## Review and release gates

The evaluator branch requires PythonPro implementation with test-first red/green
evidence, BasedPyright CLI and actual LSP diagnostics, full project checks, and
fresh safety and performance critics. It must be merged before the production
optimization branch is created. The production branch then runs paired
`tracker-quick` and `tracker-full` comparisons against the merged evaluator.
Accessing the live qBittorrent instance for the protected dry-run remains a
separate human approval gate.
