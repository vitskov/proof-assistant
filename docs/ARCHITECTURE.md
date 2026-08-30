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
    | snapshots, SQLite, graphs, provider-neutral AI workers, Lean certification
    v
Lean kernel
```

## Component silos

### `proof_assistant.tui`

Owns Textual screens, widgets, Rich syntax rendering, key bindings, and
background worker lifecycles. It may call workflow interfaces and render typed
results. It must not write SQLite, copy manuscript trees, calculate graph
closure, invoke an AI provider directly, or decide certification state.
Informational values are rendered through selectable read-only surfaces;
nonselectable syntax/Markdown renderers require an exact copyable source twin.

### `proof_assistant.workflow`

Owns the UI-neutral application state machine. Immutable contracts live in
`workflow.contracts`; `workflow.service.ProofAssistantWorkflow` implements
`default_task_text`, `browse_manuscript_folders`,
`remember_manuscript_folder`, `inspect_source`,
`inspect_project_destination`, `list_projects`, `create_project`,
`select_project_main_file`, `resume_project`, `plan_changes`, and
`confirm_and_verify`. It also owns the TUI-facing, sanitized provider operations
(`get_ai_setup`, revision-checked settings updates, install-plan review,
credential submission/deletion, and explicit account verification).
`CancellationFlag` and
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
models. An optional isolated AI presenter may improve prose only within a
strict output schema. Deterministic facts remain host-owned, and a deterministic
fallback is always available.

### `proof_assistant.ai`

Owns provider-neutral contracts, machine-scoped provider configuration,
credential indirection, executable/account inspection, model-catalog
provenance, task/model policy, setup plans, and execution adapters. It supports
Codex, Claude Code, and Copilot CLIs plus OpenAI, Anthropic, and Gemini APIs.
The module does not decide claim state or certification.

Provider settings contain no secret values. API credentials are resolved just
in time from the selected environment variable or OS keyring. CLI sessions use
their native account login without Proof Assistant reading auth files.

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
- `ManuscriptFolderListing` contains a backend-resolved current directory,
  parent, home directory, ordered/deduplicated child directories, and the reason
  for its initial location. The terminal picker traverses only these immutable
  listings; it never enumerates directories or reads configuration itself.
  `remember_manuscript_folder` persists a choice only after the user's explicit
  **Select** action. This advisory preference is machine-local, stored outside
  managed projects and Dropbox, and falls back to home if absent or invalid.
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
- `ProjectDeletionInspection` is the backend's non-mutating, lock-aware
  authorization for recoverably moving exactly one validated `RESUMABLE`
  project. `delete_project` repeats that preflight while holding the exclusive
  project lock, reserves a collision-safe recovery container, atomically moves
  the managed project, and only then removes its catalog entry. Catalog failure
  triggers an atomic rollback. Project/source/recovery overlap, Dropbox-managed
  paths, active writers, and non-project paths fail closed. The TUI only invokes
  these two methods; it has no filesystem-deletion authority.
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
- `FailureDependencyReport` is an immutable, run-scoped explanation containing
  ordered targets, frozen claim nodes and dependency edges, structured failure
  incidents, exact evidence artifacts, canonical blocker paths, and a
  deterministic primary incident. `load_failure_report` constructs it inside
  the backend. The TUI must not query SQLite, infer graph reachability, choose a
  blocker, or recalculate strongly connected components.
- `ClarificationView` binds an exact persisted question to source path/span,
  quoted text, diagnostics, possible resolutions, and blocked claims.
- `ResumeDecision` is derived from project state, open questions, source
  stability, run status, and writer ownership—not from the last visible screen.

These contracts make stale UI actions fail closed and let a future web or
desktop front end reuse the backend unchanged.

## Detached verification-job boundary

The Textual application is only a client. It performs short typed
`start_verification`, cursor-based `observe_verification`, and
`request_verification_cancel` calls; it never executes the long verifier or
holds the project mutation lease. The backend atomically creates or attaches to
one durable job and transfers a lifetime worker lease to a detached Python 3.13
process. That worker holds the project lock while verification mutates Git,
SQLite, reports, or certificates.

Job identity, request fingerprint, settings, state, heartbeat, replayable
progress events, and cancellation intent are persisted under `.repoprover/jobs`.
The lifetime lease and one-active-job transaction prevent two workers from
mutating the same project. Closing a TUI only stops its observer. A replacement
client resumes from its last event sequence; cancellation remains cooperative
and durable. A legacy session-lock-only run is exposed as coarse,
non-cancellable active verification rather than as a project owned by another
TUI.

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
                              isolated AI/RepoProver -> Lean kernel
```

AI performs semantic interpretation, correspondence proposals, diagnostics,
proof search, and optionally user-facing clarification phrasing. Host code
controls source identity, graph updates, invalidation, scheduling, state
transitions, provenance, and presentation facts. Lean is the proof authority.

