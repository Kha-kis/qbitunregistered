# Gauntlet evaluator

The gauntlet measures real production preview and dry-run boundaries against
deterministic qBittorrent and filesystem fixtures. It never contacts a live
qBittorrent instance.

## Profiles

| Profile | Pipeline | Locked workload | Intended use |
|---|---|---:|---|
| `quick` | Orphan discovery and reconciliation | 1,200 torrents / 9,400 files | Fast development feedback |
| `full` | Orphan discovery and reconciliation | 12,000 torrents / 94,000 files | Candidate evaluation |
| `tracker-quick` | Unregistered preview and dry-run | 1,300 torrents / 3,900 trackers | Fast tracker feedback |
| `tracker-full` | Unregistered preview and dry-run | 13,000 torrents / 39,000 trackers | Tracker candidate evaluation |

The tracker fixtures also lock 1,200/12,000 save-path groups, 200/2,000
default-tag targets, 100/1,000 cross-seed-tag targets, and 13/130
torrent-only deletion targets. Exact/prefix-message targets are 150/150 for
`tracker-quick` and 1,500/1,500 for `tracker-full`. File deletion is disabled.

## Tracker metadata evaluation

Run either tracker profile without a comparison and keep the raw JSON outside
the repository:

```bash
uv run python -m benchmarks.gauntlet \
  --profile tracker-quick \
  --output /tmp/qbitunregistered-tracker-quick.json

uv run python -m benchmarks.gauntlet \
  --profile tracker-full \
  --output /tmp/qbitunregistered-tracker-full.json
```

The quick comparison entry point is:

```bash
uv run python -m benchmarks.gauntlet \
  --profile tracker-quick \
  --compare \
  --output /tmp/qbitunregistered-tracker-quick.json
```

The checked-in standalone baselines intentionally remain
`pending_clean_evaluator_commit`: contemporaneous paired comparison is the
canonical optimization decision. Until a measured baseline is reviewed, the
command still validates deterministic and safety evidence but reports an
overall pending comparison and returns a nonzero comparison status.

Run the canonical full tracker comparison from the evaluator checkout, with
two distinct clean worktrees at the control and candidate revisions:

```bash
uv run python -I -S -B benchmarks/gauntlet/launcher.py \
  --profile tracker-full \
  --paired-control /path/to/control \
  --paired-candidate /path/to/candidate \
  --output /tmp/qbitunregistered-tracker-paired-full.json
```

The current control transport performs one ordinary torrent-list read, no bulk
`includeTrackers` read, and exactly one `/torrents/trackers` read for each of
`N` torrents. A supported optimization replaces that ordinary response with
one `includeTrackers` response and performs no exact tracker reads. The only
accepted triples, in ordinary/bulk/exact order, are therefore `(1, 0, N)` for
control and `(0, 1, 0)` for candidate. Every warm-up, timed, and memory pass
must have its role's exact triple; aggregate-only, partial, mixed, redundant,
or synthesized evidence fails. Synthetic runtime is a
CPU/regression guard capped at the paired control runtime, and peak memory is
capped at 125% of control; the existing variance limits also apply. The
evaluator does not add artificial latency or a local network service. Real
wall-clock improvement requires separately approved protected live dry-run
evidence.

### Evidence semantics

- `tier` locks whether a profile is a round or candidate workload.
- `fixture_manifest_digest` hashes the exact effective torrent-info mapping
  returned after mutable snapshot overlays and host-path normalization, while
  `intended_action_digest` locks the exact preview action, tag, and torrent-hash
  tuples.
- `execution_action_digest` locks the normalized per-hash arguments observed
  during one untimed mutating shadow execution against a fresh fake. It must
  equal the independent preview oracle and is excluded from timing and memory.
- `workload` records the locked profile size, and `candidate_counts` records
  the independently expected default-tag, cross-seed-tag, and torrent-only
  deletion targets.
- `reconciliation.digest` locks primary dry-run per-path results and sanitized
  operator-visible action counts.
