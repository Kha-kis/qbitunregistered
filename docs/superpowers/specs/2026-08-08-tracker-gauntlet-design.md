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
- exact shadow-execution action digest from a fresh in-memory fake;
- dry-run reconciliation digest over returned per-path counts and sanitized
  operator-visible action counts;
- candidate counts for default tags, cross-seed tags, and torrent-only deletes;
- explicit zero values for every primary qBittorrent mutation counter and
  every filesystem-write, network-connect, network-DNS, and destination-bearing
  network-outbound attempt class;
- ordinary `torrents.info`, `torrents.info.include_trackers`, and
  `torrents_trackers` request counts.

The supported-response profile accepts the current control transport
`(include_trackers=0, exact=N)` and a future bulk transport
`(include_trackers=1, exact=0)`. It rejects more than one bulk request,
more than `N` exact requests, and redundant bulk plus `N` exact requests. The
paired optimization target is structurally one combined tracker-metadata
request in every candidate pass while every control pass performs exactly `N`
exact reads. Synthetic runtime is a regression guard with a maximum candidate
ratio of `1.0`; peak memory remains at most 125%, relative MAD at most 0.15,
and relative range at most 0.50. No artificial latency or network service is
introduced to manufacture a wall-clock improvement.

The primary path remains a genuine dry-run. Because its logs do not expose
exact execution-time tag hashes, one untimed shadow calls the real mutating
production boundary against a fresh fake, records normalized per-hash endpoint
arguments, and requires them to match the fixture-independent preview oracle.
The shadow cannot delete files and contributes no runtime or memory samples.

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
records that are absent from `includeTrackers`. Their statuses are literal `0`,
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

## Critic hardening round 2

The primary dry-run remains the measured production path, but aggregate log
counts are not sufficient execution identity evidence. After the measured
passes, the evaluator runs one untimed mutating shadow execution against a
fresh in-memory fake. The fake records normalized per-hash arguments supplied
to `torrents_add_tags` and `torrents_delete`; the evaluator compares the full
record set and its digest with the fixture-derived preview oracle. This shadow
is excluded from runtime and peak-memory measurements, uses
`delete_files=False`, and has no filesystem or network implementation.

All production calls made by measured passes, the shadow execution, and every
semantic scenario run inside one process-audit boundary. While active, the
boundary denies every filesystem write or mutation event regardless of path or
directory descriptor, plus socket connection and DNS-resolution events. It
records only sanitized attempt classes and counts. Successful artifacts lock
all such counters to zero; denied attempts fail the evaluator without retaining
paths, hostnames, addresses, or payloads.

Real tracker mappings mirror Web API 2.15.1 with `next_announce`,
`min_announce`, and deterministic nested endpoint objects in both exact and
embedded responses. Every response remains freshly decoded down to nested
objects. Torrent roles are ordered by a stable seed/index hash rather than by
role prefix, with an action-bearing record forced to the tail. This makes
truncated or default-empty bulk caches fail the existing complete action and
reconciliation oracles.

Tracker quality-bar profiles lock their exact tier, fixture digest, preview
action digest, shadow execution digest, isolation schema, and complete
transport alternatives. Endpoint collapse from `N` exact calls to one bulk
call is the structural optimization gate. Synthetic runtime is only a CPU and
runtime regression guard: before a candidate exists its maximum paired ratio
is `1.0`; peak memory remains at most `1.25`. No artificial latency or local
network service is part of the evaluator. A real wall-clock improvement can be
accepted only from separately approved paired protected live dry-run evidence.

## Critic hardening round 3

The malformed-embedded scenario has two transport-specific safe outcomes. An
exact-only control ignores the unrequested malformed mapping and must produce
the complete fixture-derived action records. A bulk candidate consumes the
malformed mapping and must fail closed before producing any action or mutation.
The evaluator validates those branch-specific facts and endpoint counts first,
then emits one `transport_safe` scenario digest for both. The paired sanitizer
therefore compares one transport-neutral safety fact while retaining each
child's transport counters. Exact success with wrong action hashes and bulk
success or mutation remain failures.

The fake response wrapper mirrors the installed `qbittorrent-api`
`TorrentDictionary` boundary without importing that dependency into isolated
paired children. Embedded trackers remain available only through mapping access
(`torrent["trackers"]` or `torrent.get("trackers")`). Attribute access through
`torrent.trackers` delegates to `torrents_trackers()` and increments the exact
endpoint counter. Wrapper construction exposes ordinary mapping fields as
attributes and converts the raw `reannounce` key to `reannounce_in`, matching
the installed model. Consequently, reading `.trackers` after one bulk response
produces the redundant `(bulk=1, exact=N)` shape and fails the transport gate.

