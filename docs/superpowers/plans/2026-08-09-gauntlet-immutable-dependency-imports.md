# Gauntlet Immutable Dependency Imports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow digest-bound paired tracker children to execute the real production import closure while preserving swap-import-restore protection and keeping installed dependency roots off `sys.path`.

**Architecture:** The coordinator publishes a bounded, hash-only manifest for the installed `tqdm` Python sources. Each child verifies and captures those exact sources into memory, loads real `tqdm` through a dedicated immutable finder, and installs a fail-closed evaluator-owned `qbittorrentapi` shim for the already mocked client boundary. Optional Apprise remains unavailable, and the existing complete dependency digest is revalidated after every child.

**Tech Stack:** Python 3.11+, `importlib`, SHA-256, JSON, pytest, Black, Flake8, BasedPyright CLI/LSP, mypy, Bandit, pip-audit, uv.

## Global Constraints

- Change only evaluator code, evaluator tests, and evaluator/architecture/release documentation on the dedicated evaluator branch.
- Do not change `qbitunregistered/`, `pyproject.toml`, `uv.lock`, configuration, console commands, or 2.x wrappers.
- Keep every installed dependency directory absent from digest-bound child `sys.path` throughout protected production imports and execution.
- Load only manifest-matching regular `.py` sources from the single canonical `tqdm` package; never reopen them after capture.
- Reject redirects, duplicate roots/modules, case-fold collisions, bytecode-only modules, native extensions, unsupported resource requests, and unexpected imports fail closed.
- Expose only `qbittorrentapi.Client` and `qbittorrentapi.exceptions.APIConnectionError`; both must fail closed if instantiated and must remain unused by a successful tracker run.
- Ordinary non-paired execution retains its existing installed-dependency behavior.
- Assume verified evaluator/bootstrap bytes, conforming CPython, and control,
  candidate, and dependency code that does not deliberately inspect or mutate
  evaluator-private Python state.
- Exclude deliberate mutation of evaluator globals, frames, `sys.meta_path`,
  evaluator-owned `sys.modules` entries, loader/finder internals, and audit
  registries. Protecting those objects from arbitrary same-interpreter code
  requires a separate native or process-isolation design.
- Treat final finder/loader/spec/origin/package checks as current drift
  detection, not cryptographic attestation of historical execution.
- Tests use synthetic worktrees, mocked clients, and temporary filesystems. Do not contact qBittorrent, the network, or the media library.
- Every Python change is implemented or reviewed by PythonPro after reading `AGENTS.md`, with `uv run basedpyright` and an actual `basedpyright-langserver --stdio` session.
- Use test-first red/green cycles, commit each independently reviewed task, and run full security/quality gates before publication.

---

### Task 1: Build and validate the immutable `tqdm` source manifest

**Files:**
- Modify: `benchmarks/gauntlet/import_bootstrap.py`
- Test: `tests/test_gauntlet_runner.py`

**Interfaces:**
- Produces: `IMMUTABLE_TQDM_MANIFEST_ARGUMENT = "--immutable-tqdm-manifest"`.
- Produces: `immutable_tqdm_manifest(dependency_paths: Sequence[str]) -> str`, returning canonical bounded JSON with `schema_version`, `namespace`, and ordered source records.
- Produces: frozen `_ImmutableDependencySource` records containing `fullname: str`, `root_index: int`, `relative_path: PurePosixPath`, `is_package: bool`, `size: int`, `sha256: str`, and later-populated `source_bytes: bytes`.
- Produces: `_capture_immutable_tqdm_sources(dependency_paths: Sequence[str], raw_manifest: str) -> tuple[_ImmutableDependencySource, ...]`.

- [ ] **Step 1: Add focused failing manifest tests**

Add tests that build a synthetic `site-packages/tqdm` tree and assert the canonical manifest contains `tqdm` and nested package/module records in module-name order, contains hashes but no absolute paths or source bytes, ignores ordinary non-importable regular data while the complete dependency digest still tracks it, and rejects:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_root",
        "symlink_source",
        "redirected_package",
        "casefold_collision",
        "bytecode_only",
        "native_extension",
        "oversized_source",
        "too_many_sources",
    ],
)
def test_immutable_tqdm_manifest_rejects_unsafe_source_trees(
    tmp_path: Path,
    mutation: str,
) -> None:
    dependency_paths = _build_unsafe_tqdm_tree(tmp_path, mutation)

    with pytest.raises(DependencyEnvironmentError):
        immutable_tqdm_manifest(dependency_paths)