- `endpoint_counters`, per-pass endpoint counters, and
  `mutation_counters` prove the selected transport and require zero primary
  qBittorrent mutations. `isolation_counters` require zero filesystem-write,
  network-connect, network-DNS, and destination-bearing network-outbound
  (`sendto`/`sendmsg`) attempts for measured passes, the shadow, and every
  semantic scenario. This audit-event boundary cannot separately observe
  `send` or `sendall` on a socket connected before the boundary.
- Runtime statistics retain all five untraced samples. Peak memory comes from a
  separate traced, untimed pass. Each primary pass uses a fresh fixture. Its
  measured interval starts immediately before the fake materializes the
  production-selected initial torrent response and ends immediately after the
  real `unregistered_checks()` call returns. Fixture construction, sanitized
  CLI-config creation, manifest verification, and semantic safety scenarios
  remain outside that interval.
- The twelve normalized scenario results traverse the real CLI and lock
  compatibility and fail-closed behavior with endpoint, exit-code, terminal
  phase, observation-order, mutation, and isolation evidence. Scenario hooks
  inject churn only after initial acquisition or after preview. Legacy omission
  or rejection of embedded trackers may use the exact fallback; malformed
  metadata, uncertain refreshes, hash re-addition, or preflight churn must not
  authorize mutation.

