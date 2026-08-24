# Test report — Proof Assistant 0.1.0

Tested on 2026-08-23 in `America/New_York`.

## Release result

The current source passed the complete automated suite, the supported local
installer, a clean Python 3.13 wheel installation, real Lean memory
calibration, and a provider-backed two-claim manuscript verification.

| Gate | Result |
|---|---|
| complete automated suite | **433 passed** on macOS in 133.94 seconds |
| Cortex Linux suite | **433 passed** in 99.64 seconds |
| supported installer | **passed**; compiler compile/run and 393 tests in 123.92 seconds |
| Ruff lint and format | **passed** |
| strict mypy | **passed**; 64 source files |
| explicit `Any` policy | **passed**; 98/98 AST uses, 11 boundary modules `Any`-free |
| Python `compileall` | **passed** |
| `git diff --check` | **passed** |
| clean Python 3.13 wheel | **passed**; `py.typed` and downstream strict mypy |
| real Lean calibration | **passed**; no Codex traffic |
| real manuscript verification | **verified**, 2/2 current certificates |
| RepoProver checkout | exact commit, clean and unchanged |
| upstream RepoProver PR | **NOT CREATED** |

## Python 3.13 roadmap acceptance

Strict mypy now checks all 64 package source files in CI. A companion policy
gate caps the remaining 98 explicit AST-level `Any` uses and requires 11
protocol, backend, JSON, task, Lean, workflow, and catalog boundary modules to
remain `Any`-free. The original roadmap inventory contained 225 textual `Any`
occurrences; the implemented tree contains 121, including comments, while the
highest-traffic request/response and persistence seams now validate recursive
`JSONValue` data before it reaches application logic.

The universal wheel and source distribution both contain `py.typed`. The wheel
was installed into a fresh external CPython 3.13.15 environment, passed the
native compiler compile-and-execute command, and exposed precise public types
to a separate strict-mypy consumer.

Three immutable, high-volume incremental records were selected for
`slots=True` after measurement. For 100,000 representative instances on the
tested Mac, peak traced allocation fell from 24.42 to 19.07 MiB for
`SourceObject`, 11.45 to 7.63 MiB for `ManuscriptEdge`, and 12.21 to 8.39 MiB
for `LeanDeclaration`. Construction throughput did not change materially, so
the accepted optimization claim is memory reduction only.

The same source tree passed compiler preflight, a clean uv-managed Python 3.13
installation, Ruff, strict mypy, the typing policy, and all 433 tests on Cortex
(Ubuntu, Linux 6.8.0-136, x86_64). This clean run did not reproduce the earlier
host-specific Linux behavior. GitHub's Ubuntu 24.04 runner subsequently exposed
a genuine test synchronization race: the deletion outcome's copyable result
could mount before its navigation toolbar. The acceptance test now waits for
both controls, matching the complete-screen readiness rule used elsewhere.

## Fresh-project and state-isolation regressions

This revision addresses three failures found on the real `lapl` project and in
release testing:

- Lean-version auto-tuning no longer invokes `lake env` before cache setup.
  The direct `lean --version` probe cannot materialize a fresh project's
  `.lake/packages` directory.
- Each isolated build now has an atomic ownership marker bound to the exact
  project and build target. Proof Assistant-owned disposable packages may be
  replaced by the matching shared depot, while imported, unmarked, mismatched,
  and explicitly unowned package trees are refused.
- Detached workers receive the launcher's exact catalog and machine-settings
  paths. A direct/legacy hidden worker uses a project-local catalog and cannot
  populate the real interactive project list. A sentinel credential test also
  proves that secrets are not serialized in durable worker commands.

The existing `/Users/vui1/proof-assistant/lapl` build was repaired without
modifying its authoritative source at `/Users/vui1/manuscripts/laplacians`.
Thirty-one managed-snapshot source files were compared against both the
external source and the recoverable deleted-project copy; every corresponding
file was byte-identical. The production catalog was backed up, purged of stale
test records, and retained only the valid `lapl` project. Its SHA-256 remained
`245bd42a1f9887b579966df9bee5c932a61bbfa25a49e52d0328e45aaa53373f`
across the final complete suite.

