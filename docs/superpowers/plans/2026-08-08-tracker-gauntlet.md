# Tracker Metadata Gauntlet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a locked, evaluator-only tracker gauntlet that measures the real unregistered preview/dry-run pipeline at 1,300 and 13,000 torrents before production optimization.

**Architecture:** Add a tracker-specific fixture and runner beside the existing orphan evaluator, then dispatch profiles through the shared identity, publication, paired-comparison, and quality-bar infrastructure. The fixture derives its own action oracle and exposes both exact and embedded tracker transports without depending on future production helper names.

**Tech Stack:** Python 3.11+, pytest, stdlib JSON/hashlib/tracemalloc/statistics, existing qbitunregistered production modules, BasedPyright CLI/LSP, mypy, Black, Flake8.

## Global Constraints

- Establish mode: modify only `benchmarks/gauntlet/`, evaluator tests, evaluator documentation, `ARCHITECTURE.md`, and `CHANGELOG.md`; do not modify `qbitunregistered/`, dependency metadata, or compatibility wrappers.
- Use PythonPro for every Python implementation and review; read `AGENTS.md`, use BasedPyright for navigation/diagnostics, and exercise the actual BasedPyright language server.
- Follow strict TDD: each production-evaluator behavior starts with a test observed failing for the expected missing behavior.
- Keep all fixtures sanitized and deterministic; never contact a live qBittorrent client or network.
- Primary measured and CLI paths are dry-run and must record zero qBittorrent and filesystem mutations.
- Missing optional embedded `trackers` metadata may use exact fallback; a present malformed embedded value must fail closed when consumed, while a control that never requests bulk metadata remains valid on its exact path.
- Preserve existing orphan profiles, actions, digests, and performance semantics while bumping the evaluator schema/version for the new profile variant.
- `tracker-quick`: 1,300 torrents, 3,900 tracker records, 1,200 save-path groups, 200 default-tag targets, 100 cross-seed-tag targets, 13 torrent-only delete targets.
- `tracker-full`: 13,000 torrents, 39,000 tracker records, 12,000 save-path groups, 2,000 default-tag targets, 1,000 cross-seed-tag targets, 130 torrent-only delete targets.
- Supported-response API ceilings: at most one `torrents.info.include_trackers`, at most `N` `torrents_trackers`, ordinary `torrents.info` exactly zero, no redundant one-bulk-plus-`N` exact pattern; paired optimization target is one combined metadata request.
- Paired tracker acceptance structurally requires every control pass to use
  exactly `N` reads and every candidate pass to use one bulk read. Synthetic
  runtime is a regression guard at ≤ 100% of paired control; peak memory
  remains ≤ 125%, relative MAD ≤ 0.15, and relative range ≤ 0.50.
- Raw benchmark output stays outside the repository and contains no credentials, tracker URLs from real systems, torrent names, or host paths.

---

### Task 1: Deterministic tracker fixture, pipeline, schema, and safety gates

**Files:**
- Create: `benchmarks/gauntlet/tracker_fixture.py`
- Create: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/fixture_factory.py`
- Modify: `benchmarks/gauntlet/runner.py`
- Modify: `benchmarks/gauntlet/__main__.py`
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify only if strict variant validation requires it: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`

**Interfaces:**
- Produces `TRACKER_QUICK_PROFILE` and `TRACKER_FULL_PROFILE` registered under `tracker-quick` and `tracker-full`.
- Produces a tracker fixture whose fake client implements `torrents.info(include_trackers=True)` and `torrents_trackers(torrent_hash=...)`, returning fresh JSON-decoded values and separate endpoint counters.
- Produces `evaluate_tracker_fixture(...)` with the same timing-policy/result envelope expected by shared `run_gauntlet(...)` and paired evidence.
- Extends the quality-bar/result schema with an explicit profile kind and strict kind-specific workload, action, reconciliation, scenario, and API evidence.

- [ ] **Step 1: Write failing fixture-shape and transport tests**

Add literal assertions proving the quick/full counts above, three tracker records per torrent, reserved `.invalid` URLs, complete mapping fields, fresh response identities, explicit zero counters, and supported/omitted/rejected/malformed modes. Name the break each test catches; derive expected counts and digests independently from literals.