See the [tracker gauntlet design](../../docs/superpowers/specs/2026-08-08-tracker-gauntlet-design.md)
for fixture and compatibility details. The longer trust-boundary and
publication guarantees are documented once in
[Evaluator Isolation](../../ARCHITECTURE.md#evaluator-isolation) and below.

### Branch and live-test gates

The public evaluator branch contains evaluator code, locked inputs, tests, and
documentation only. Private builder/critic orchestration is not part of the
repository. Merge and independently review the evaluator before creating the
production optimization branch; optimization commits must not change evaluator
sources, fixtures, deterministic digests, quality thresholds, or dependency
inputs.

Synthetic evaluation is not live acceptance. A protected installed-wheel
dry-run against a real qBittorrent instance is a separate soak that requires
explicit human approval after synthetic and review gates pass. It must never be
inferred from permission to run the evaluator.

## Contemporaneous paired comparison

Busy hosts can move substantially between an accepted historical baseline and
a later candidate run. The paired mode keeps the existing target fractions but
replaces that stale machine comparison with two contemporaneous measurements
of each production revision:

```bash
uv run python -I -S -B benchmarks/gauntlet/launcher.py \
  --profile full \
  --paired-control /path/to/clean-control-worktree \
  --paired-candidate /path/to/clean-candidate-worktree \
  --output /tmp/qbit-gauntlet-paired-full.json
```

Both paths must be distinct, clean Git worktrees. The invoking checkout must
also be clean. All three checkouts must have byte-identical evaluator sources,
including the executable `benchmarks/__init__.py` parent package initializer,
canonical quality-bar bytes, `pyproject.toml`, and `uv.lock`. Paired mode rejects
a custom `--compare` path and any seed other than the selected profile's
canonical seed, and always loads
`benchmarks/gauntlet/quality-bar.toml` from the invoking checkout's regular,
visible stage-0 blob at its originally captured commit. Threshold parsing and
the recorded digest use that same immutable byte buffer, which is reverified
before comparison and artifact publication. The artifact
records the three clean identities plus evaluator, quality-bar, and dependency
digests, and the orchestrator rechecks all identities and digests after the
run. All evaluator, quality-bar, and dependency-lock inputs must be regular
stage-0 index entries without skip-worktree or assume-unchanged flags; the
coordinator verifies this before and after every child. Protected package trees
may not contain ignored Python sources, symbolic
links, Windows junctions, or other reparse points that can redirect imports.
Ignored-source detection honors repository, worktree, and configured global Git
excludes. The coordinator checks ignored sources, redirects, and native
extensions before and after each child crossover and rechecks all package-tree
protections after the run. Every isolated child checks redirecting entries again
immediately before imports and after evaluation. Its bootstrap binds protected
root and descendant imports to immutable blob bytes captured from regular,
canonically visible Git index entries. The loader retains worktree filenames
for package and traceback semantics, but it compiles only the captured index
bytes and rechecks staged modes, object IDs, and the complete protected source
set before every protected import and after evaluation. Before any protected
blob is read or imported, the complete canonical Python path, mode, and object
ID map must also exactly match the role's originally captured commit. Every
measured child receives that commit's verified bootstrap blob through isolated
Python standard input, and the coordinator repeats the same binding after the
child returns. Skip-worktree,
assume-unchanged, missing, ignored, native-only, namespace, redirected, or
case-ambiguous protected imports fail closed without falling through to
installed import finders.
Contemporaneous paired
execution requires the platform to expose
`O_NOFOLLOW` (or equivalent descriptor no-follow support); it fails closed when
that capability is unavailable. An explicit output destination must also
resolve to the same path outside all three worktrees and each checkout's
canonical Git administration and common directories before and after the
crossover. The coordinator obtains those metadata directories with inherited
Git repository-selection variables removed, rejects ambiguous or unusable Git
output, and requires the complete protected-directory set to remain unchanged.
Before creating its isolated bytecode cache, the source launcher independently
applies the same metadata protection to the selected cache parent.
Publication is bound to an identity-checked directory descriptor carrying that
same protected set;
staging, cleanup, and replacement use names relative to that descriptor so an
ancestor symlink retarget cannot redirect the artifact. Paired execution fails
closed before staging when the platform lacks the required descriptor-relative
filesystem operations or trustworthy kernel descriptor-path introspection. The
publisher atomically detaches an existing explicit output into a reserved,
descriptor-relative backup, revalidates that the detached leaf remains a
regular file or symbolic link, then installs the fsynced staging inode with a
descriptor-relative no-clobber hard link. Allocator-owned output names likewise
detach and verify their reserved inode before the same no-clobber install. The
backup remains until the final directory and published-leaf checks succeed.
Rollback atomically captures both explicit and allocator-owned public leaves
before removing an unaccepted staging inode, and restores the prior output only
into an absent name. A concurrent replacement,
its uniquely named recovery link, and any restored prior-output backup remain
preserved after a failed publication so a later replacement cannot erase the
last recovery link. The
parent directory for an explicit paired `--output` must already exist so it can
be safely bound. The output leaf must be missing, a regular file, or a symbolic
link; directories and special files are rejected before evaluation and
rejected again if raced into place before publication. Ordinary non-paired
output retains automatic parent creation.

Paired mode must start through the source-only launcher shown above. The
operator-selected launcher file is the entry trust root: callers must invoke it
from the intended clean checkout with the `-I -S -B` startup semantics;
additional interpreter flags are permitted.
The launcher verifies isolated, no-site, safe-path, and no-bytecode flags before
it continues. With automatic `site` initialization disabled, it locates an
active virtual environment from the lexical interpreter path, reads only its
bounded `include-system-site-packages` setting from a stable, regular
`pyvenv.cfg`, and constructs canonical existing package directories with
`sysconfig` and `site.getsitepackages()`. It never calls `site.main()` or
`site.addsitedir()`, so `.pth` files and `sitecustomize` cannot run at this trust
boundary. Before executing downstream repository code,
the launcher requires `import_bootstrap.py` to be the same regular, visible
stage-0 blob in both `HEAD` and the index, verifies the stable worktree bytes
against that immutable blob (allowing only Git's deterministic whole-file CRLF
checkout representation), and supplies the immutable blob bytes to isolated
Python over standard input. It also removes inherited Python injection variables
and Git repository-selection variables, and starts the coordinator with a fresh
temporary bytecode cache before any repository package can be imported. The
coordinator and every measured child then start from verified bootstrap blob
bytes with Python's site initialization disabled and unsafe path prepending
blocked. A
source-only finder binds the `benchmarks` and `qbitunregistered` package trees
to the selected worktree without adding its repository root to `sys.path`.
Interpreter-owned standard-library paths, including the standard-library zip
and native-module directory, remain first. The coordinator keeps ordinary
installed dependency directories behind those paths without executing editable
install hooks. Digest-bound measured children instead reject modules already
loaded from dependency origins and remove those directories from `sys.path`;
their imports are limited to the standard library and immutable protected
first-party sources. Thus checkout-level or temporarily replaced dependency
modules cannot enter measured execution.

The coordinator fixes one ordered set of installed dependency paths for the
complete crossover and fingerprints every relative path and regular file's
contents. Every child bootstrap recomputes that fingerprint immediately before
and after evaluator execution, and the coordinator rechecks it after each
child. Redirecting and special dependency entries fail closed. The artifact
dependency digest binds this environment fingerprint to the identical
`pyproject.toml` and `uv.lock` bytes for comparability; it records observed
contents but is not an installation-provenance claim. This launcher invocation
is the only supported paired entry path. The coordinator rejects direct paired
execution with `python -m benchmarks.gauntlet` unless the validated bootstrap
supplies its one-use in-process isolation state and the exact interpreter flags,
protected finder, and sanitized import paths still match. Caller-controlled
cache environment markers cannot satisfy that proof. Ordinary non-paired
execution remains available through the module command with installed
dependency imports unchanged.

The orchestrator uses two symmetric crossover blocks: `control, candidate,
candidate, control`, then `candidate, control, control, candidate`
(ABBA+BAAB). Role position sums are identical. Each child starts with
`-s -S -P`, user-site and ordinary site initialization disabled, and Python injection
environment variables removed. Each invocation also uses a fresh temporary
bytecode cache outside the evaluated worktree. Repository-local native
extensions that could shadow the `benchmarks` or `qbitunregistered` package
trees are rejected before and after the crossover. Each run retains its
warmup, five untraced timed samples, and traced memory pass. No sample is
rejected. The artifact therefore retains 40 raw runtime samples and eight
peak-memory values. Child standard output is discarded. Standard error is
drained without accumulating it on disk or in unbounded memory; a nonzero exit
reports only a bounded excerpt with paths, credential-like values, control
sequences, and URL user information redacted.

Runtime pools all 20 samples for each role and compares their medians with the
profile target. Tracker profiles use `1.0` as a CPU/regression ceiling because
endpoint collapse is their structural optimization gate; orphan profiles keep
their existing `0.50` target. Each four-run block must independently meet the
selected profile target, preventing a favorable later phase from hiding an
unfavorable one.
The relative range across each role's four run medians must stay within the
existing profile `relative_range_max`.

Memory compares the median of four candidate peaks with the median of four
control peaks, and each crossover block independently, against the unchanged
`1.25` target. Each role's four memory peaks must also meet the existing
`relative_range_max`; the evaluator deliberately reuses that locked limit
rather than introducing or relaxing a threshold. Adjacent control/candidate
runtime and memory ratios remain recorded as drift evidence.

Every child must independently pass the existing identity,
measurement-policy, safety, oracle, API-budget, and variance gates. The
evaluator fails closed on missing samples, dirty identities, environment
differences, schema drift, malformed evidence, unknown JSON fields, or nonzero
mutations. Child JSON is read once through a bounded no-follow regular-file
descriptor, strictly validated at every nested level, and reconstructed before
it is retained. Arbitrary child fields cannot flow into the paired artifact.

A self-comparison should use two isolated clean worktrees at revisions with
identical production code. It is a stability check: ratios should be near
`1.0`; it cannot satisfy the tracker transport gate because candidate passes
must use one bulk request while control passes must use exact requests.

Earlier tracker artifacts predate the effective-payload manifest and real-CLI
scenario phase evidence. They are non-comparable and must not be used as
control evidence; regenerate quick and full artifacts with schema version 8.

## qBittorrent file metadata fixture

The deterministic fake implements the qBittorrent 5.2 `include_files=True`
torrent-list response as mapping-shaped torrents with attribute access. Every
bulk and exact file-metadata read performs a JSON encode/decode round trip,
allocating fresh response containers and mapping data during the measured call.
Bulk responses include a `files` field for every torrent, while legacy exact
calls allocate one fresh sequential response per requested torrent.

Explicit fixture modes cover a legacy response without the `files` field, an
endpoint that rejects the option, and malformed embedded metadata. Ordinary
`info()` retains the original legacy object response and exact per-torrent
endpoint behavior. These tests establish evaluator compatibility; actual
production use of the bulk path is proven only after the optimization branch
rebases onto this evaluator and reports zero `torrents_files` calls.

The synthetic allocation model measures Python JSON decoding and retained
objects, not qBittorrent server serialization, socket latency, native-library
RSS, or live filesystem contention. The protected live soak remains the final
real-host acceptance gate.
