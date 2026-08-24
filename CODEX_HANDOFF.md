# CODEX HANDOFF — Proof Assistant

This file is a compact maintainer handoff. The maintained user and architecture
documentation under `docs/` is authoritative.

## Mission and identity

Proof Assistant is a persistent, incremental formal referee for multi-file
LaTeX manuscripts. It combines a replaceable Textual interface, UI-neutral
workflow/project services, isolated Codex/RepoProver proof workers, and
independent Lean certification.

Current identity:

```text
product             Proof Assistant
distribution        proof-assistant
import package       proof_assistant
primary command      proof-assistant
deprecated alias     repoprover-codex (0.1 migration window)
repository           vitskov/proof-assistant
version              0.1.0
source               $HOME/src/proof-assistant
environment          $HOME/.venvs/proof-assistant
new projects         $HOME/proof-assistant/<project-name>
cache                $HOME/.cache/repoprover-codex
```

The cache name intentionally does not follow the product rename. Reusing it
prevents duplication of the shared multi-gigabyte Mathlib dependency depot.

## Non-negotiable rules

- Use Python 3.13 and `uv` whenever feasible.
- Never place Python environments, managed projects, or Lean/Lake/Mathlib
  caches in Dropbox.
- External manuscript sources may be in Dropbox; warn and use stable staged
  import.
- Every installer must compile and execute a native test program.
- Authentication stays inside Codex CLI. Never read/print `auth.json` or turn
  its credential into an API key.
- Verification Codex children start with MCP servers, apps, plugins, bundled
  skills, and local skills disabled, then verify this fail-closed.
- Never push to, open a pull request against, or create an issue in
  `facebookresearch/repoprover`.
- Never publish Proof Assistant without explicit user authorization.

## User workflow contract

Bare `proof-assistant` (or `proof-assistant tui`) launches the TUI.

For a new project it selects an external source folder, creates a managed
project outside Dropbox, and creates a project-owned `VERIFY.yaml`. The user
chooses the default task or customizes seeded instructions in the built-in text
editor. There is no user-facing external task-file workflow.

Folder selection is also a workflow-service boundary. The TUI renders immutable
`ManuscriptFolderListing` values and must not enumerate the filesystem itself.
The backend persists the most recently explicitly selected folder in
`$HOME/.config/proof-assistant/preferences.json` (or a safe XDG equivalent),
with home as the fallback. The preference file must remain outside Dropbox and
managed projects; the selected manuscript source itself may be in Dropbox.

If verification needs clarification, show the actual input file and exact
highlighted LaTeX span. The user edits the external source. Detect all stable
multi-file changes, stage/re-hash them, calculate proof-tree impact, and require
explicit confirmation before the next iteration. Revalidate the source
inventory at confirmation time.

Resume routing is based on persistent backend state:

- unchanged open question -> existing clarification screen;
- stable source change -> change review;
- completed run -> findings;
- interrupted run -> recovery;
- provider/Lean failure -> diagnostics; and
- active detached verification worker -> attached replayable progress;
- legacy session-lock-only verifier -> attached coarse read-only progress.

The TUI is never the verification worker. It submits, observes by durable event
cursor, and requests cooperative cancellation through the workflow contract.
The detached backend worker holds the lifetime and project-mutation leases, so
a TUI exit or SSH disconnect does not stop a run. The bounded default is two
independent proof-batch agents; `jobs=1` remains the explicit sequential
override.

Project removal is also backend-owned. The welcome-screen TUI may request
`inspect_project_deletion` and, after exact-name confirmation, `delete_project`;
it must never move or remove directories itself. Deletion applies only to a
validated resumable managed project, is refused under an active writer lock,
and moves the project atomically to the platform recovery area before catalog
reconciliation. The authoritative manuscript source remains untouched.

## Architectural boundary

```text
proof_assistant.tui
        | typed commands/view models/events only
        v
workflow + workspace + presentation services
        | validated specs, inventories, plans, results
        v
proof_assistant.incremental
        | Git/SQLite/graphs/Codex workers/certificates
        v
Lean kernel
```

The backend must not import Textual. The TUI must not copy source, write
SQLite, calculate invalidation, invoke Codex directly, or assign certificate
state. Filesystem notifications only wake the observer; stable full inventories
and staged re-hashing establish source identity.

