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
- Produces sanitized `isolation_counters` for global filesystem-write,
  network-connect, network-DNS, and destination-bearing network-outbound
  attempt classes.
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

### Task 4: Critic hardening round 3

**Files:**
- Modify: `benchmarks/gauntlet/tracker_fixture.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/runner.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`
- Modify: evaluator and project documentation named in Task 3

**Interfaces:**
- Produces one canonical `transport_safe` digest for the malformed-embedded
  scenario after validating either exact-success actions or bulk fail-closed
  behavior.
- Produces mapping-only embedded tracker metadata and real-client-style
  `.trackers` exact endpoint delegation.
- Produces a complete sanitized torrent-info mapping and a fourth zero-locked
  `network_outbound_attempts` isolation counter.

- [x] **Step 1: Write paired scenario RED tests**

Add a regression that obtains exact-only scenario evidence, simulates a bulk
consumer that raises on malformed embedded metadata, and proves both sanitize
to the same literal transport-neutral digest before a paired exact-control /
one-bulk-candidate comparison passes. Add negative cases in which exact success
swaps a hash or bulk consumption succeeds and require `GauntletSafetyError`.

- [x] **Step 2: Run the scenario nodes and verify RED**

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'malformed_embedded_transport_neutral or malformed_embedded_wrong_actions or malformed_embedded_bulk_success' -q
```

Expected: the safe bulk candidate is rejected because its fail-closed digest
differs from the exact-success quality-bar digest; wrong exact actions are not
independently checked.

- [x] **Step 3: Implement branch validation and normalization**

Introduce one helper equivalent to:

```python
def _validated_scenario_action_digest(
    summary: ImpactSummary,
    profile: TrackerGauntletProfile,
    seed: int,
) -> str:
    if _action_records(summary) != list(expected_tracker_action_records(profile, seed)):
        raise GauntletSafetyError("tracker scenario actions did not match the fixture oracle")
    return expected_tracker_action_digest(profile, seed)
```

For `malformed_embedded_transport_aware`, validate `(0, N)` plus exact records
on success or `(1, 0)` plus fail-closed/no-mutation evidence on error, then
record `_scenario_digest(name, "transport_safe")` in both branches.

- [x] **Step 4: Run the scenario nodes and verify GREEN**

Run the Step 2 command and the existing paired transport tests. Every selected
test must pass; unchanged unsafe paths must still fail closed.

- [x] **Step 5: Write wrapper and payload RED tests**

Add literal assertions that one bulk response uses the complete qBittorrent
torrent-info wrapper key set, exposes ordinary mapping fields as attributes,
renames `reannounce` to `reannounce_in`, keeps embedded trackers under mapping
access, and delegates `.trackers` to the exact endpoint. After reading
`.trackers` for all `N` bulk items, require counters `(1, N)` and rejection by
`validate_tracker_endpoint_counts`. Require compact JSON for one supported item
to remain between 3,000 and 5,000 bytes.

- [x] **Step 6: Run the wrapper/payload nodes and verify RED**

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'torrent_info_payload or tracker_attribute_uses_exact_endpoint or redundant_bulk_attribute_transport' -q
```

Expected: seven-key payload and embedded-returning `.trackers` assertions fail.

- [x] **Step 7: Implement the dependency-free faithful wrapper and payload**

Build the raw torrent-info payload from deterministic synthetic values for the
official Web API 2.15.1 serializer keys. Convert each decoded item with:

```python
class FakeTrackerBulkTorrent(dict[str, object]):
    def __init__(self, payload: Mapping[str, object], client: FakeTrackerClient) -> None:
        converted = dict(payload)
        converted["reannounce_in"] = converted.pop("reannounce")
        super().__init__(converted)
        self._client = client

    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    @property
    def trackers(self) -> object:
        return self._client.torrents_trackers(torrent_hash=cast(str, self["hash"]))
```

Keep `trackers` in the mapping, bind wrappers to the fake client, and incorporate
a path-normalized payload into the manifest oracle.

- [x] **Step 8: Run the wrapper/payload nodes and verify GREEN**

Run the Step 6 command plus existing fixture freshness, truncation, and endpoint
budget tests. Recompute only manifest digests changed by the complete payload.

- [x] **Step 9: Write outbound audit RED tests**