- [ ] **Step 2: Run the fixture tests and verify RED**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'tracker_profile or tracker_fixture or tracker_transport' -v
```

Expected: failures because tracker profiles, fixtures, or transport modes do not exist; no import/setup error is acceptable as the final red state.

- [ ] **Step 3: Implement the minimal deterministic tracker fixture**

Use frozen slot dataclasses and literals equivalent to:

```python
TRACKER_QUICK_PROFILE = TrackerGauntletProfile(
    name="tracker-quick",
    torrent_count=1_300,
    tracker_record_count=3_900,
    save_path_group_count=1_200,
    default_tag_count=200,
    cross_seed_tag_count=100,
    delete_count=13,
    tier="round",
)
TRACKER_FULL_PROFILE = TrackerGauntletProfile(
    name="tracker-full",
    torrent_count=13_000,
    tracker_record_count=39_000,
    save_path_group_count=12_000,
    default_tag_count=2_000,
    cross_seed_tag_count=1_000,
    delete_count=130,
    tier="candidate",
)
```

Generate three realistic Web API 2.15.1 tracker mappings per torrent using
only deterministic `.invalid` URLs. Count `torrents.info.include_trackers`,
ordinary `torrents.info`, and `torrents_trackers` separately. A missing key or
rejected parameter represents compatibility; a present malformed value remains
distinguishable and is never silently converted to an empty list. The exact
endpoint fake prepends literal DHT, PeX, and LSD pseudo records with qBittorrent
5.2.3 URL/status/message values; the embedded response contains only real
trackers.

- [ ] **Step 4: Run fixture tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Write failing pipeline-oracle and endpoint-budget tests**

Add tests that invoke the real `analyze_impact(..., ["unregistered"])` and
`unregistered_checks(..., dry_run=True, deletion_plan=...)`, then assert exact
literal candidate counts, stable 64-character fixture/action/reconciliation
digests, zero mutations, the current control endpoint shape `(0, N)`, and
rejection of counts outside these allowed supported-response pairs:

```python
allowed = {(0, profile.torrent_count), (1, 0)}
```

Also prove ordinary `torrents.info` is zero and redundant `(1, N)` evidence is
rejected.

- [ ] **Step 6: Run pipeline tests and verify RED**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'tracker_pipeline or tracker_oracle or tracker_api' -v
```

Expected: failures because tracker evaluation/result validation is absent.

- [ ] **Step 7: Implement the tracker runner and strict result variant**

Dispatch tracker profiles from `run_gauntlet` while retaining the common
repository identity, environment, timing policy, output publication, and paired
comparison paths. Hash sorted action records shaped as:

```python
{"action": "add_tag", "tag": tag, "torrent_hash": torrent_hash}
{"action": "delete_torrent_only", "tag": delete_tag, "torrent_hash": torrent_hash}
```

Hash execution reconciliation from sanitized per-path counts and operator-visible
action counts. Extend quality-bar and paired-evidence parsing with explicit,
closed profile variants; reject missing, extra, or malformed keys. Preserve the
existing orphan profile oracle byte-for-byte apart from the deliberate global
schema/version bump.

- [ ] **Step 8: Run pipeline tests and verify GREEN**

Run the Step 6 command. Expected: all selected tests pass and current orphan
gauntlet tests remain green.

- [ ] **Step 9: Write failing semantic-safety tests**

Add tiny-fixture tests for supported metadata; missing-key and rejected-parameter
fallback; present malformed embedded metadata fail-closed; malformed exact
metadata fail-closed; proven disappearance; same-hash re-addition; malformed or
duplicate refresh hashes; delete disappearance/tag change before mutating
preflight; and tracker state change after preview. Every failure scenario asserts
zero mutation counters and an unchanged temporary filesystem. The malformed
embedded case is transport-aware: an exact-only control passes without requesting
the malformed optional field; an implementation that requests it must fail
closed. Normalize only after validating the applicable endpoint evidence.

- [ ] **Step 10: Run semantic tests and verify RED**

Run:

```bash
uv run pytest tests/test_gauntlet_safety.py -k 'tracker or unregistered' -v
```

Expected: failures because the tracker semantic matrix/evidence is incomplete.

- [ ] **Step 11: Implement semantic evidence and actual CLI dry-run coverage**

Run the scenario matrix outside timed/memory sections and serialize only
sanitized outcome, action digest, and endpoint counts. Add an actual CLI test
using a temporary config, the fake client, `--unregistered --dry-run`, and exact
before/after filesystem snapshots. Preflight tests may pass `dry_run=False` only
when their arranged state guarantees a raised safety error before any fake
mutation endpoint.