Each raw torrent-list item contains the complete sanitized torrent-info field
set serialized by qBittorrent Web API 2.15.1, plus the optional `trackers`
field. Values are deterministic and realistic but contain only fixture paths,
reserved `.invalid` URLs, and synthetic hashes. Tests lock the exact wrapper
key set and require one supported embedded item serialized as compact JSON to
remain between 3,000 and 5,000 bytes. The fixture manifest incorporates a
path-normalized form of these fields so field/value drift changes the reviewed
oracle without retaining temporary host paths.

The guarded production boundary records a fourth sanitized isolation class,
`network_outbound_attempts`. CPython audit events `socket.sendto` and
`socket.sendmsg` are denied in that class before the underlying call proceeds;
connection establishment (`connect`/`connect_ex`) and DNS events retain their
existing classes. This is not a complete syscall-level network sandbox:
CPython does not publish separate audit events for `send` or `sendall` on a
socket connected before the guarded boundary. Documentation therefore claims
only the connection, DNS, and destination-bearing outbound attempts that the
audit hook actually observes and denies.

## Establish correction round 4: real CLI acquisition boundary

The measured tracker workload invokes the real `qbitunregistered.cli.main`
orchestrator because it is the only existing production boundary that owns the
choice of initial torrent-list request. The evaluator supplies a sanitized
temporary configuration and substitutes only `cli.create_client` with the
in-memory fake. It does not call a candidate helper, choose request arguments
from the control/candidate role, or materialize an ordinary response before
production runs. The selected production revision therefore determines whether
the authoritative snapshot is one ordinary response or one
`include_trackers=True` response.

Timing and allocation tracing arm immediately before the fake materializes the
first `torrents.info` response and stop immediately after the real
`cli.unregistered_checks` returns. Each warm-up, timed, and memory pass owns a
fresh fixture, cache, counters, observer state, configuration directory, and
authoritative response. The CLI-local snapshot remains alive through preview
and execution. The evaluator never retains or creates a second full response;
the control may add only exact tracker responses, while a future candidate may
reuse embedded trackers from its sole bulk response.

Two transparent evaluator observers retain exact evidence without adding a
production hook. One wraps the existing dynamically imported
`qbitunregistered.impact.analyze_impact`; the other wraps the existing
`qbitunregistered.cli.unregistered_checks` binding. They call the real
functions, record snapshot hash order and structured returns, and must each run
exactly once in preview-before-execution order. Execution must receive the
preview's exact deletion-plan object. Missing, duplicate, reordered, or
plan-substituting calls fail closed. The CLI must return success, dry-run must
attempt no mutation, and preview actions, shadow actions, returned
reconciliation, and operator counts must match their independent fixture
oracles. If production ceases to traverse either established observation
point, the evaluator rejects the pass rather than parsing presentation text.

The only accepted standalone endpoint triples are, in
`(ordinary info, include-trackers info, exact trackers)` order:

- control: `(1, 0, N)`;
- candidate: `(0, 1, 0)`.

Paired validation applies the first triple to every control warm-up, timed, and
memory pass and the second to every candidate pass. It rejects partial,
redundant, mixed, or synthesized evidence, including `(1, 1, 0)`, `(1, 0, 0)`,
and any exact count other than `N` on the control. The paired CPU ceiling
remains `1.0` and the peak-memory ceiling remains `1.25`; candidate evidence
cannot weaken either threshold.

The fixture manifest hashes the actual materialized raw mappings stored in
`FakeTrackerClient.torrent_info_by_hash`, after deterministic host-path
normalization. Its key set must exactly equal the authoritative snapshot hashes,
and every stored mapping's `hash` must equal its key. Every snapshot-controlled
torrent-info value overlays the stored mapping before fresh wrapper conversion:
hash, name, category, tags, save/content/download/root paths, magnet identity,
state, `added_on`, `completion_on`, `seeding_time`, `ratio`, `uploaded`, and
`downloaded`. Snapshot replacement affects the next response without mutating
the stored canonical payload.

This measurement boundary and manifest definition invalidate all round-3 raw
controls. Evaluator, result, quality-bar, and paired schemas advance together;
fresh round-4 quick and full controls must be produced from the clean committed
evaluator before another independent critic review. Shadow execution, semantic
scenarios, the global filesystem/network audit, sanitizer strictness, and the
separate protected-live approval gate remain unchanged.