```

Add `_build_unsafe_tqdm_tree(tmp_path: Path, mutation: str) -> tuple[str, ...]`
beside the existing dependency-tree test helpers. It must create exactly the
selected invalid shape rather than mocking the manifest implementation.

Also assert `_capture_immutable_tqdm_sources()` rejects malformed JSON, unknown keys, wrong types/schema/namespace, duplicate records, path traversal, reordered/noncanonical records, size/hash drift, and a manifest that does not exactly cover the installed importable `tqdm` source tree.

- [ ] **Step 2: Run the focused tests and record RED evidence**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'immutable_tqdm_manifest or capture_immutable_tqdm' -vv
```

Expected: failure because the manifest constant, builder, and capture validator do not exist.

- [ ] **Step 3: Implement the bounded manifest builder**

In `import_bootstrap.py`, reuse the existing stable regular-file descriptor validation rather than adding a weaker file read. Add explicit limits for manifest bytes, source count, individual source bytes, and total captured bytes. Map only:

```python
tqdm/__init__.py       -> ("tqdm", is_package=True)
tqdm/<name>.py         -> ("tqdm.<name>", is_package=False)
tqdm/<pkg>/__init__.py -> ("tqdm.<pkg>", is_package=True)
```

Reject module-name collisions after Unicode case-folding. Include `root_index` and a POSIX relative path, never an absolute path. Serialize with sorted keys and compact separators so the exact manifest string is deterministic.

- [ ] **Step 4: Implement one-time verified source capture**

Parse the manifest with exact-key/type checks, re-enumerate the canonical source tree, and require exact record equality. For each record, open without following links where supported, validate the stable descriptor identity, read exactly the bounded size, and require SHA-256 equality before retaining bytes. Return immutable records and do not retain dependency root paths in them.

- [ ] **Step 5: Run focused quality and type checks**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'immutable_tqdm_manifest or capture_immutable_tqdm' -vv
uv run black --check benchmarks/gauntlet/import_bootstrap.py tests/test_gauntlet_runner.py
uv run flake8 benchmarks/gauntlet/import_bootstrap.py tests/test_gauntlet_runner.py --count --select=E9,F63,F7,F82 --show-source --statistics
uv run basedpyright
uv run mypy benchmarks/gauntlet/import_bootstrap.py --ignore-missing-imports
```

Expected: focused tests pass; Black/fatal Flake8/BasedPyright/mypy report no new errors.

- [ ] **Step 6: Exercise BasedPyright LSP and commit**

Start `uv run basedpyright-langserver --stdio` through an LSP client, open both changed Python files, request document symbols plus a definition/reference round trip for `immutable_tqdm_manifest`, collect version-matched diagnostics, and shut down cleanly. Record zero error diagnostics in the task report.

```bash
git add benchmarks/gauntlet/import_bootstrap.py tests/test_gauntlet_runner.py
git commit -m "test: bind immutable tqdm sources"
```

---

### Task 2: Load real `tqdm` from captured bytes in digest-bound children

**Files:**
- Modify: `benchmarks/gauntlet/import_bootstrap.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Test: `tests/test_gauntlet_runner.py`

**Interfaces:**
- Consumes: `IMMUTABLE_TQDM_MANIFEST_ARGUMENT`, `immutable_tqdm_manifest()`, and `_capture_immutable_tqdm_sources()` from Task 1.
- Produces: `_ImmutableDependencySourceLoader(importlib.abc.SourceLoader)` and `_ImmutableDependencyFinder(importlib.abc.MetaPathFinder)` serving exactly `tqdm` records.
- Changes: `_run_child(repository_root: Path, *, profile: str, seed: int, samples: int, output: Path, dependency_paths: Sequence[str], dependency_environment_digest: str, immutable_tqdm_manifest: str, bootstrap_source: bytes, expected_commit: str) -> dict[str, object]`; the child command includes the required manifest argument immediately after the dependency digest.

- [ ] **Step 1: Add failing loader and child-isolation tests**

Add tests proving that a digest-bound bootstrap:

- imports the installed real `tqdm.tqdm` through `_ImmutableDependencySourceLoader`;
- imports `qbitunregistered.operations.unregistered_checks` successfully;
- keeps all dependency roots absent from `sys.path`;
- reports synthetic evaluator origins without local paths;
- rejects `tqdm` resource access, unknown `tqdm` modules, and external mandatory imports;
- validates every loaded `tqdm` module still has the immutable loader; and
- leaves ordinary non-digest-bound imports unchanged.

Rewrite `test_digest_bound_bootstrap_never_imports_swap_restored_dependency` into a test where verified `tqdm` source records a benign marker, the installed file is temporarily replaced with a side-effecting source, and the child must execute only the previously manifest-matching captured bytes. Preserve separate tests that reject changes before capture, during capture, and at final digest validation.