- [ ] **Step 12: Run focused tests and changed-file checks**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py -v
uv run black --check benchmarks/gauntlet tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py
uv run flake8 benchmarks/gauntlet tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py
uv run basedpyright
uv run mypy benchmarks/gauntlet --ignore-missing-imports
```

Expected: all commands exit zero. Then exercise `uv run basedpyright-langserver
--stdio` as a real LSP client: initialize the worktree, open every changed Python
file, receive version-matching zero-error diagnostics, request definition or
references for a tracker evaluator symbol, shut down, and exit cleanly.

- [ ] **Step 13: Commit the evaluator implementation**

```bash
git add benchmarks/gauntlet tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py
git commit -m "test: establish tracker metadata gauntlet"
```

### Task 2: Lock documentation, provisional evidence, and full verification

**Files:**
- Modify: `benchmarks/gauntlet/README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`
- Modify: `benchmarks/gauntlet/quality-bar.toml` only if clean-commit evidence exposes an incorrect deterministic digest; do not change thresholds after observing candidate results.

**Interfaces:**
- Documents exact tracker commands, evidence fields, profile sizes, supported compatibility behavior, and the evaluator-only/optimization-branch separation.
- Produces raw quick/full tracker artifacts under `/tmp`, never inside the repository.

- [ ] **Step 1: Run clean-commit tracker profiles and capture raw evidence outside the repository**

After Task 1 is committed and the worktree is clean, run:

```bash
uv run python -m benchmarks.gauntlet --profile tracker-quick --output /tmp/qbitunregistered-tracker-gauntlet-quick.json
uv run python -m benchmarks.gauntlet --profile tracker-full --output /tmp/qbitunregistered-tracker-gauntlet-full.json
```

Expected: both exit zero, report the locked candidate/action/scenario digests,
control endpoint counts of `N` exact tracker reads, and zero mutations.

- [ ] **Step 2: Validate the locked schema against clean artifacts**

Use `jq` to compare each raw artifact's profile, workload, deterministic digests,
endpoint counters, mutation counters, and provisional comparison status with the
already-tested quality-bar values. Any mismatch is a Task 1 defect: return it to
the PythonPro builder with a focused failing regression test before changing the
quality bar. Human documentation prose does not receive a source-text test.

- [ ] **Step 3: Update the quality bar and documentation**

Retain the tested deterministic digests and schema-required provisional baseline
status. Document:

```bash
uv run python -m benchmarks.gauntlet --profile tracker-quick --compare --output /tmp/qbitunregistered-tracker-quick.json
uv run python -I -S -B benchmarks/gauntlet/launcher.py --profile tracker-full --paired-control /path/to/control --paired-candidate /path/to/candidate --output /tmp/qbitunregistered-tracker-paired-full.json
```

Explain that the evaluator never contacts a live client, production optimization
must not edit evaluator inputs, and the protected live soak requires separate
approval.

- [ ] **Step 4: Run the complete verification tier**

Run:

```bash
uv run black --check .
uv run flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
uv run pytest --cov=qbitunregistered --cov-report=term-missing --cov-fail-under=60
uv run pytest -m gauntlet_full -v
uv run basedpyright
uv run mypy qbitunregistered benchmarks --ignore-missing-imports
uv run --with pip-audit pip-audit
uv run --with bandit bandit -q -r qbitunregistered -ll
uv build
```

Expected: every command exits zero. Smoke-test the built wheel and both console
commands from a temporary directory outside the checkout. Repeat actual LSP
diagnostics for every changed Python file and retain only a sanitized summary.

- [ ] **Step 5: Commit documentation and locked evidence**

```bash
git add benchmarks/gauntlet/README.md benchmarks/gauntlet/quality-bar.toml CONTRIBUTING.md ARCHITECTURE.md CHANGELOG.md tests
git commit -m "docs: lock tracker gauntlet quality bar"
```

- [ ] **Step 6: Run independent gauntlet critics**

Give fresh-context read-only PythonPro safety and performance critics the actual
branch diff plus `/tmp/qbitunregistered-tracker-gauntlet-quick.json` and
`/tmp/qbitunregistered-tracker-gauntlet-full.json`. Require separate verdicts on
oracle independence/fail-closed behavior and measurement representativeness/API
budgets. Return actionable findings to the builder for at most three fix rounds,
with focused tests, LSP diagnostics, and scoped re-review after every round.

- [ ] **Step 7: Prepare the evaluator-only PR**

Verify `git diff main...HEAD` contains no production package, dependency, generated,
credential, or raw benchmark artifact changes. Push the branch and open a PR
describing Establish mode, exact profiles, safety evidence, endpoint baseline,
review results, and the requirement to merge this evaluator before beginning the
production optimization. Do not merge, tag, publish, or access the live instance.

### Task 3: Critic hardening round 2

**Files:**
- Modify: `benchmarks/gauntlet/tracker_fixture.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Modify: `tests/test_gauntlet_runner.py`
- Modify: `tests/test_gauntlet_safety.py`
- Modify: existing evaluator and project documentation named in Task 2

