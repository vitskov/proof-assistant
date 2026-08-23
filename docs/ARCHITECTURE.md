# Architecture and security

Proof Assistant separates interaction from project and proof authority. The TUI
can be replaced without migrating verification projects or changing the
incremental engine.

```text
Textual TUI
    | typed commands, immutable view models, progress events
    v
UI-neutral workflow service
    | typed project/source/task/change-plan contracts
    v
backend project management
    | catalog reconciliation, occupancy, migration, destination policy
    v
persistent incremental verifier
    | snapshots, SQLite, graphs, Codex workers, Lean certification
    v
Lean kernel
```

## Component silos

### `proof_assistant.tui`

Owns Textual screens, widgets, Rich syntax rendering, key bindings, and
background worker lifecycles. It may call workflow interfaces and render typed
results. It must not write SQLite, copy manuscript trees, calculate graph
closure, invoke Codex directly, or decide certification state.
Informational values are rendered through selectable read-only surfaces;
nonselectable syntax/Markdown renderers require an exact copyable source twin.

### `proof_assistant.workflow`

Owns the UI-neutral application state machine. Immutable contracts live in
`workflow.contracts`; `workflow.service.ProofAssistantWorkflow` implements
`default_task_text`, `inspect_source`, `inspect_project_destination`,
`list_projects`, `create_project`, `select_project_main_file`, `resume_project`,
`plan_changes`, and `confirm_and_verify`. `CancellationFlag` and
`StaleChangePlanError` make cancellation and stale confirmation explicit. The
service maps persisted backend state to screens but contains no Textual imports.

### `proof_assistant.workspace`

Contains the distinct `workspace.management.ProjectManager` backend component.
It owns project discovery, catalog reconciliation (default
`$HOME/.config/proof-assistant/projects.json`), destination occupancy and
Dropbox/path policy, legacy main-file migration, stable source observation,
staged copying, and complete inventories. It does not prove claims or render
UI. The TUI is prohibited from importing this package and can access project
management only through workflow contracts.

### `proof_assistant.presentation`

Builds source-location, excerpt, affected-tree, clarification, and findings view
models. An optional isolated Codex presenter may improve prose only within a
strict output schema. Deterministic facts remain host-owned, and a deterministic
fallback is always available.

### `proof_assistant.incremental`

Owns immutable source snapshots, the LaTeX object index, manuscript/Lean graphs,
state transitions, invalidation, scheduling, verification worktrees,
clarifications, certificates, and reports. It has no Textual dependency.

## Strict boundary contracts

The interfaces exchange validated, serializable data rather than paths with
implicit meaning or mutable UI objects.

- `SourceInspection` contains the resolved external folder and every candidate
  LaTeX root. The TUI must use it instead of implementing its own file
  discovery.
- `NewProjectRequest` and the persisted project configuration contain a validated,
  source-relative `main_file` in addition to resolved source/project locations
  and a project-owned task. There is no backend state in which a new project
  has an implicit root. Project creation rejects overlapping paths and managed
  Dropbox destinations.
- `ProjectDestinationInspection` is the backend's non-mutating preflight for the
  resolved default or explicit destination. `ProjectCatalogEntry` uses the same
  classifier and is tagged `RESUMABLE`, `NEEDS_MAIN_FILE`, `INCOMPLETE`, or
  `OCCUPIED`; impossible tag/payload combinations are rejected by the contract.
  Occupied paths remain visible and are never deleted to make creation succeed.
- `ProjectSummary` and `ChangeImpactPlan` expose the persisted main file and
  its ordered, resolved input closure. A UI therefore renders the exact backend
  interpretation rather than reconstructing inclusion topology.
- `SourceInventory` contains the complete filtered path/type/size/hash view.
  Two equal inventories plus a verified staged copy are required for stability.
- `ChangeImpactPlan` binds before/after inventory hashes, source diff, changed
  claims, descendant closure, reusable certificates, superseded questions, and
  project generation. Confirmation fails if the source or generation changed.
- `ProgressEvent` values are read-only observations. They cannot mutate
  state or imply success. Their initial context identifies the main file and
  all resolved inputs; subsequent events distinguish source observation,
  import, indexing, impact analysis, cache setup, Lean build/extraction, proof
  batches, independent certification, and reporting.
- `VerificationResult` uses explicit outcome categories and evidence paths. An
  agent completion marker is never a certificate.
- `ReportDocument` contains the backend-validated canonical report path and
  UTF-8 Markdown. `load_report` classifies the managed project without migration
  or mutation, rejects paths that escape the project root, and normalizes load
  errors. The TUI renders this document and never reads project files itself.
- `ClarificationView` binds an exact persisted question to source path/span,
  quoted text, diagnostics, possible resolutions, and blocked claims.
- `ResumeDecision` is derived from project state, open questions, source
  stability, run status, and writer ownership—not from the last visible screen.

These contracts make stale UI actions fail closed and let a future web or
desktop front end reuse the backend unchanged.

