# Gauntlet evaluator

The gauntlet measures the real orphan-discovery, immutable-plan, and dry-run
reconciliation pipeline against deterministic qBittorrent and filesystem
fixtures. It never contacts a live qBittorrent instance.

## Contemporaneous paired comparison

Busy hosts can move substantially between an accepted historical baseline and
a later candidate run. The paired mode keeps the existing target fractions but
replaces that stale machine comparison with two contemporaneous measurements
of each production revision:

```bash
uv run python -m benchmarks.gauntlet \
  --profile full \
  --paired-control /path/to/clean-control-worktree \
  --paired-candidate /path/to/clean-candidate-worktree \
  --output /tmp/qbit-gauntlet-paired-full.json
```

Both paths must be distinct, clean Git worktrees. The invoking checkout must
also be clean. All three checkouts must have byte-identical evaluator sources,
including the executable `benchmarks/__init__.py` parent package initializer,
canonical quality-bar bytes, `pyproject.toml`, and `uv.lock`. Paired mode rejects
a custom `--compare` path and always loads
`benchmarks/gauntlet/quality-bar.toml` from the invoking checkout. The artifact
records the three clean identities plus evaluator, quality-bar, and dependency
digests, and the orchestrator rechecks all identities and digests after the
run. Contemporaneous paired execution requires the platform to expose
`O_NOFOLLOW` (or equivalent descriptor no-follow support); it fails closed when
that capability is unavailable. An explicit output destination must also
resolve to the same path outside all three repositories before and after the
crossover.

The orchestrator uses two symmetric crossover blocks: `control, candidate,
candidate, control`, then `candidate, control, control, candidate`
(ABBA+BAAB). Role position sums are identical. Each child starts with `-s`,
user-site loading disabled, and Python injection environment variables
removed. Each run retains its warmup, five untraced timed samples, and traced
memory pass. No sample is rejected. The artifact therefore retains 40 raw
runtime samples and eight peak-memory values.

Runtime pools all 20 samples for each role and compares their medians with the
locked `0.50` target. Each four-run block must independently meet that same
target, preventing a favorable later phase from hiding an unfavorable one.
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
identical production code. It is a stability check: it should produce ratios
near `1.0` and therefore is not expected to pass the `0.50` optimization target.

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