- [ ] **Step 2: Run the focused tests and record RED evidence**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'immutable_dependency_loader or swap_restored_dependency or controlled_bootstrap' -vv
```

Expected: the new digest-bound imports fail because dependency paths are removed and no immutable finder exists.

- [ ] **Step 3: Implement the immutable finder/loader**

The finder accepts only exact names present in its frozen source map and returns `None` for names outside `tqdm`. The loader compiles only retained bytes, exposes a fixed path-free origin, implements no resource reader or data access, and refuses a second source mutation. Keep `_WorktreePackageFinder` first for protected first-party names and place the immutable finder before ordinary path-based finders.

After evaluation, validate the source map identity, require the protected
finder and immutable finder to retain their first and second meta-path slots,
and require every loaded `tqdm` module to retain the expected loader, spec,
origin, and package status. Reject malformed non-`ModuleSpec` metadata through
the existing bounded dependency-isolation diagnostic rather than leaking a raw
attribute error. These checks detect final drift within the approved threat
boundary; they do not attest the complete historical execution path.

- [ ] **Step 4: Wire the manifest through paired orchestration**

Build the manifest once after `dependency_environment_identity` is captured. Pass the exact same canonical string to all eight crossover children. In the child bootstrap, require the manifest argument whenever `DEPENDENCY_DIGEST_ARGUMENT` is present, capture sources before removing dependency paths, then remove the paths and install the loader before protected production imports.

Update mocked `_run_child` expectations to require one identical manifest across all child calls. Do not add manifest details to the output artifact.

- [ ] **Step 5: Run focused checks and the real child import regression**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'immutable_dependency_loader or immutable_tqdm or swap_restored_dependency or controlled_bootstrap or dependency_environment' -vv
uv run basedpyright
uv run mypy benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/paired.py --ignore-missing-imports
```

Expected: all focused tests pass, including an actual `-s -S -P -` child process importing real `unregistered_checks`; BasedPyright and mypy are clean.

- [ ] **Step 6: Exercise BasedPyright LSP and commit**

Open all changed Python files through the actual language server, exercise symbols/definition/references for the finder and `_run_child`, verify version-matched zero-error diagnostics, and shut down cleanly.

```bash
git add benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/paired.py tests/test_gauntlet_runner.py
git commit -m "fix: load verified tqdm in paired children"
```

---

### Task 3: Add the protected qBittorrent API boundary and real CLI closure test

**Files:**
- Modify: `benchmarks/gauntlet/import_bootstrap.py`
- Modify: `benchmarks/gauntlet/tracker_runner.py`
- Test: `tests/test_gauntlet_runner.py`
- Test: `tests/test_gauntlet_safety.py`

**Interfaces:**
- Produces: `_QbittorrentApiShimState` with `install() -> None`, `validate() -> None`, `client_constructions: int`, and `exception_instances: int`.
- Produces protected modules `qbittorrentapi` and `qbittorrentapi.exceptions` with only `Client` and `APIConnectionError` respectively.
- Consumes the existing tracker fixture patch of `cli.create_client` before `cli.main()`.

- [ ] **Step 1: Add failing shim-contract tests**

Test exact module exports, protected path-free specs/origins, root-to-exceptions identity, and zero counters. Assert each of these fails closed:

```python
qbittorrentapi.Client()
qbittorrentapi.Client(host="unused")
qbittorrentapi.exceptions.APIConnectionError()
import qbittorrentapi.torrents
getattr(qbittorrentapi, "Session")
```

Add mutation probes that replace a module, exported class, spec, loader, or root `exceptions` attribute and require post-import/post-evaluation validation failure.

Add a real digest-bound child test that imports `benchmarks.gauntlet.tracker_runner`, executes the small CLI scenario through its fake client, and proves:

```python
assert shim.client_constructions == 0
assert shim.exception_instances == 0
assert notifications.APPRISE_AVAILABLE is False
assert not config.get("apprise_url")
assert not config.get("notifiarr_key")
```