A new disposable project was then initialized from the two-claim test
manuscript and prepared with the installed command. `cache prepare` reused
dependency depot `deps-e50bd489673215a5b89a0dc2` (7.06 GiB), created an
ownership-marked isolated root build of only 23 MiB, and completed `lake build`
successfully. The disposable project, isolated build, lease, and index row were
removed after inspection; no Codex traffic was used.

The new terminal folder chooser is covered at an 80×24 viewport. Tests exercise
keyboard traversal, compact Up/Home/Open/Select/Cancel controls, copyable paths,
unreadable directories, cancellation, explicit-selection-only persistence,
sorted symlink-deduplicated listings, valid/stale/malformed preferences, safe
XDG handling, and the home fallback. The preference path is injected in backend
tests, and the real machine preference file was neither created nor modified.
The final full-suite run also exposed an existing Textual mount-order race in
the recoverable-deletion result screen. Focus assignment is now deferred until
refresh, readiness assertions wait for the complete control set, and both
deletion scenarios passed ten consecutive stress repetitions before the final
complete suite.

## Adaptive-concurrency acceptance

The implementation was checked against GitHub issue 2 and its complete
1,825-line design guide. The guide used for the implementation had SHA-256:

```text
8bf7b42fec5493caebacfb98324ec5a26859d035f22f534757d960809d92f53f
```

The automated tests cover:

- separate cross-process AI, Lean, and build admission controllers;
- one global AI budget shared across task classes and distributed lease
  reclamation;
- additive increase, multiplicative decrease, `Retry-After`, jittered backoff,
  fixed mode, and economy/balanced/throughput policies;
- Linux affinity, cgroup, and SLURM allocation detection, ordinary Linux and
  macOS detection, and missing optional metrics;
- CPU/RAM Lean limits, real project import profiles, p95 RSS calibration,
  stale-profile rejection, pressure hysteresis, and manual overrides;
- a machine-global conservative Lean budget across every fresh project profile
  for the same hardware allocation, preventing a light or uncalibrated project
  from raising a heavier project's safe limit;
- build limits, pressure adaptation, hard ceilings, and a four-process yellow
  pressure race in which exactly one full build is admitted;
- dependency-priority scheduling and repair-first duplicate escalation;
- machine-scoped configuration, deterministic CLI/environment precedence,
  future project-overlay composition, and safe live limit reductions;
- detached worker refresh of changed machine settings without revoking
  in-flight leases or allowing stale adaptive state to restore old limits;
- TUI **Settings → Concurrency / Resources**, configured-versus-effective
  values, live telemetry, warnings, presets, reset-to-auto, project calibration,
  adaptive-history reset, and the separate **Legacy settings** page;
- configured/effective run provenance, peaks, queues, pressure events, and
  calibration identity; and
- reliable closure of every SQLite connection in admission, job, and cache
  stores, including early returns and failures;
- explicit closure of Codex app-server standard-I/O pipes when a client exits,
  preventing long-lived coordinators from accumulating file descriptors; and
- cancel-first, button-confirmed recoverable project deletion, including busy
  refusal, delete-time backend revalidation, copyable paths, and recovery
  destination reporting without a typed-name hurdle.

The concurrency package is in `src/proof_assistant/concurrency/`; the UI-neutral
settings and project contracts are in `workflow/`; Textual remains a replaceable
client. Backend modules cannot import Textual or Rich.

## Real Lean calibration

Command:

```bash
proof-assistant benchmark lean-concurrency \
  --project /Users/vui1/repoprover-test-runs/concurrency-small-20260823
```

| Evidence | Value |
|---|---|
| disposable REPL processes | 2, sequential |
| working RSS samples | 8 |
| p95 working process-tree RSS | 3.564 GiB |
| automatic per-REPL budget | 5.346 GiB |
| recommended/effective Lean pool | 2 |
| calibration profile | `7643540f04f6ffffad313ce9f1575307` |
| Codex traffic | none |
| proof state modified | no |

The first real attempt exposed an invalid assumption that `Manuscript.lean`
was necessarily a loadable Lake module. Calibration now derives its workload
from the project's actual declared imports; a regression protects generated
projects that do not produce `Manuscript.olean`.

## Real manuscript verification

Command:

```bash
proof-assistant manuscript verify \
  --project /Users/vui1/repoprover-test-runs/concurrency-small-20260823 \
  --model gpt-5.6-sol \
  --effort high \
  --batch-size 1 \
  --turn-timeout 1800
```

The final successful pass was run 5 at manuscript snapshot
`5ca8aa75b950e51bae19ae56a17a4c78c862ea09`.

| Evidence | Value |
|---|---|
| outcome | `verified` |
| selected claims | 2 |
| current Lean certificates | 2 |
| clarification requests | 0 |
| Lean | 4.28.0 |
| Mathlib | `8f9d9cff6bd728b17a24e163c9402775d9e6a365` |
| configured policy | adaptive, balanced, unknown Codex plan |
| effective AI | 4, ceiling 8 |
| effective Lean | 2, automatic maximum 32 |
| effective builds | 1, safety ceiling 8 |
| observed peaks | AI 1, Lean 1, build 1 |
| observed pressure | yellow memory; no swap growth during recorded samples |

The claims form a dependency chain, so only one Codex proof turn was ready at a
time even though logical multi-agent fan-out defaults to two. The run exercised
Codex, Lean checks, agent and host Lake builds, deterministic integration,
dependency extraction, and independent final certification. During one proof
turn the ledger showed simultaneous AI and build leases, demonstrating that
those capacities are independent.

The sustained yellow-memory state exposed a no-progress edge case in the first
acceptance attempt: all full builds were blocked. Yellow admission now permits
exactly one full build machine-wide under a serialized cross-process check.
This remains conservative while guaranteeing progress; red and emergency
pressure still block new builds.

The Codex child was started with apps and plugins disabled, every discovered
local MCP server disabled, bundled/workspace skill instructions disabled, and
every discovered local skill disabled. The backend also queried the child and
failed closed unless no external tools or enabled skills remained exposed.

## Installed and packaged state

The supported installer was run with:

```text
PROOF_ASSISTANT_VENV=/Users/vui1/.venvs/proof-assistant
PROOF_ASSISTANT_CACHE_HOME=/Users/vui1/.cache/repoprover-codex
PROOF_ASSISTANT_PYTHON=3.13
```

It reused `/Users/vui1/.local/bin/uv`, installed the editable package and test
dependencies, compiled and executed a native C program with `/usr/bin/clang`,
initialized the cache, and ran all 393 tests in 123.92 seconds. The compiler
check is mandatory in the installer and records `LEAN_CC` when Lean's bundled
compiler is unusable.

An sdist and universal wheel were built outside the repository with `uv`. The
wheel was installed into a disposable Python 3.13.15 environment and checked
for:

- distribution/import version 0.1.0;
- `proof-assistant` and deprecated 0.1 `repoprover-codex` entry points;
- packaged `proof_assistant.lean/DependencyExtractor.lean`;
- packaged machine-local preference module plus public `ProofAssistantWorkflow`
  and `ConcurrencyResourcesScreen` imports;
- fresh automatic concurrency provenance;
- Textual 1.0.0 compatibility; and
- native compiler compile-and-execute behavior.

The disposable build and environment were deleted after the checks. The live
installation is:

```text
source:      /Users/vui1/src/proof-assistant
environment: /Users/vui1/.venvs/proof-assistant
command:     /Users/vui1/.local/bin/proof-assistant
cache:       /Users/vui1/.cache/repoprover-codex
```

All four locations are outside Dropbox. The historical cache path remains
intentional so existing Mathlib artifacts are shared rather than duplicated.

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
| psutil / NetworkX | 7.2.2 / 3.6.1 |

The RepoProver checkout at `/Users/vui1/src/repoprover` was clean at the exact
commit above and remained untouched by Proof Assistant publication work.

## Storage and repository hygiene

At the final cache check:

```text
managed cache:          9.88 GiB
cache limit:           16.00 GiB
filesystem free:      118.41 GiB
dependency depots:      7.06 GiB
isolated builds:        2.43 GiB
active reservations:    0.00 GiB
```

Before publication the repository is checked for private-key material,
credentials, tokens, `.env`/`auth.json` files, machine-private configuration,
virtual environments, package builds, caches, and temporary test output.
Generated repository-local caches are removed, ignored-path rules are verified,
and the remote history is fetched before any push. No command in this work may
push to or open a pull request against `facebookresearch/repoprover`.