## Main-file and input-closure contract

The external folder is a source container, not the manuscript definition. One
normalized, source-relative `main_file` is mandatory at every project-creation,
workflow, index, change-plan, resume, and verification boundary. Source
snapshots preserve the complete filtered container, while their interpretation
always comes from this persisted root. The backend validates that the root
names a discovered `.tex` or `.ltx` file, then recursively resolves literal
`\input` and `\include` commands. Plain forms check the including directory and
source root and reject distinct dual matches; import-package forms use
including-file semantics.

Resolution is deterministic and cycle-safe. Absolute paths, directory escape,
missing files, and dynamic include expressions that cannot be resolved are
errors. The resulting ordered closure is persisted/exposed as `input_files`.
The LaTeX object index scans only that closure, while stable source observation
may still inventory the complete filtered container so changes cannot be
accepted from a partial multi-file save. Alternate document roots and orphaned
drafts never contribute claims, labels, or duplicate-label failures.

The TUI has no permission to infer this closure. It displays the candidates
returned by `inspect_source`, supplies the selected `main_file` to
`create_project`, and renders the `main_file`/`input_files` returned by the
backend. A one-file folder is an unambiguous UI shortcut, not an optional
backend field. For an ambiguous legacy project, it renders candidates from a
`NEEDS_MAIN_FILE` catalog entry and calls `select_project_main_file`; only the
backend validates and persists that choice.

## Verification authority

```text
author source
    |
    v
deterministic host: snapshot -> index -> graph -> scheduler -> certificate DB
                                      |                         ^
                                      v                         |
                              isolated Codex/RepoProver -> Lean kernel
```

AI performs semantic interpretation, correspondence proposals, diagnostics,
proof search, and optionally user-facing clarification phrasing. Host code
controls source identity, graph updates, invalidation, scheduling, state
transitions, provenance, and presentation facts. Lean is the proof authority.

After a proposal, the host merges only assigned claim modules, runs an
independent `lake build`, inspects elaborated declarations through Lean's
environment API, hashes structural types and values, records proof-term
dependencies and axioms, and only then changes a claim to `CERTIFIED`.

## Codex and RepoProver boundary

```text
RepoProver agent tools
    | validated function schemas and handlers
    v
Proof Assistant adapter
    | client-defined dynamic tools over app-server JSONL
    v
isolated Codex app-server -> existing Codex CLI login
```

RepoProver remains the control plane for Lean, Git, files, shell operations,
and Mathlib search. Proof workers receive narrow reads and validated mutation
requests. They cannot directly write SQLite, source snapshots, graph exports,
certificate rows, the managed manuscript, or host aggregate modules.

Before a turn, child-only configuration disables apps, plugins, bundled/local
skills, and configured MCP servers. Startup verifies the effective inventory
and fails closed if an external capability remains. The user's normal Codex
configuration is not modified.

Authentication stays inside Codex. Proof Assistant does not read
`~/.codex/auth.json`, print tokens, or convert a login into an API key. The
system is not offline: task context and tool results needed for a turn are
processed through the authenticated Codex service.

## Persistence and recovery

SQLite uses foreign keys, WAL, full synchronous commits, and explicit
transactions. A project POSIX lock serializes mutation; read-only status remains
available through safe WAL reads. Canonical exports use atomic rename. Bare-Git
source commits and ordinary Lean Git history remain recoverable after an
interruption.

Safe cancellation is observed only at host-controlled boundaries. Worker
candidates are either discarded before merge or carried through the merged
project's independent build and kernel certification as one indivisible round.
Already-issued certificates remain durable; an in-flight `PROVING` marker has
no authority and is transactionally reset to retryable `INVALIDATED`. The run
becomes `INTERRUPTED`, its cancellation facts are persisted, and a `finally`
boundary removes every temporary batch worktree, including worker-failure
paths. `WorkflowSnapshot.cancellation` carries a typed `CancellationReport`
with the run ID, preserved certificate IDs, retryable claim IDs, detail, and
worktree-cleanup result; the TUI cannot infer or overstate those facts.

The next invocation applies the same recovery to an abandoned `RUNNING` row and
every orphaned `PROVING` claim, including state left by older releases. An open
question is superseded only when
its source object changes or the user explicitly resolves/dismisses it.
Reopening the TUI derives the correct findings, clarification, change-review,
recovery, or read-only progress screen from this state.

## Parallelism and storage

At most two independent proof processes run concurrently. Each has its own
Codex process, RepoProver Lean pool, Git worktree, and isolated root build. The
host merges results deterministically and performs fresh certification.

Compatible Mathlib/REPL dependencies share the established depot under
`$HOME/.cache/repoprover-codex`. Projects and ephemeral worktrees have isolated
root builds. Reservations, OS leases, a coarse SQLite index, atomic quarantine,
and one deadline per GC pass protect active data and bound cleanup work. See
[Cache and storage](CACHE_AND_STORAGE.md).