- [ ] **Step 2: Run the focused tests and record RED evidence**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py -k 'qbittorrentapi_shim or digest_bound_tracker_cli or apprise' -vv
```

Expected: CLI import fails at `qbittorrentapi`, and the shim interfaces do not exist.

- [ ] **Step 3: Implement the fail-closed protected shim**

Create both modules from immutable bootstrap code before first-party imports. Use sentinel classes whose `__new__` increments the relevant counter and immediately raises `DependencyEnvironmentError`. Provide no module `__getattr__`, permissive subclasses, or additional qBittorrent symbols. Install fixed specs/loaders/origins and retain the exact expected object graph in `_QbittorrentApiShimState`.

`validate()` must compare module identity, exact exported keys after excluding required module metadata, root/exceptions linkage, types, specs, loaders, origins, and both zero counters. Run it immediately after protected imports and in the outermost final validation path.

- [ ] **Step 4: Enforce the tracker fixture boundary**

In `tracker_runner.py`, add an evaluator invariant at the real CLI scenario boundary requiring Apprise unavailable and notification configuration absent. Retain the current patch ordering so `cli.create_client` is replaced before `cli.main()` can call it. Do not weaken the process-audit boundary or substitute the production progress loop.

- [ ] **Step 5: Run focused and full tracker evaluator tests**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py -k 'qbittorrentapi_shim or tracker or bootstrap or dependency' -vv
uv run black --check benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/tracker_runner.py tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py
uv run flake8 benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/tracker_runner.py tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py --count --select=E9,F63,F7,F82 --show-source --statistics
uv run basedpyright
uv run mypy benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/tracker_runner.py --ignore-missing-imports
```

Expected: focused evaluator tests pass with no error diagnostics.

- [ ] **Step 6: Exercise BasedPyright LSP and commit**

Open all changed Python files, request symbols and definition/references for `_QbittorrentApiShimState`, collect zero error diagnostics for matching document versions, and shut down cleanly.

```bash
git add benchmarks/gauntlet/import_bootstrap.py benchmarks/gauntlet/tracker_runner.py tests/test_gauntlet_runner.py tests/test_gauntlet_safety.py
git commit -m "test: protect paired client imports"
```

---

### Task 4: Document the revised trust boundary and advance evaluator identity

**Files:**
- Modify: `benchmarks/gauntlet/quality-bar.toml`
- Modify: `benchmarks/gauntlet/runner.py`
- Modify: `benchmarks/gauntlet/paired.py`
- Modify: `benchmarks/gauntlet/README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_gauntlet_runner.py`

**Interfaces:**
- Changes evaluator identity from `1.11.0` to `1.12.0` without changing result schema 9.
- Changes pairing identity from `2.7.0` to `2.8.0` without changing paired schema 6.

- [ ] **Step 1: Add failing identity and documentation assertions**

Update tests to require evaluator `1.12.0` and pairing `2.8.0`, and add text assertions that the documented paired child boundary includes immutable real `tqdm`, the protected fake-client shim, absent installed dependency roots, and the unchanged optional-Apprise path.

- [ ] **Step 2: Run focused tests and record RED evidence**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'version or evaluator_identity or documentation' -vv
```

Expected: failures still report evaluator `1.11.0` or pairing `2.7.0`;
the approved immutable-`tqdm` boundary wording is already carried forward.

- [ ] **Step 3: Update code and documentation consistently**

Set the new identities in `quality-bar.toml`, `runner.py`, and `paired.py`. Explain that:

- all installed roots remain off child `sys.path`;
- real `tqdm` executes only from captured manifest-matching bytes;
- qBittorrent API is an evaluator-owned fail-closed shim because the fake client is the locked boundary;
- Apprise is intentionally unavailable for tracker fixtures;
- the complete dependency tree remains fingerprinted before/after; and
- artifacts from evaluator 1.11.0 and earlier cannot establish the repaired paired tracker gate.

Do not claim that the gauntlet verifies installed qBittorrent client imports or supports arbitrary third-party packages.

- [ ] **Step 4: Run documentation and identity tests**

Run:

```bash
uv run pytest tests/test_gauntlet_runner.py -k 'version or evaluator_identity or documentation or quality_bar' -vv
git diff --check
```

Expected: tests pass and no whitespace errors remain.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/gauntlet/quality-bar.toml benchmarks/gauntlet/runner.py benchmarks/gauntlet/paired.py benchmarks/gauntlet/README.md ARCHITECTURE.md CONTRIBUTING.md CHANGELOG.md tests/test_gauntlet_runner.py
git commit -m "docs: define immutable paired imports"
```

---

### Task 5: Run security, compatibility, and whole-branch review gates

**Files:**
- Review: all changes from `origin/main` through branch HEAD
- Create locally only: private task/review reports under `.superpowers/sdd/` if already ignored

**Interfaces:**
- Consumes all Task 1–4 interfaces.
- Produces a clean evaluator-fix commit range with no unresolved Critical or Important findings.

- [ ] **Step 1: Run the full repository gate**

Run:

