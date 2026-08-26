# Test report — Proof Assistant 0.1.0

Current compiler-integration revision tested on 2026-08-26 in
`America/New_York`. The provider-only acceptance result below is the preceding
2026-08-25 snapshot.

## 2026-08-26 Lean compiler integration repair

The compiler preflight now validates Lean's actual native compilation path.
For the bundled Lean toolchain, Proof Assistant invokes `leanc` with `LEAN_CC`
unset; a user-supplied external compiler remains an explicit override only
after it passes both a native standard-header probe and a Lean-header probe.
Cache schema 3 records the discovered compiler separately from the optional
override and migrates older bundled-clang records to the safe unset state.

| Gate | Result |
|---|---|
| focused compiler/cache/CLI tests | **68 passed in 3.33 seconds** |
| complete automated suite | **564 passed in 119.68 seconds** |
| Ruff lint | **passed** |
| typing policy | **passed**; 94/98 explicit `Any` uses and 11 boundary modules `Any`-free |
| strict mypy | **passed**; 72 source files |
| Python `compileall` | **passed** |
| source distribution and wheel build | **passed** with `uv build` |
| real Lean compiler preflight | **passed in 0.63 seconds** with Lean 4.28.0 and `LEAN_CC` unset |
| live cache migration and doctor | **passed**; schema 3 stores `lean_cc: null` for the bundled toolchain |
| original `laplacians` project | **passed**; a cold schema-3 dependency depot completed `lake update` and `lake build`, then a project-scoped warm rerun reused it successfully |
| platform CI contract | pinned Lean setup and compiler preflight added to both Ubuntu and macOS jobs |

This repair validation made no provider request and did not run a
provider-backed manuscript proof.

## 2026-08-25 provider revision result

The current source passed the supported installer in a disposable location
outside Dropbox. That run installed the development/test dependencies with
Python 3.13 and `uv`, compiled and executed a real native test program, and ran
the complete automated suite. This pass deliberately used fake provider
process/HTTP/keyring boundaries: it consumed no provider subscription quota and
made no OpenAI, Anthropic, or Gemini API request.

| Gate | Result |
|---|---|
| supported disposable installer | **passed**; **551 tests passed in 162.10 seconds** |
| complete automated suite | **passed independently**; **551 tests passed in 162.36 seconds** |
| provider security tests | **32 passed**; included in the 551-test suite |
| provider TUI tests | **6 passed**; included in the 551-test suite |
| Ruff lint and format | **passed** |
| strict mypy | **passed**; 72 source files |
| Python `compileall` | **passed** |
| `git diff --check` | **passed** |
| native compiler preflight | **passed**; `/usr/bin/clang` compiled and executed the probe |
| provider traffic | **none**; no real CLI model turn and no provider API request |
| RepoProver checkout | exact commit recorded; upstream untouched |
| upstream RepoProver PR | **NOT CREATED** |

This validation does not assign a final release commit SHA. A final 0.1.0 wheel
and source distribution were built outside the repository and checked as
described below. The real Lean calibration, provider-backed manuscript run,
and cross-platform suite retained below were collected on the earlier
2026-08-23 snapshot. They remain useful
regression evidence, but they are not represented as reruns of the new provider
revision.

## Multi-provider feature scope

The 2026-08-25 suite covers the new provider-neutral AI layer:

- Codex CLI, Claude Code CLI, GitHub Copilot CLI, OpenAI API, Anthropic API,
  and Gemini API driver contracts;
- machine-scoped, revision-checked provider settings with no secret fields;
- live-account model catalogs bound to setup readiness and automatic task
  policy, with unavailable explicit models rejected before persistence;
- API credentials resolved only from provider environment variables or the OS
  keyring, with one-shot/redacted credential submissions;
- minimal provider-specific subprocess environments that keep API keys out of
  CLI setup, discovery, execution, entitlement checks, and npm installation;
- native CLI authentication checks without reading provider auth files;
- live-versus-curated model-catalog provenance and exact model/difficulty
  validation;
- an automatic, non-billable Codex/Claude readiness path and a separate
  explicit-consent Copilot entitlement probe that is never run by ordinary
  startup/status inspection;
- reviewed, allowlisted, user-local CLI installation plans with executable
  identity and PATH handling;
- task-class model/difficulty policy for proof, sketch, maintenance,
  clarification, diagnosis, review, duplicate proof, and reporting;
- provider-neutral execution through the global AI controller and the common
  allowlisted RepoProver tool host, with Lean/build admission preserved;
- Codex external-capability isolation, ephemeral restricted Claude/Copilot MCP
  execution, and provider-native API function-calling loops;
- first-run primary-driver setup, machine-wide **Settings → AI Providers**,
  copyable sanitized status, model/auth/install controls, and consent-first
  credential/account actions; and
- global **F2 Main menu** and **F3 Settings** navigation, including cancel-first
  modal behavior and observer-only detachment from a running job.