Detached-worker launch arguments carry the launcher's exact catalog and
machine-settings paths. A legacy/direct hidden worker falls back to a
project-local worker catalog, never the interactive user's production catalog.
Secrets remain in the child environment and are never serialized into the
durable launch command.

Codex may improve clarification wording under a validated schema, but the host
owns the claim, source path/span, quotation, diagnostics, affected graph, and
possible-resolution facts. Invalid output falls back deterministically.

## Concurrency contract

- AI turns, Lean work, and Lake builds use three independent machine-global
  admission namespaces backed by expiring, heartbeat-renewed SQLite leases.
- Machine policy lives in `$HOME/.config/proof-assistant/settings.yaml` (or the
  XDG equivalent). The service accepts a future project overlay, but no
  project-specific concurrency settings are currently enabled.
- The TUI is a client of the workflow settings API. **Concurrency / Resources**
  exposes configured and effective values, telemetry, warnings, calibration,
  and reset actions; **Legacy settings** contains logical batch fan-out and
  other compatibility knobs.
- Manual reductions drain safely without killing admitted work. Detached jobs
  reload machine settings during the run, and stale refresh failures cannot
  restore old limits.
- Automatic Lean capacity is CPU- and RAM-bounded. Project import/RSS profiles
  are exact provenance, while the shared limit uses the most conservative fresh
  p95 across profiles for the same machine allocation and never drops below the
  uncalibrated memory fallback.
- Yellow memory pressure allows at most one full build machine-wide so it
  cannot create either build storms or a permanent no-progress state. Red and
  emergency pressure block new builds.
- Every managed Codex turn—including reviewers and diagnostics—shares the AI
  controller. Every dynamic `lean_check` and recognized `lake build`, plus host
  bootstrap, extraction, merge, and certification work, passes through its
  corresponding controller.
- `jobs=2` is the default logical multi-agent fan-out. Logical agents never
  override the current AI, Lean, or build admission limits.

## Managed-cache ownership contract

- Lean version discovery invokes `lean --version` directly. It must never call
  `lake env`, resolve dependencies, or materialize `.lake` before cache attach.
- Every isolated build target has an atomic
  `.proof-assistant-build.json` marker bound to the exact resolved project and
  build-target identity.
- A nonempty packages directory may be replaced by the shared dependency depot
  only when that marker proves Proof Assistant ownership. Imported, unmarked,
  mismatched, and explicitly unowned package trees fail closed.
- Pre-marker Proof Assistant projects are recognized only through their exact
  deterministic managed-project identity. Reattaching a target cannot escalate
  an unowned marker.

## Verification invariants

- The author-facing source and managed project cannot contain one another.
- Immutable filtered Git snapshots identify source input.
- Stable manuscript objects and explicit/semantic edges form the manuscript
  graph; elaborated Lean expressions form the Lean graph.
- Changed statements invalidate the complete reverse-dependent closure and no
  unrelated branch.
- Only ready claims whose dependencies are certified may run.
- Parallel workers modify assigned claim modules in isolated worktrees.
- The host merges deterministically, independently builds, extracts structural
  type/value/dependency/axiom evidence, and only then certifies.
- Inconclusive, ambiguous, technical failure, suspected false, and
  kernel-checked counterexample remain distinct states.
- Questions and completed evidence survive process interruption without model
  conversation state.
- Failure incidents, run scope, end states, and dependency edges are immutable
  per-run evidence. The backend chooses deterministic blocker paths and emits a
  tree for acyclic graphs; only actual cycles use the component-graph fallback.
- A TUI may color failure states, but it must also show textual status tags and
  a selectable exact-reason/copyable-outline twin. It never reconstructs the
  failure graph from SQLite.

## Validation before handoff/publication

```bash
cd "$HOME/src/proof-assistant"
scripts/install-dev.sh
proof-assistant compiler-check
proof-assistant cache doctor
proof-assistant doctor
proof-assistant models
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
git diff --check
```

Also run a fresh Python 3.13 wheel smoke outside Dropbox and one small real
manuscript/TUI feedback-loop acceptance. Validate default/custom task, Dropbox
warning, exact multi-file clarification location, stable change review, stale
confirmation rejection, resume behavior, certificate reuse, and no duplicate
cache depot.

Record exact Python/uv/Codex/Lean/Lake/Mathlib/RepoProver/compiler versions and
all actual evidence in `TEST_REPORT.md`. Never claim a gate that was not run.
