# Test report — Proof Assistant 0.1.0

Tested on 2026-08-23 in `America/New_York`.

## Result

Proof Assistant 0.1.0 passed the complete automated suite, a clean Python 3.13
wheel installation, mandatory native compiler execution, an installed-command
multi-root manuscript acceptance, and cache and repository hygiene checks. The
earlier provider-backed two-claim acceptance remains recorded separately below
and was not rerun for this main-source/TUI development pass.

- Complete automated suite: **201 passed**.
- Supported installer: **passed**, including its mandatory compiler check and
  complete suite.
- Fresh wheel and Python 3.13 environment: **passed**.
- Textual Pilot suite: **13 passed**.
- Live catalog regression: the installed backend surfaced the existing
  `laplacians` project as **NEEDS_MAIN_FILE** with 17 candidates without
  modifying its legacy configuration.
- Installed multi-root sample: **1 selected claim indexed**, alternate root
  excluded.
- Earlier real small manuscript baseline: **2/2 claims certified**.
- Earlier unchanged second pass: **2 certificates reused, 0 proof turns
  required**.
- Shared Mathlib depot: **reused**; no new `~/.cache/proof-assistant` tree.
- RepoProver checkout: **clean and unchanged**.
- Upstream RepoProver PR: **NOT CREATED**.

## Tested environment

| Component | Tested value |
|---|---|
| OS | macOS 12.7.6 (21H1320), x86_64 |
| Python | CPython 3.13.15 |
| uv | 0.9.26 (`ee4f00362`, 2026-01-15) |
| Codex CLI | 0.149.0 |
| Git | 2.37.1 (Apple Git-137.1) |
| Lean | 4.28.0, commit `7e01a1bf5c70fc6167d49c345d3bf80596e9a79b` |
| Lake | 5.0.0-src+7e01a1b |
| Mathlib | v4.28.0, commit `8f9d9cff6bd728b17a24e163c9402775d9e6a365` |
| RepoProver | commit `386adba3df572cb71df534add2c764e071898a2e` |
| native compiler | `/usr/bin/clang`, Apple clang 14.0.0 |
| Textual / Rich | 1.0.0 / 14.3.4 |
| source | `/Users/vui1/src/proof-assistant` |
| Python environment | `/Users/vui1/.venvs/proof-assistant` |
| managed cache | `/Users/vui1/.cache/repoprover-codex` |
| installed command | `/Users/vui1/.local/bin/proof-assistant` |

The source checkout, environment, managed projects, and cache all resolved
outside Dropbox. The historical cache name is intentionally preserved to avoid
duplicating the multi-gigabyte Mathlib depot.

## Automated contract suite

Command:

```bash
/Users/vui1/.venvs/proof-assistant/bin/python -m pytest -q
```

Result: `201 passed` (56.48 seconds in the final installer run).

The suite covers:

- Codex app-server framing, provider failures, model/effort validation, and
  fail-closed disabling of MCP servers, apps, plugins, and skills;
- native compiler detection and compile-and-execute behavior;
- cache admission, leases, shared dependency depots, bounded coarse-index GC,
  interrupted reconciliation, and Dropbox rejection;
- mandatory main-file inspection/selection, deterministic recursive LaTeX
  closure indexing, multi-input source spans, alternate-root exclusion,
  dependency graphs, snapshots, SQLite state, correspondence, and kernel-backed
  certificates;
- project-owned default/custom `VERIFY.yaml` and migration of older external
  task configurations;
- backend-owned project catalog reconciliation, default-path resolution,
  destination occupancy, incomplete-directory visibility, explicit ambiguous
  legacy-root migration, and non-destructive conflict handling;
- stable before/copy/after source inventories, add/modify/delete/rename plans,
  task changes, transitive impact closure, and stale confirmation rejection;
- deterministic resume routing for complete, interrupted, failed, externally
  busy, clarification, and changed-source states;
- exact clarification file/span/excerpt/blocked-claim presentation, strict
  optional Codex narration, deterministic fallback, and provenance storage;
- Textual one-file/multi-file root selection, pre-creation review, stateful
  wizard back-navigation, new/resume, task editing, copyable detailed progress,
  cooperative cancellation reports, exact source display, change confirmation,
  findings, and recovery screens;
- cancellation boundary cleanup, durable certificate preservation, retryable
  in-flight claims, and legacy orphaned-`PROVING` recovery; and
- the architecture rule that backend code cannot import Textual or Rich.

Ruff (`E`, `F`, `I`, `UP`), Ruff formatting, `compileall`, and
`git diff --check` also passed.

## Packaging and installer gates

`scripts/install-dev.sh` was run with:

```text
PROOF_ASSISTANT_VENV=/Users/vui1/.venvs/proof-assistant
PROOF_ASSISTANT_CACHE_HOME=/Users/vui1/.cache/repoprover-codex
PROOF_ASSISTANT_PYTHON=3.13
```

It used `uv`, installed the package and development dependencies, executed
`proof-assistant compiler-check` before cache/test work, compiled and ran a C
program with `/usr/bin/clang`, initialized the preserved cache, and ran all 201
tests.

An sdist and wheel were built outside Dropbox. The wheel was installed into a
fresh Python 3.13.15 environment and validated for:

- version `0.1.0` and distribution/import identity;
- package license metadata `CC-BY-NC-4.0` matching `LICENSE`;
- primary `proof-assistant` and deprecated 0.1 `repoprover-codex` entrypoints;
- packaged `proof_assistant.lean/DependencyExtractor.lean` resource;
- pinned supported Textual 1.x resolution (`1.0.0` in the test);
- packaged main-file/cancellation contracts and Textual screens import cleanly;
- native compiler compile/run; and
- cache doctor against the existing shared cache.

The fresh-wheel gate initially exposed that `textual>=1,<10` allowed an
incompatible Textual 8 release. The supported range was corrected to
`textual>=1,<2`, guarded by a distribution-contract test, and the entire wheel
gate was rerun successfully.

## Installed main-source acceptance

The installed command was exercised against a fresh source container outside
Dropbox with `paper.tex`, its nested `sections/result.tex` input, and an
independent `alternate.tex` document containing a deliberately duplicate label.

| Evidence | Value |
|---|---|
| discovered LaTeX candidates | 3 |
| explicit selection required | yes |
| conventional suggestion | `paper.tex` |
| selected main file | `paper.tex` |
| persisted input closure | `sections/result.tex` |
| indexed claims | 1 |
| duplicate label in alternate root | excluded; no error |
| initialized snapshot | `8a801c92cb606d5aa8f3ac99aed48cd0d051540a` |

The acceptance project and fresh wheel environment were moved to Trash after
the evidence was recorded.

## Earlier real verification acceptance

The following provider-backed evidence was produced before this main-source/TUI
pass and remains useful regression context. It was not rerun in the final 201-test
gate.

A fresh managed project outside Dropbox was initialized from the two-claim
incremental manuscript fixture without an external task argument.

| Evidence | Value |
|---|---|
| indexed claims | 2 |
| manuscript snapshot | `2c55a7285f4ae09bafa2ec019511feec13f403d1` |
| project task SHA-256 | `5675e9a3186aebcc0e5526b84202f1cf5e843da98fc8c780658612fd3a05fea0` |
| dependency key | `e50bd489673215a5b89a0dc2` |
| shared dependency depot | reused: yes |
| native compiler | `/usr/bin/clang` |
| `lake build` | OK |
| first verify outcome | `verified` |
| first verify certificates | 2 certified, 0 reused |
| unchanged verify outcome | `verified` |
| unchanged verify certificates | 0 new, 2 reused |
| clarification requests | none |

During the real proof pass the observed Codex child command contained
`--disable apps`, `--disable plugins`, disabled entries for every discovered
local MCP server, `skills.include_instructions=false`, disabled bundled skills,
and disabled entries for every discovered local skill. Lean independently built
the resulting declarations before certification.

The acceptance project, its 24 MiB isolated build, temporary wheel environments,
and package-build directories were moved to Trash after the results were
recorded. The shared dependency depot was retained.

## Cache and storage

Final cache status:

```text
root: /Users/vui1/.cache/repoprover-codex
managed: 8.84 GiB
limit: 16.00 GiB
filesystem free: 118.84 GiB
minimum free: 25.00 GiB
dependency depots: 7.06 GiB
isolated project builds: 1.38 GiB
Mathlib downloads: 0.39 GiB
active reservations: 0.00 GiB
```

The final GC reconciliation performed zero recursive measurements, confirming
that normal operation uses the bounded coarse accounting index. Only the active
`/Users/vui1/.venvs/proof-assistant` environment and the explicitly protected
`/Users/vui1/myenv` remain; obsolete and temporary environments were moved to
Trash. The stale Dropbox checkout was also moved to Trash.

## Repository hygiene

Before publication:

- tracked private-filename scan: clean;
- credential/token/private-key content-pattern scan: clean;
- ignored build/environment/cache rules: verified;
- generated repository-local caches and package artifacts: removed after final
  tests;
- Markdown local-link audit: 17 files, 0 broken links;
- `git diff --check`: passed;
- local branch: `main`;
- package commit author/committer name: `vladimir.itskov`;
- RepoProver worktree at the exact commit above: clean; and
- no command was issued to push to or create a pull request for
  `facebookresearch/repoprover`.

Publication verification is recorded in the release handoff after the final
commit and remote repository rename.