Inject `sys.audit("socket.sendto", ...)` in a primary pass and
`sys.audit("socket.sendmsg", ...)` in scenario and shadow boundaries. Assert a
sanitized `network outbound` failure with no destination retained and no socket
call or packet. Extend strict evidence tests to require
`network_outbound_attempts = 0` and reject missing, extra, or nonzero values.

- [x] **Step 10: Run outbound audit nodes and verify RED**

```bash
uv run pytest tests/test_gauntlet_safety.py -k 'sendto or sendmsg or outbound' -q
```

Expected: all injected outbound events are accepted because the audit boundary
does not yet classify them.

- [x] **Step 11: Implement outbound isolation and schema locks**

Add `socket.sendto` and `socket.sendmsg` to a dedicated outbound event set and
increment `network_outbound_attempts`. Bump evaluator, quality-bar, and paired
schemas/versions; require the fourth exact zero counter at standalone, scenario,
and paired boundaries.

- [x] **Step 12: Run outbound audit nodes and verify GREEN**

Run Step 10 plus all existing safety tests. Failures must contain only the
sanitized attempt class and every normal artifact must emit zero.

- [x] **Step 13: Update documentation and locked values**

Correct all global-network claims, including root README and changelog, to say
connection, DNS, `sendto`, and `sendmsg` attempts. Explicitly disclose that
CPython audit hooks do not separately expose `send`/`sendall` on a pre-connected
socket. Update scenario/manifest digests and evidence schema without changing
runtime/memory thresholds.

- [x] **Step 14: Verify, commit, and regenerate clean artifacts**

Run focused/full pytest, `gauntlet_full`, Black, fatal and changed-file Flake8,
BasedPyright CLI, mypy, actual LSP over every changed Python file, pip-audit,
Bandit, build, installed-wheel smoke, and `git diff --check`. Commit one coherent
evaluator-only change, generate `round3` quick/full JSON under `/tmp` from the
clean commit, and append the ignored task report and progress ledger.

### Task 5: Establish correction round 4

**Files:**
- Modify: `benchmarks/gauntlet/tracker_fixture.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/runner.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`
- Modify: `benchmarks/gauntlet/README.md`
- Modify: this design and plan
- Append ignored evidence: `.superpowers/sdd/2026-08-08-tracker-gauntlet/`

**Interfaces:**
- `tracker_fixture_manifest_digest(fixture)` hashes validated stored
  `torrent_info_by_hash` mappings after path normalization.
- `FakeTrackerClient` exposes a fresh response from its current snapshot and
  arms an evaluator-supplied first-response measurement callback before
  materialization.
- The measured pass invokes `qbitunregistered.cli.main` with evaluator-only
  wrappers around `impact.analyze_impact` and `cli.unregistered_checks`.
- Endpoint evidence uses complete triples and paired validation assigns the
  exact control or candidate triple by role.

- [x] **Step 1: Add manifest and snapshot RED tests**

Add literal behavioral tests proving that a stored non-overlay payload mutation
changes the manifest; missing, extra, mismatched-key, and duplicate snapshot
hashes fail closed; and replacing the current snapshot changes hash/name,
category/tags, all four path fields, magnet identity, state, added/completion
times, seeding time, ratio, uploaded, and downloaded in the next fresh response
without changing the stored payload.

- [x] **Step 2: Run manifest tests and verify RED**

Run the new node IDs with `uv run pytest -q`. Expected failures are an unchanged
digest, accepted malformed payload ownership, and stale response values. Import,
fixture-construction, or assertion-setup errors are not valid RED evidence.

- [x] **Step 3: Implement manifest ownership and complete overlays**

Validate an exact one-to-one snapshot/payload hash mapping, normalize a deep
copy of each stored mapping for hashing, and overlay every snapshot-controlled
field before wrapper conversion. Preserve fresh nested response identities and
leave `torrent_info_by_hash` unchanged.

- [x] **Step 4: Run manifest tests and verify GREEN**

Run the Step 2 nodes plus existing payload-shape, wrapper, freshness, and
manifest determinism tests. All must pass before changing the runner.

- [x] **Step 5: Add real-CLI boundary RED tests**

