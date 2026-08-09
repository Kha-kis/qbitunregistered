# Gauntlet Immutable Dependency Imports Design

## Objective

Make digest-bound paired tracker gauntlet children capable of importing and
executing the real production tracker path without weakening dependency-tamper
isolation or changing production behavior.

The current paired child verifies the installed dependency tree and then
removes every dependency directory from `sys.path`. That correctly prevents a
swap-import-restore attack, but it also makes the real application import
closure impossible: `unregistered_checks` imports `tqdm`, and the later CLI
scenario imports `qbittorrentapi`. The observed `ModuleNotFoundError` is an
evaluator trust-boundary defect, not a missing installation.

## Scope and branch separation

This work belongs on a dedicated evaluator-fix branch based on the common
control commit. It may change only:

- `benchmarks/gauntlet/` evaluator and bootstrap code;
- gauntlet tests;
- evaluator, architecture, contribution, and changelog documentation.

It must not change `qbitunregistered/`, dependency metadata, configuration,
console commands, or compatibility wrappers. The evaluator fix must merge
before the bulk-tracker optimization is rebased and measured.

## Chosen design

Use a deliberately narrow hybrid boundary:

1. Load the real `tqdm` package from immutable, digest-bound source bytes.
2. Install a protected evaluator-owned `qbittorrentapi` import-surface shim for
   the client boundary that the tracker fixture already replaces.
3. Let the existing optional `apprise` import fail normally.
4. Keep every installed dependency directory absent from the measured child's
   `sys.path` for the entire evaluation.

This preserves the real `tqdm.tqdm` iteration and allocation behavior inside
the measured production loop. It avoids admitting the unused
`qbittorrentapi`/Requests/Apprise dependency closure, which currently reaches
native charset-normalizer and PyYAML modules and therefore cannot be served by
a portable source-only loader.

The design is intentionally not a general dependency import system. A future
mandatory `tqdm` dependency or a new production use of `qbittorrentapi` must
fail closed and receive a separate review.

## Immutable `tqdm` snapshot

The paired coordinator derives a manifest for the single allowed third-party
namespace, `tqdm`, from the same canonical dependency roots used for the
dependency-environment digest. The manifest contains the normalized module
name, package status, canonical relative source path, and SHA-256 digest for
every accepted Python source file. It contains no absolute path or source
content and is bound into every child invocation.

The manifest builder and child validator must:

- accept exactly one canonical `tqdm` package root;
- accept regular `.py` source files only;
- reject symbolic links, redirects, duplicate module names, case-folded name
  collisions, namespace ambiguity, bytecode-only modules, native extensions,
  and unsupported package resources;
- use bounded counts and byte sizes before reading source;
- open sources without following links where the platform supports it, verify
  the opened descriptor is the expected stable regular file, and read bytes
  once;
- require each captured source digest to match the coordinator manifest;
- reject any import outside the captured `tqdm` namespace; and
- preserve the existing full dependency-environment validation before and
  after evaluation.

After validation, the child retains only immutable in-memory source records.
It removes dependency roots from `sys.path` before any protected production
import and installs a dedicated finder/loader that compiles `tqdm` modules from
the captured bytes. The loader never reopens installed files. Module specs and
origins use a fixed evaluator-owned synthetic scheme and do not disclose local
paths in artifacts or diagnostics.

The child validates after evaluation that every loaded `tqdm` module came from
this loader and that the loader's manifest and captured byte identities are
unchanged.

## Protected qBittorrent API shim

The tracker gauntlet uses an in-memory qBittorrent client and patches
`cli.create_client` before `cli.main()` executes. The real third-party client
is therefore outside the evaluated boundary. Immutable bootstrap code installs
the minimum import surface needed to import the production CLI:

- `qbittorrentapi.Client`: a sentinel class used only by annotations and the
  default argument in `client.py`; construction immediately raises a dedicated
  evaluator error;
- `qbittorrentapi.exceptions.APIConnectionError`: a minimal `Exception`
  subclass required by the CLI's exception handler.

Both `qbittorrentapi` and `qbittorrentapi.exceptions` are fixed protected
modules registered before production imports. The exceptions module is also
the root module's `exceptions` attribute. Their specs and origins identify the
protected evaluator boundary rather than a filesystem location.

The shim exposes no permissive attribute fallback. Unknown attributes,
submodules, construction of `Client`, or instantiation of the shim exception
fail the evaluation. The child records construction/instantiation counters and
requires both to remain zero. It also validates the module identities, specs,
classes, and exported attributes after protected imports and again after the
evaluation.

This shim does not claim to test whether the installed `qbittorrentapi` package
imports correctly. Packaging and application tests retain that responsibility.

## Optional notifications dependency