**Interfaces:**
- Produces exact untimed shadow-execution action records and `execution_action_digest` from a fresh fake fixture.
- Produces sanitized `isolation_counters` for global filesystem-write, network-connect, and network-DNS attempt classes.
- Extends scenario evidence with the same exact isolation-counter schema.
- Locks `tier` and `execution_action_digest` in each tracker quality-bar profile.

- [ ] **Step 1: Write safety regressions before evaluator edits**

Add tests that make a same-path cross-seed/healthy hash swap at the fake mutation
boundary, transiently write outside the fixture, write during a semantic
scenario, call descriptor-relative `os.open`, and attempt socket connection and
DNS resolution. Each test must exercise `evaluate_tracker_fixture` or
`evaluate_tracker_scenarios` and expect `GauntletSafetyError` with only a
sanitized attempt class.

- [ ] **Step 2: Run the safety regressions and verify RED**

Run the exact new pytest node IDs. Expected failures are acceptance of the
swapped hash, path-scoped/out-of-scenario filesystem writes, or unguarded
network calls; import and fixture-construction errors are not acceptable RED
states.

- [ ] **Step 3: Implement the shadow and production-boundary audit**

Replace the root-scoped observer with an active global audit that raises on
write/mutation, socket-connect, and DNS events. Scope it only to production
calls. Add a fresh-fixture mutating shadow after measured passes; normalize
batched fake mutation arguments into the existing per-hash action record shape,
then compare records and digest with `expected_tracker_action_records(...)`.

- [ ] **Step 4: Run the Step 2 nodes and verify GREEN**

The regressions must pass, the primary pass must still show genuine
`dry_run=True`, and the emitted primary mutation plus isolation counters must
all be zero.

- [ ] **Step 5: Write fixture and schema regressions before their edits**

Add literal tests for the complete Web API 2.15.1 mapping fields and fresh
nested identities, deterministic role interleaving with an action target at the
tail, failure from a truncated/default-empty tracker cache, `AttributeError` for
a missing fake attribute while the mapping key stays absent, exact quality-bar
tier, execution digest, isolation keys, and paired/standalone rejection of
missing, extra, or cross-kind evidence.

- [ ] **Step 6: Run the fixture/schema nodes and verify RED**

Expected failures are the three absent tracker fields, clustered response
roles, permissive fake attribute behavior, missing quality fields, and the
current `0.5` tracker runtime target.

- [ ] **Step 7: Implement the fixture, schema, and methodology changes**

Add deterministic `next_announce`, `min_announce`, and nested endpoint data;
stable hash ordering with an action record at the tail; tracker-only execution
and isolation evidence validation; exact tier matching; and tracker runtime
target `1.0` while retaining memory `1.25`. Keep exact-only and one-bulk
transports as the structural gate and do not introduce latency or networking.

- [ ] **Step 8: Update documentation and locked deterministic values**

Update this design, the gauntlet README, `CONTRIBUTING.md`, `ARCHITECTURE.md`,
and `CHANGELOG.md`. Recompute only fixture/scenario digests changed by the
specified deterministic payload/order/schema changes; do not derive a runtime
threshold from candidate measurements.

- [ ] **Step 9: Verify and commit one coherent evaluator-only change**

Run focused and full pytest, Black, repository fatal and changed-file Flake8,
BasedPyright CLI, mypy, a real BasedPyright LSP session over every changed
Python file, pip-audit, Bandit, build, installed-wheel smoke, and
`git diff --check`. Generate exact-only quick/full artifacts under `/tmp`, append
the ignored report and progress ledger with sanitized evidence, and commit only
evaluator/tests/docs/quality-bar files.