After a proposal, the host merges only assigned claim modules, runs an
independent `lake build`, inspects elaborated declarations through Lean's
environment API, hashes structural types and values, records proof-term
dependencies and axioms, and only then changes a claim to `CERTIFIED`.

## AI providers and RepoProver boundary

```text
machine provider policy + credential reference
    |
    v
optional project provider + per-role model/difficulty matrix
    |
    v
Proof Assistant AI adapter -> isolated CLI or provider-native API tool loop
    |                                      |
    +------ allowlisted tool calls --------+
                       |
                       v
          RepoProver schemas/handlers -> Lean and Lake admission
```

Proof Assistant reuses tested RepoProver prompts, dynamic-tool schemas, and tool
handlers where appropriate. It does not adopt RepoProver's legacy raw API-key
configuration, static model table, OpenAI-compatible provider client, retry
policy, or research-deployment concurrency as an authoritative provider layer.
Provider selection, credential indirection, catalog provenance, task policy,
execution isolation, and admission have one source of truth in
`proof_assistant.ai`.

Machine policy owns provider installation, authentication, credentials, role
defaults, and resource admission. A managed project may persist only one public
provider plus a model/difficulty assignment for every `TaskKind` in
`.repoprover/verification-settings.json`. The workflow service validates that
override against the current machine provider setup and approved catalog, then
merges it with machine defaults when a verification is submitted. The resolved
role matrix is copied into the durable job row and request fingerprint;
clarification and proof execution read from that frozen matrix. Later edits
affect only future jobs. Version-1 project files migrate their former scalar
choice as the proof role and receive provider-aware defaults for the remaining
roles. The project file is revisioned, locked, atomically replaced, and rejects
unknown or secret-shaped fields.

RepoProver tools remain the control plane below that boundary for Lean, Git,
files, shell operations, and Mathlib search. Proof workers receive narrow reads
and validated mutation requests. They cannot directly write SQLite, source
snapshots, graph exports, certificate rows, the managed manuscript, or host
aggregate modules.

Codex uses the established isolated app-server adapter: child-only
configuration disables apps, plugins, bundled/local skills, and configured MCP
servers, then startup verifies the effective inventory and fails closed if an
external capability remains. Claude and Copilot use ephemeral mode-`0600`
prompt/tool/MCP files and are restricted to the supplied Proof Assistant MCP
tools while their general shell/file mutation surface is disabled. Direct API
drivers use provider-native function calling. In every case, dynamic tool
requests return to the same allowlisted host and pass through Lean/build
admission where applicable.

CLI authentication stays inside the native provider CLI. Proof Assistant does
not read `~/.codex/auth.json` or any Claude/Copilot credential store, print
tokens, or convert a subscription login into an API key. API keys come only
from the configured provider environment variable or OS keyring and never
enter provider settings, project state, an argument vector, or a log.

Codex and Claude have non-billable status checks. Automatic Copilot inspection
does not make a request; its optional tiny no-tools entitlement probe is a
separate explicit-consent operation. Catalogs distinguish live account results
from contract-approved curated fallbacks. See [AI providers and first-time
setup](AI_PROVIDERS.md).

## Persistence and recovery

SQLite uses foreign keys, WAL, full synchronous commits, and explicit
transactions. A project POSIX lock serializes mutation; read-only status remains
available through safe WAL reads. Canonical exports use atomic rename. Bare-Git
source commits and ordinary Lean Git history remain recoverable after an
interruption.

Failure evidence is append-only and tied to one run. A run freezes its target
and selected-claim scope, end-of-run claim metadata/state, and dependency edges
alongside structured incidents and artifact references. Consequently, viewing
an old failed run never substitutes the current claim state or a later version
of the proof graph. Insertion/completion timing from parallel workers is not a
causal ordering contract; the primary blocker is selected by deterministic
target/path/incident ordering.

The backend emits a tree outline whenever the frozen dependency graph is
acyclic. A shared prerequisite may occur under more than one parent, but a
repeated expansion terminates in an explicit shared-reference leaf. If and only
if a cycle exists, the backend condenses strongly connected components and
returns a finite component graph. Every UI uses that reported mode rather than
attempting recursive graph traversal itself.

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

The default logical proof-batch fan-out is two processes. Each has its own AI
turn, RepoProver Lean pool, Git worktree, and isolated root build, but logical
fan-out does not override the machine-global AI, Lean, or build admission
limits. The host merges results deterministically and performs fresh
certification.

Compatible Mathlib/REPL dependencies share the established depot under
`$HOME/.cache/repoprover-codex`. Projects and ephemeral worktrees have isolated
root builds. Reservations, OS leases, a coarse SQLite index, atomic quarantine,
and one deadline per GC pass protect active data and bound cleanup work. See
[Cache and storage](CACHE_AND_STORAGE.md).