Exercise a small real `cli.main` dry-run with a temporary sanitized JSON config
and the fake client. Require exactly one preview and execution observation in
that order, identical snapshot hash order, identity reuse of the preview
deletion plan, a success exit code, exact action/reconciliation evidence, zero
mutation, and measurement markers bracketing first-response materialization
through execution return. Add bypass, duplicate, reorder, snapshot-order, plan
substitution, nonzero-exit, and mutation failures.

- [x] **Step 6: Run CLI-boundary tests and verify RED**

Run only the new CLI-boundary nodes. Expected failures must show that the
current direct `analyze_impact`/`unregistered_checks` runner never invokes real
CLI acquisition or cannot prove the observation contract.

- [x] **Step 7: Implement real-CLI measured passes**

Give every warm-up, timed, and memory pass a fresh fixture and observer state.
Patch only `cli.create_client` plus transparent wrappers for the two approved
observation points, start the requested clock/tracer immediately before the
fake's first response is materialized, and stop immediately after real
execution returns. Keep the global production audit around `cli.main`, require
the exact observation contract and successful exit, and do not retain or
materialize concurrent ordinary and bulk responses.

- [x] **Step 8: Run CLI-boundary tests and verify GREEN**

Run the Step 6 nodes plus the primary tracker pipeline, dry-run safety, shadow,
semantic scenario, and global audit tests. Confirm every selected test passes
and ordinary control acquisition is counted inside every measured pass.

- [x] **Step 9: Add endpoint/schema/paired RED tests**

Require standalone triples `(1, 0, N)` or `(0, 1, 0)`. Require every paired
control pass to equal the former and every candidate pass to equal the latter.
Reject `(1, 1, 0)`, `(1, 0, 0)`, partial exact reads, redundant reads, missing
per-pass evidence, and forged aggregate-only evidence. Lock unchanged CPU
`1.0`, memory `1.25`, isolation, scenario, sanitizer, and shadow gates.

- [x] **Step 10: Run endpoint/schema/paired tests and verify RED**

Run the new endpoint and paired node IDs. Expected failures are rejection of
the newly valid ordinary-control triple or acceptance of at least one invalid
role/pass shape under the old two-field transport schema.

- [x] **Step 11: Implement complete endpoint evidence and schema bumps**

Validate the complete triple at standalone and paired boundaries, require role
specific shapes for warm-up, each timed sample, and memory passes, and advance
evaluator/result/quality-bar/paired versions together. Recompute deterministic
manifest locks only from the corrected fixture and leave performance thresholds
unchanged.

- [x] **Step 12: Run endpoint/schema/paired tests and verify GREEN**

Run Step 10 plus all gauntlet runner/safety tests. Confirm invalid triples and
forged/missing per-pass evidence still fail closed.

- [x] **Step 13: Update documentation and review the complete diff**

Update root and gauntlet READMEs, contribution workflow, architecture,
changelog, design, and plan with real CLI ownership, snapshot lifetime, exact
triples, invalid round-3 artifacts, and unchanged thresholds. Review the diff
for production/dependency/generated/raw-artifact changes, secrets, unsafe
paths, permissive schema behavior, and missing negative tests.

- [x] **Step 14: Verify, commit, and regenerate clean artifacts**

Run focused RED/GREEN evidence, full pytest with coverage, `gauntlet_full`,
Black, fatal and scoped Flake8, BasedPyright CLI, mypy, pip-audit, Bandit,
build, installed-wheel console-command smoke tests outside the checkout, an
actual BasedPyright LSP session over every changed Python file, and
`git diff --check`. Commit one coherent evaluator-only round, generate clean
`round4` quick/full JSON under `/tmp`, and append the ignored report and progress
ledger with exact sanitized evidence using `apply_patch`.

### Task 6: Establish correction round 5

**Files:**
- Modify: `benchmarks/gauntlet/tracker_fixture.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/runner.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`
- Modify: `benchmarks/gauntlet/README.md`
- Modify: this design and plan
- Append ignored evidence: `.superpowers/sdd/2026-08-08-tracker-gauntlet/`

**Interfaces:**
- `effective_torrent_info_payload(stored, torrent)` returns the authoritative
  post-overlay base mapping shared by manifest and response construction.
- Dependency-free response containers mirror installed 2026.8.0
  TorrentInfoList/TorrentDictionary and TrackersList/Tracker allocation.
- `_execute_scenario_cli(...)` runs real CLI acquisition with optional
  post-acquisition and post-preview hooks and returns structured observations.
