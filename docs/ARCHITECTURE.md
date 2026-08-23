# Architecture and security

Proof Assistant separates interaction from project and proof authority. The TUI
can be replaced without migrating verification projects or changing the
incremental engine.

```text
Textual TUI
    | typed commands, immutable view models, progress events
    v
UI-neutral workflow service
    | project/source/task/change-plan contracts
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

### `proof_assistant.workflow`

Owns the UI-neutral application state machine. Immutable contracts live in
`workflow.contracts`; `workflow.service.ProofAssistantWorkflow` implements
`default_task_text`, `list_projects`, `create_project`, `resume_project`,
`plan_changes`, and `confirm_and_verify`. `CancellationFlag` and
`StaleChangePlanError` make cancellation and stale confirmation explicit. The
service maps persisted backend state to screens but contains no Textual imports.

### `proof_assistant.workspace`

Owns the project catalog (default
`$HOME/.config/proof-assistant/projects.json`), Dropbox/path policy, stable source observation,
staged copying, complete inventories, and project-owned task management. It
does not prove claims or directly render UI.

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

- `ProjectSpec` contains resolved source/project locations and a validated
  project-owned task. Project creation rejects overlapping paths and managed
  Dropbox destinations.
- `SourceInventory` contains the complete filtered path/type/size/hash view.
  Two equal inventories plus a verified staged copy are required for stability.
- `ChangeImpactPlan` binds before/after inventory hashes, source diff, changed
  claims, descendant closure, reusable certificates, superseded questions, and
  project generation. Confirmation fails if the source or generation changed.
- `VerificationProgress` events are read-only observations. They cannot mutate
  state or imply success.
- `VerificationResult` uses explicit outcome categories and evidence paths. An
  agent completion marker is never a certificate.
- `ClarificationView` binds an exact persisted question to source path/span,
  quoted text, diagnostics, possible resolutions, and blocked claims.
- `ResumeDecision` is derived from project state, open questions, source
  stability, run status, and writer ownership—not from the last visible screen.

These contracts make stale UI actions fail closed and let a future web or
desktop front end reuse the backend unchanged.

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

The next invocation marks an abandoned `RUNNING` row `INTERRUPTED`. An open
question is superseded only when its source object changes or the user
explicitly resolves/dismisses it. Reopening the TUI derives the correct findings,
clarification, change-review, recovery, or read-only progress screen from this
state.

## Parallelism and storage

At most two independent proof processes run concurrently. Each has its own
Codex process, RepoProver Lean pool, Git worktree, and isolated root build. The
host merges results deterministically and performs fresh certification.

Compatible Mathlib/REPL dependencies share the established depot under
`$HOME/.cache/repoprover-codex`. Projects and ephemeral worktrees have isolated
root builds. Reservations, OS leases, a coarse SQLite index, atomic quarantine,
and one deadline per GC pass protect active data and bound cleanup work. See
[Cache and storage](CACHE_AND_STORAGE.md).