With dependency roots absent, `notifications.py` follows its supported
`ImportError` path for Apprise. Tracker fixtures contain no Apprise or Notifiarr
configuration. The evaluator requires `APPRISE_AVAILABLE` to be false and
requires the scenario configuration to keep notifications disabled. Any
attempt to construct or send a notification remains prohibited by the existing
process-audit boundary.

## Data and trust flow

1. The source-only launcher verifies the orchestrator bootstrap as it does
   today.
2. The coordinator verifies identical evaluator and dependency-lock bytes in
   orchestrator, control, and candidate worktrees.
3. The coordinator fingerprints the complete installed dependency environment
   and constructs the bounded `tqdm` source manifest.
4. Each child revalidates the complete environment, captures only manifest-
   matching `tqdm` sources into memory, removes dependency roots from
   `sys.path`, and installs the immutable loader and protected qBittorrent shim.
5. Protected first-party imports and tracker evaluation run normally.
6. The child validates loaded-module and shim identities, protected sources,
   and the complete dependency environment before exiting.
7. The coordinator repeats its existing worktree, evaluator, and dependency
   checks after every crossover child.

Control and candidate must use byte-identical evaluator, quality-bar,
`pyproject.toml`, and `uv.lock` inputs. The loader and shim are evaluator code,
so their bytes are covered by the existing evaluator identity gate.

## Failure behavior

Every uncertainty fails closed before publishing paired evidence. Diagnostics
identify only the failed invariant and exception type; they do not reveal
dependency roots, source paths, environment values, or captured source bytes.

No fallback may add `site-packages` to `sys.path`, import an installed package
directly, substitute a simplified `tqdm`, or continue after a loader/shim
validation failure. Ordinary non-paired execution keeps its current installed-
dependency behavior.

## Test-first acceptance

Implementation starts with failing regression tests for:

- a real digest-bound child importing the tracker runner and later production
  CLI closure;
- real `tqdm.tqdm` being loaded by the immutable loader and used by the
  measured production loop;
- a swap-import-restore attempt executing only previously verified captured
  bytes and never the replacement side effect;
- before-capture, during-capture, persistent, and final dependency tampering;
- dependency roots remaining absent from `sys.path`;
- duplicate roots, links, path collisions, unallowlisted modules, resources,
  bytecode-only modules, and native extensions failing closed;
- unexpected qBittorrent attributes/submodules, `Client` construction,
  exception instantiation, and shim mutation failing closed;
- Apprise being unavailable and notification configuration remaining empty;
- unchanged standalone/non-paired imports; and
- supported behavior on every CI Python/platform combination.

The swap/restore regression must be updated from "dependency remains
unimportable" to "the child executes only manifest-matching captured bytes."
Existing dependency digest, protected-source, output-publication, descriptor,
filesystem, network, dry-run, scenario-contract, and paired-role tests remain
in force.

Required verification is:

```bash
uv run black --check .
uv run flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.venv/
uv run pytest --cov=qbitunregistered --cov-report=term-missing --cov-fail-under=60
uv run basedpyright
uv run mypy qbitunregistered --ignore-missing-imports
uv run --with pip-audit pip-audit
uv run --with bandit bandit -q -r qbitunregistered -ll
```

PythonPro must implement and independently review the change, using
BasedPyright for navigation and diagnostics and exercising the actual
BasedPyright language server. Because this is evaluator trust code, review must
explicitly probe swap/restore, import-surface expansion, native/resource
loading, and diagnostic redaction.

## Merge and measurement sequence

1. Implement and review this evaluator-only branch.
2. Run the full project verification and a same-code paired tracker smoke test.
3. Open and merge the evaluator-fix PR.
4. Create a fresh control worktree at the merged evaluator commit.
5. Rebase the bulk-tracker branch onto that commit.
6. Verify identical evaluator, quality-bar, dependency metadata, and lock bytes
   across orchestrator, control, and candidate.
7. Run paired `tracker-quick`; publish no artifact unless all safety and
   comparison gates pass.
8. Run paired `tracker-full` only after quick passes.
9. Complete a fresh whole-branch PythonPro review before merging the production
   optimization.

No live qBittorrent, network, or media-library access is authorized by this
design. A protected live dry-run remains a separate explicit approval gate.

## Rejected alternatives

- **Installed dependency roots on `sys.path`:** rejected because before/after
  hashes cannot prevent swap-import-restore execution.
- **Generic immutable loader for the full CLI closure:** rejected because it
  would absorb Requests, Apprise, native charset-normalizer/PyYAML modules, and
  resource semantics unrelated to the measured client boundary.
- **Evaluator-owned `tqdm` surrogate:** rejected because it would change common
  runtime and allocation behavior inside the measured production loop.
- **Production optional-import fallback or progress removal:** rejected because
  it changes supported application behavior and can hide packaging defects.
- **Direct installed `qbittorrentapi` import:** rejected because the fake client
  boundary does not exercise it and admitting its transitive closure would
  needlessly expand the evaluator trust surface.