- `TrackerScenarioEvidence` contains strict exit, phase, observation, action,
  endpoint, mutation, and isolation evidence.

- [ ] **Step 1: Add effective-manifest RED tests**

Add a literal parameter matrix that replaces one valid snapshot field at a
time and proves the manifest changes for hash/name/category/tags, save and
content paths, derived download/root paths and magnet identity, state,
added/completion/seeding times, ratio, uploaded, and downloaded. Add one stored
non-overlay mutation that must change the digest and one stored overwritten
field mutation that must not.

- [ ] **Step 2: Run effective-manifest tests and verify RED**

Run only the new manifest node IDs. Expected failures are unchanged digests for
snapshot overlays and a changed digest for a stored field that cannot reach the
effective response. Test setup, ownership validation, and path normalization
must remain valid.

- [ ] **Step 3: Implement the shared effective payload builder**

Move the complete overlay mapping into
`effective_torrent_info_payload(stored, torrent)`. Use its result in `.info()`
and in `tracker_fixture_manifest_digest`; add optional embedded trackers only
after the base result. Preserve stored mappings and existing ownership checks.

- [ ] **Step 4: Run effective-manifest tests and verify GREEN**

Run the Step 2 matrix plus existing ownership, response overlay, payload key,
manifest determinism, and fresh nested identity nodes. Recompute no locked
digest until all behavioral assertions pass.

- [ ] **Step 5: Add installed-wrapper graph RED tests**

Construct literal torrent and exact-tracker payloads with installed
`qbittorrent-api 2026.8.0` and the fake. Assert equivalent container,
top-level wrapper, recursive mapping, embedded sequence, exact Tracker, and
endpoint sequence shapes; repeated calls must share no mutable wrapper or
nested identity. Assert `.trackers` still performs exactly one exact call.

- [ ] **Step 6: Run wrapper tests and verify RED**

Run only the graph, identity, and endpoint nodes. Expected failures are the
fake's plain top response list, missing exact response/entry wrappers, or
different mapping normalization. The installed characterization must pass.

- [ ] **Step 7: Implement dependency-free response wrappers**

Add minimal AttrDict-normalizing mappings and UserList-shaped torrent/exact
containers. Match installed normalization through mappings but not through
sequences, retain reannounce renaming and `.trackers` delegation, and keep
every response freshly allocated.

- [ ] **Step 8: Run wrapper tests and verify GREEN**

Run Step 6 plus existing complete payload, wrapper behavior, truncation, and
control endpoint tests. Confirm the evaluator imports no new dependency and
stored payloads remain unchanged.

- [ ] **Step 9: Add real-CLI scenario and role-contract RED tests**

For all twelve scenarios, require one real initial CLI acquisition, exact
preview/execution order, correct hook phase, expected exit/terminal phase,
literal complete endpoint triple, action outcome, zero mutation, and zero
isolation. Add paired negative cases for a candidate that skips bulk, accepts
malformed bulk, uses exact fallback after malformed bulk, omits a refresh, or
forges aggregate-only scenario evidence.

- [ ] **Step 10: Run scenario/paired tests and verify RED**

Run only the new scenario and paired node IDs. Expected failures must show the
direct analyzer bypass, missing exit/phase observations, permissive per-role
scenario validation, and live evaluator use in the sanitizer unit test.

- [ ] **Step 11: Implement the CLI scenario harness and strict evidence**

Add `_execute_scenario_cli` using sanitized config, fake `create_client`, and
transparent preview/execution observers. Invoke bounded hooks only at the two
approved observer phases. Convert every compatibility, failure, refresh,
preflight, and snapshot-binding scenario to this harness and validate its
role-neutral local invariants before emitting evidence.

- [ ] **Step 12: Implement paired role contracts and fixed unit evidence**

Extend scenario sanitization with exact keys and bounded values, validate every
scenario by paired role without passing that role into child execution, and
replace the paired sanitizer test's `run_gauntlet` call with five literal
samples plus literal valid memory and static scenario evidence.

- [ ] **Step 13: Run scenario/paired tests and verify GREEN**

Run Step 10 plus all tracker primary, scenario, paired, sanitizer, and safety
tests. Confirm malformed candidate bulk fails before execution with
`(0, 1, 0)`, only omitted/rejected compatibility paths accept mixed transport,
and all hook-based churn remains fail closed.

