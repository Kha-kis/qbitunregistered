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

Both paths must be distinct, clean Git worktrees with byte-identical evaluator
sources and quality bars. The orchestrator runs them in `control, candidate,
candidate, control` (ABBA) order using isolated subprocess imports. It checks
the identity again after all four runs.

Every child run retains its warmup, five untraced timed samples, and traced
memory pass. No sample is rejected. The paired artifact contains all four
sanitized child artifacts (20 raw runtime samples and four peak-memory values),
both repository identities, the evaluator digest, unchanged target fractions,
and the comparison report. It contains no worktree or fixture paths.

The runtime comparison pools all ten retained samples for each role, then
compares the two role medians with the locked `0.50` target. This gives both
halves of the balanced ABBA sequence equal weight without selectively dropping
samples. The relative range between each role's two run medians must remain
within the existing profile limit. Peak memory compares the median of both
retained candidate peaks with the median of both retained control peaks and the
locked `1.25` target. Adjacent candidate/control ratios are retained as drift
evidence but are not treated as independent samples.

Every child must independently pass the existing identity,
measurement-policy, safety, oracle, API-budget, and variance gates. The
evaluator fails closed on missing samples, dirty identities, environment
differences, schema drift, malformed evidence, or nonzero mutations.

A self-comparison should use two isolated clean worktrees at revisions with
identical production code. It is a stability check: it should produce ratios
near `1.0` and therefore is not expected to pass the `0.50` optimization target.

## qBittorrent file metadata fixture

The deterministic fake implements the qBittorrent 5.2 `include_files=True`
torrent-list response as mapping-shaped torrents with attribute access. Each
mapping contains the exact fake file list, so a production bulk path performs
zero `torrents_files` calls. Explicit fixture modes cover a legacy response
without the `files` field, an endpoint that rejects the option, and malformed
embedded metadata. Ordinary `info()` retains the original legacy object
response and exact per-torrent endpoint behavior.