```bash
uv run black --check .
uv run flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
uv run pytest --cov=qbitunregistered --cov-report=term-missing --cov-fail-under=60
uv run basedpyright
uv run mypy qbitunregistered --ignore-missing-imports
uv run --with pip-audit pip-audit
uv run --with bandit bandit -q -r qbitunregistered -ll
uv build
```

Expected: all required gates pass. Report advisory findings honestly; do not waive a new failure.

- [ ] **Step 2: Run the actual language-server gate over every changed Python file**

Use `uv run basedpyright-langserver --stdio` with a real LSP client. Open every changed Python file at matching versions, collect diagnostics, request document symbols for each file, and exercise at least one definition and references request across manifest, loader, shim, and paired orchestration. Require zero error diagnostics and a clean shutdown.

- [ ] **Step 3: Smoke-test the built wheel outside the checkout**

Install the wheel into a fresh temporary virtual environment outside the source tree and run both console commands with `--help` or `--version`. Confirm no generated build output is staged.

- [ ] **Step 4: Dispatch independent PythonPro security and compatibility reviews**

The fresh reviewer must read `AGENTS.md`, review `origin/main..HEAD` without editing, and explicitly probe:

- swap-import-restore and capture races;
- symlink/reparse/descriptor identity behavior on POSIX and Windows;
- manifest parsing bounds and collision handling;
- loader/resource/native-extension escapes;
- shim mutation and unexpected API use;
- diagnostics for secrets and local paths;
- ordinary non-paired compatibility; and
- dry-run/network/filesystem invariants.

Require BasedPyright CLI and actual LSP evidence. Resolve every Critical or Important finding through the original PythonPro implementer with a new RED test, then rerun a fresh scoped review.

- [ ] **Step 5: Confirm clean branch state and commit any review fixes**

Run:

```bash
git status --short
git diff --check origin/main..HEAD
git log --oneline origin/main..HEAD
```

Expected: clean worktree, intentional commits only, no credentials/generated files, and no unresolved review finding.

---

### Task 6: Merge the evaluator fix and resume the bulk-tracker gauntlet

**Files:**
- Rebase: branch `codex/bulk-tracker-metadata`
- Verify: evaluator/quality/dependency files across orchestrator, control, and candidate worktrees
- Publish privately: paired quick/full JSON artifacts outside repository worktrees

**Interfaces:**
- Consumes the merged evaluator-fix commit and the existing bulk-tracker commits.
- Produces valid paired `tracker-quick` and `tracker-full` evidence with control exact transport `(1, 0, N)` as represented by the evaluator counters and candidate bulk transport `(0, 1, 0)`.

- [ ] **Step 1: Publish and merge the evaluator-only PR**

Push the clean evaluator branch, open a focused PR summarizing the import-policy defect and trust-boundary repair, wait for required CI/review checks, and merge only if all checks are green and no Critical/Important review findings remain.

- [ ] **Step 2: Create clean post-merge worktrees**

Create a fresh control at the merged evaluator commit. Rebase or replay the bulk-tracker commits onto that same commit in its isolated candidate worktree. Preserve the existing optimization commits and resolve only evaluator-version/test expectation conflicts.

- [ ] **Step 3: Prove paired input identity before execution**

Verify byte identity across orchestrator/control/candidate for:

```text
benchmarks/gauntlet/**
pyproject.toml
uv.lock
```

Require clean Git identities for all three and confirm no local credentials, ignored Python sources, or generated output can enter the evaluator digest.

- [ ] **Step 4: Run paired tracker quick**

Run from the clean post-merge orchestrator:

```bash
uv run python -I -S -B benchmarks/gauntlet/launcher.py \
  --profile tracker-quick \
  --paired-control /absolute/path/to/fresh-control \
  --paired-candidate /absolute/path/to/rebased-candidate \
  --output /private/external/tracker-quick.json
```

Require all safety/result/API/variance/identity gates, endpoint collapse, CPU ratio at most `1.0`, and peak-memory ratio at most `1.25`. Publish no result if the command fails.

- [ ] **Step 5: Run paired tracker full only after quick passes**

Use the same clean identities and command shape with `--profile tracker-full` and a separate mode-0600 external artifact. Require the same gates and ratios.

- [ ] **Step 6: Complete the production branch review and merge decision**

Run the production branch's full project checks and actual LSP gate again after rebase. Dispatch a fresh whole-branch PythonPro reviewer covering destructive safety, batching/cache isolation, malformed metadata fail-closed behavior, CLI compatibility, and the paired artifacts. If no Critical/Important findings remain and CI is green, open/update and merge the bulk-tracker PR. Do not perform a live qBittorrent run without a new explicit approval.