- [ ] **Step 14: Advance schemas, locks, and documentation**

Advance result/evaluator/quality/paired versions together, recompute quick/full
effective manifest and scenario locks, preserve action/reconciliation digests
and CPU `1.0`/memory `1.25`, and update root/gauntlet docs, architecture,
contribution workflow, changelog, design, and plan. Mark round-4 artifacts
invalid for candidate decisions while recording their actual `(1, 0, N)`
control evidence and the critic reporting typo.

- [ ] **Step 15: Verify, commit, and regenerate clean artifacts**

Run grouped RED/GREEN evidence, all gauntlet runner/safety tests, full pytest
with coverage, `gauntlet_full`, Black, fatal and scoped Flake8, BasedPyright
CLI, mypy, pip-audit, Bandit, build, installed-wheel console smoke outside the
checkout, actual BasedPyright LSP diagnostics/navigation for every changed
Python file, and `git diff --check`. Commit one evaluator-only change, generate
clean `round5` quick/full JSON under `/tmp`, inspect actual scenario/primary
triples, and append the ignored task report and ledger using `apply_patch`.

### Task 7: Establish correction round 6

**Files:**
- Modify: `benchmarks/gauntlet/baseline.py`
- Modify: `benchmarks/gauntlet/paired_evidence.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Modify: `benchmarks/gauntlet/runner.py`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`
- Modify: root/evaluator/contribution/architecture/changelog/design/plan docs
- Append ignored evidence: `.superpowers/sdd/2026-08-08-tracker-gauntlet/`

**Interfaces:**
- `TrackerScenarioContract` is the frozen typed endpoint/exit/phase/order
  member parsed from one quality-bar role table.
- `TrackerScenarioRoleContracts` holds the control and candidate members.
- `tracker_scenario_matches_any_contract(...)` validates standalone/local
  evidence against the exact scenario union.
- `tracker_scenario_matches_role_contract(...)` validates paired evidence
  against the orchestrator-selected role.

- [x] **Step 1: Add strict standalone and parser RED tests**

Add a direct regression for fail-closed-to-success rewriting; parameterize
wrong endpoint, exit, terminal phase, and order mutations; exercise valid-field
cross inconsistencies and cross-scenario swaps; accept all twelve literal
control/candidate contracts. Add malformed TOML cases for missing/extra
scenario, role, and field, malformed triples, invalid exit/phase/order, and
inconsistent phase tuples.

- [x] **Step 2: Run the new nodes and verify RED**

The current standalone result gate must accept the adversarial success rewrite,
and the quality bar must lack the proposed frozen contract API. Parser cases
must fail because the new table is unrecognized or absent, not because of test
setup errors.

- [x] **Step 3: Implement the frozen quality-bar contract parser and union API**

Add the root TOML table, frozen dataclasses, strict load-time parser, and shared
union/role matchers. Make standalone result validation and paired child
sanitization require the exact per-scenario union. Preserve action digests,
primary transports, mutations, isolation, manifests, and thresholds.

- [x] **Step 4: Remove duplicate consumers and verify GREEN**

Delete the contract tables from `paired.py` and `tracker_runner.py`. Use the
shared role API only in paired orchestration and the shared union API in local
scenario evaluation. Run every new parser/standalone/paired test and all
tracker runner/safety tests.

- [x] **Step 5: Advance identity and narrow allocation wording**

Advance quality/result/evaluator/pairing versions to 7/9/1.9.0/2.6.0 while
keeping paired schema 6. Update all tracked operator, architecture,
contribution, changelog, design, and plan wording. Describe wrappers as a
source-faithful conservative visible model, not a complete allocation graph.

- [ ] **Step 6: Verify, commit, and generate round-6 controls**

Run focused RED/GREEN, all gauntlet runner/safety tests, full pytest with
coverage, explicit `gauntlet_full` with JUnit, Black, fatal/scoped Flake8,
BasedPyright CLI and actual LSP, mypy, pip-audit, Bandit, build, installed-wheel
smoke outside the checkout, and `git diff --check`. Commit one evaluator-only
change, generate clean round-6 tracker quick/full JSON under `/tmp`, validate
all scenario/primary contracts and zero counters, hash both artifacts, and
append the ignored task report and progress ledger.