The provider tests substitute deterministic fakes at every external account
boundary. Therefore the suite validates request construction, isolation,
redaction, consent, state transitions, and error handling without claiming that
the current machine is entitled to any particular provider model.

## Earlier 2026-08-23 acceptance evidence

The following sections describe the preceding 2026-08-23 acceptance snapshot.
Counts and artifacts in those sections are historical unless explicitly marked
as part of the 2026-08-25 provider run above.

## Python 3.13 roadmap acceptance

At that earlier snapshot, strict mypy checked all 64 package source files in
CI. A companion policy gate capped the remaining 98 explicit AST-level `Any`
uses and required 11
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

The August 23 snapshot addressed three failures found on the real `lapl`
project and in release testing:

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

### 2026-08-25 installer and package

The supported installer ran in a disposable location outside Dropbox with
Python 3.13.15 and uv 0.9.26. It installed the editable development package and
test dependencies, compiled and executed its native C preflight with
`/usr/bin/clang`, initialized the managed cache, and completed all 551 tests in
162.10 seconds. Ruff lint/format, strict mypy over 72 source files, Python
`compileall`, and `git diff --check` also passed. A separate final full-suite
run completed all 551 tests in 162.36 seconds.

No real AI turn or API request was part of this installer acceptance. No new
final release commit SHA is assigned by this report; the validated test counts
and artifact hashes identify the tested state without describing it as
uncommitted.

The final wheel and source distribution were built with `uv` outside the
repository. The wheel was installed into a fresh external CPython 3.13.15
environment alongside the exact RepoProver checkout. Distribution version,
package imports (including AI providers, TUI settings, and Lean resources), AI
setup help, and the real compiler compile-and-execute command all passed. The
installed package imported from `site-packages`, not an editable source tree.
Archive filename and content scans found no credentials, private keys,
environments, caches, or machine-private files.

| Artifact | SHA-256 |
|---|---|
| `proof_assistant-0.1.0-py3-none-any.whl` | `919341bf0cf50e5bd84048de5cb9ff1932927c86fdeab37af7834734130c30a7` |
| `proof_assistant-0.1.0.tar.gz` | `ded691c0acf8e0549ed6056c3849a00e43d00fc0be8f9c6e607a24f600cc4e6c` |

### Earlier 2026-08-23 package acceptance

The earlier supported installer was run with:

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

The disposable build and environment were deleted after the checks. At that
acceptance point, the live installation was:

```text
source:      /Users/vui1/src/proof-assistant
environment: /Users/vui1/.venvs/proof-assistant
command:     /Users/vui1/.local/bin/proof-assistant
cache:       /Users/vui1/.cache/repoprover-codex
```

All four locations are outside Dropbox. The historical cache path remains
intentional so existing Mathlib artifacts are shared rather than duplicated.

## Tested environment

### 2026-08-25 provider pass

| Component | Tested value |
|---|---|
| OS | macOS 12.7.6 (21H1320), x86_64 |
| Python | CPython 3.13.15 |
| uv | 0.9.26 |
| Codex CLI | 0.149.1; version inspection only, no model turn |
| Lean | 4.28.0, commit `7e01a1bf5c70fc6167d49c345d3bf80596e9a79b` |
| Lake | 5.0.0-src+7e01a1b |
| RepoProver | commit `386adba3df572cb71df534add2c764e071898a2e` |
| native compiler | `/usr/bin/clang`; real compile-and-execute preflight passed |

The RepoProver integration version was the exact commit above and remained
untouched. No upstream RepoProver pull request was created.

### Earlier 2026-08-23 manuscript environment

| Component | Earlier tested value |
|---|---|
| Codex CLI | 0.149.0 |
| Git | 2.37.1 (Apple Git-137.1) |
| Mathlib | v4.28.0, commit `8f9d9cff6bd728b17a24e163c9402775d9e6a365` |
| native compiler | `/usr/bin/clang`, Apple clang 14.0.0 |
| Textual / Rich | 1.0.0 / 14.3.4 |
| psutil / NetworkX | 7.2.2 / 3.6.1 |

These dependency versions accompany the earlier real Lean/manuscript evidence;
they are not a claim that the provider revision repeated that manuscript run.

## Storage and repository hygiene

At the earlier 2026-08-23 final cache check:

```text
managed cache:          9.88 GiB
cache limit:           16.00 GiB
filesystem free:      118.41 GiB
dependency depots:      7.06 GiB
isolated builds:        2.43 GiB
active reservations:    0.00 GiB
```

For that earlier publication, the repository was checked for private-key
material, credentials, tokens, `.env`/`auth.json` files, machine-private
configuration, virtual environments, package builds, caches, and temporary
test output.
Generated repository-local caches were removed, ignored-path rules were
verified, and remote history was fetched before the push. No command in this
work may push to or open a pull request against `facebookresearch/repoprover`.
