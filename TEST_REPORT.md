# Test report — Proof Assistant 0.1.0

Tested on 2026-08-23 in `America/New_York`.

## Release result

The current source passed the complete automated suite, the supported local
installer, a clean Python 3.13 wheel installation, real Lean memory
calibration, and a provider-backed two-claim manuscript verification.

| Gate | Result |
|---|---|
| complete automated suite | **377 passed** in 117.15 seconds |
| supported installer | **passed**; compiler compile/run and 377 tests |
| Ruff lint and format | **passed** |
| Python `compileall` | **passed** |
| `git diff --check` | **passed** |
| clean Python 3.13 wheel | **passed** |
| real Lean calibration | **passed**; no Codex traffic |
| real manuscript verification | **verified**, 2/2 current certificates |
| RepoProver checkout | exact commit, clean and unchanged |
| upstream RepoProver PR | **NOT CREATED** |

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
  stores, including early returns and failures.

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
initialized the cache, and ran all 377 tests in 117.84 seconds. The compiler
check is mandatory in the installer and records `LEAN_CC` when Lean's bundled
compiler is unusable.

An sdist and universal wheel were built outside the repository with `uv`. The
wheel was installed into a disposable Python 3.13.15 environment and checked
for:

- distribution/import version 0.1.0;
- `proof-assistant` and deprecated 0.1 `repoprover-codex` entry points;
- packaged `proof_assistant.lean/DependencyExtractor.lean`;
- public `ProofAssistantWorkflow` and `ConcurrencyResourcesScreen` imports;
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
managed cache:          9.64 GiB
cache limit:           16.00 GiB
filesystem free:      119.61 GiB
dependency depots:      7.06 GiB
isolated builds:        2.19 GiB
active reservations:    0.00 GiB
```

Before publication the repository is checked for private-key material,
credentials, tokens, `.env`/`auth.json` files, machine-private configuration,
virtual environments, package builds, caches, and temporary test output.
Generated repository-local caches are removed, ignored-path rules are verified,
and the remote history is fetched before any push. No command in this work may
push to or open a pull request against `facebookresearch/repoprover`.
