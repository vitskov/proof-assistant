# Incremental verification model

## Persistent project

A verification project is both an ordinary Lean/Git project and a deterministic
state store:

```text
project/
├── Manuscript.lean
├── Formalization/
│   ├── Foundation.lean
│   ├── All.lean
│   └── Claims/                    # stable per-claim modules
├── manuscript/                    # exact current read-only snapshot
├── VERIFY.yaml                    # project-owned validated task
├── RepoProverInput/TASK.md        # generated worker view of that task
├── VERIFICATION_STATUS.md
├── CLARIFICATION_REQUEST.md
├── VERIFICATION_REPORT.md
└── .repoprover/
    ├── state.sqlite3
    ├── snapshots/manuscript.git/  # immutable content-deduplicated source
    ├── exports/                   # canonical graphs/certificates
    └── runs/000001/               # diff, diagnostics, model/build evidence
```

The author edits the original manuscript folder, never the project copy. The
source may be in Dropbox, but the managed project may not. A stable-source
observer inventories the entire filtered source tree twice, stages and
re-hashes its copy, and asks for explicit change-plan confirmation. The backend
then snapshots it into a private bare Git repository and atomically updates
`project/manuscript/` from that exact commit.

Filesystem notifications are only wake-up signals; they are not evidence that
a multi-file save has finished. The confirmed source inventory is the strict
input contract between observation and snapshot preparation.

Each project also persists one mandatory source-relative main file. From that
root the backend recursively resolves literal `\input` and `\include` commands,
including inputs of inputs, and records the resulting closure. The source
snapshot can preserve the complete filtered container, but theorem extraction,
labels, references, and custom theorem environments come only from the selected
main-file closure. This allows several papers or abandoned drafts to coexist in
one author folder without contaminating one another's verification graph.

Include resolution is deterministic, relative to the including file, and
cycle-safe. A missing include, absolute path, source-root escape, or dynamic
path that cannot be interpreted causes indexing to fail closed. Changing an
include command can add or remove an entire source subtree; the next impact
plan recalculates the closure before deciding which claims and descendants need
work.

## Two dependency graphs

The manuscript graph contains definitions, assumptions, theorem-like objects,
and labeled equations. Edges initially come from `\ref`, `\eqref`, `\cref`,
and related references. A proof worker can propose a semantic edge through a
validated host tool; the SQLite graph owns it afterward and rejects cycles.

The Lean graph is extracted mechanically from elaborated declarations. A
Lean-side helper traverses declaration types and proof/value expressions,
collects direct `Expr.const` references, records axioms, and emits structural
expression encodings. Python hashes those canonical structures; it never
infers Lean dependencies from source text or agent prose.

An explicit correspondence table maps manuscript IDs to Lean declarations.
Dependency audits compare mapped nodes in both graphs and report formal proof
dependencies that the manuscript graph does not mention.

## Failure explanations and dependency maps

A failed search is not explained by a bare terminal state. Each verification
run records structured failure incidents with their scope (run, batch, claim,
or dependency component), category, exact message/detail, affected claims,
retryability, and available command/log artifacts. It also freezes the run's
targets, selected claims, end states, source locations, and dependency edges.
Those historical facts remain unchanged when a later manuscript edit or retry
changes the live graph.

For each target, the backend follows dependent-to-dependency edges and records a
canonical target-to-blocker path. The report's “first blocking reason” is the
first deterministic representative under stable target, shortest-path, graph,
and incident ordering. It is deliberately not defined as whichever parallel
worker happened to finish first. All independent blockers remain present even
when one is selected for the headline.

The ordinary presentation is a proof tree. Direct failure nodes carry exact
incident references; claims that cannot run because of a failed prerequisite
are shown separately as blocked. Shared prerequisites become finite shared
references rather than recursively duplicating their descendants. Explicit
LaTeX-reference cycles are possible but uncommon. When one occurs, the backend
condenses strongly connected components and reports a component/edge view;
neither the report nor a UI recursively unfolds a cyclic structure.

Run-wide provider, compiler, workspace, or runtime-dependency incidents remain
run/batch incidents instead of being misrepresented as independent mathematical
failures in every affected claim. A later retry preserves the earlier incident
and records new state rather than overwriting its explanation.

## Change detection and invalidation

Every source object has exact statement/proof hashes and a whitespace/comment
normalized statement hash. Author-to-assistant context is tracked separately;
changing it invalidates the attached claim's AI input without pretending that
the mathematical statement itself changed. A pass distinguishes:

- no relevant change: preserve state;
- proof-only edit in theorem mode: preserve the theorem certificate;
- proof edit in argument-audit mode: dirty the claim;
- statement edit: mark the claim `DIRTY_SOURCE`; and
- deletion or dependency change: invalidate the reverse-reachable slice.

Unaffected branches remain certified. A dirty statement is reviewed and
elaborated again. If its new structural Lean type hash equals the previous
certificate, the existing proof is reconciled and reused; otherwise the claim
and its dependent slice require new proof work.

## Scheduler and parallelism

Only claims whose known dependencies are certified enter the ready frontier.
Ready claims are placed in bounded batches. The legacy `--jobs 2` default is a
minimum logical worker fan-out, not an account-level AI-provider limit. When
several batches are ready, the scheduler may instantiate additional logical
workers up to the current machine AI limit, while the global AI admission controller
independently caps active remote turns. Fixed mode plus explicit numeric limits
provides reproducible single- or multi-worker debugging.
Each batch uses a detached Git worktree and isolated root `.lake/build`, while
sharing the compatible dependency depot.

Workers may commit only their assigned per-claim Lean modules. The host merges
completed batches in deterministic order and rejects changes to manuscript or
host-controlled files. It then independently builds the merged project and
extracts declarations before changing any claim to `CERTIFIED`.

## Clarification and resume

A clarification tool accepts only manuscript-level categories and requires the
worker to attest that Lean/API diagnosis and assumption-sufficiency checks were
performed first. Questions are structured records; at most one can be open for
a claim. Dependents become `BLOCKED_DEPENDENCY`, while independent certificates
remain usable.

Conjectures and theorem-like assertions without a structurally attached
manuscript proof are not proof obligations. The host records them as
`SKIPPED_UNPROVED` and never schedules them for an AI proof batch. It creates a
deterministic clarification only if a selected proof-bearing assertion depends
on one; the question's blocking set contains those proof-bearing dependents and
never the unsupported assertion itself. A project containing only skipped
assertions completes with `no_proof_obligations`. Opening an older project also
reconciles and supersedes legacy self-blocking conjecture questions.
Agent-discovered semantic edges are tied to the current source argument: an
edit to their dependent assertion retires those edges and schedules fresh
dependency discovery, so removing a reliance also clears its policy question.

### Author-to-assistant LaTeX comments

Place an assistant comment block immediately before the mathematical
environment it describes:

```latex
%% assistant: This abridged theorem is a corollary of the stronger proved
%% \Cref{thm:full}; use the full theorem when checking later dependents.
\begin{theorem}\label{thm:abridged}
  ...
\end{theorem}
```

The exact contract is:

- `%% assistant:` starts the block;
- immediately following `%%` lines continue it;
- the first ordinary LaTeX line ends it; and
- the block applies only to the indexed object it immediately precedes.

Proof workers receive the block as `assistant_context`. It is advisory author
context, never a premise, proof, certificate, or source of claim identity. A
valid `\ref`, `\cref`, or related reference in the block adds an advisory graph
edge. For an unproved abridged claim, that edge allows the scheduler to certify
the referenced stronger claim first and then attempt dependents directly from
kernel-checked facts. The abridged claim remains `SKIPPED_UNPROVED` and is never
silently certified from the comment.

Edit the authoritative manuscript. The workflow service creates a complete
`ChangeImpactPlan` and the TUI shows the file changes, changed claims, and
dependent closure. Only explicit confirmation permits the next iteration. A
changed question source object deterministically supersedes the old question.
The new snapshot, graph slice, and formal type decide what is reused. Questions
never disappear merely because a later agent is silent.

The source file, excerpt span, question category, diagnostics, and blocked
claims come from persisted deterministic data. A separately isolated AI
presentation pass may improve wording, but its output is schema-validated and
cannot change those facts.

## Claim states and authority

Important states include `DISCOVERED`, `PROVING`, `CERTIFIED`, `DIRTY_SOURCE`,
`INVALIDATED`, `NEEDS_CLARIFICATION`, `BLOCKED_DEPENDENCY`,
`FAILED_TECHNICAL`, `FAILED_FORMALIZATION`, `UNRESOLVED`, `SUSPECT_FALSE`, and
`COUNTEREXAMPLE_FOUND`. `SKIPPED_UNPROVED` is a terminal, non-failing state for
a conjecture or theorem-like assertion without an attached manuscript proof.

Only Lean can create a certificate. A certificate records source snapshot and
statement hash, Lean declaration/type/value hashes, manuscript and Lean
dependencies, axiom set, Lean/Mathlib versions, environment hash, and run ID.
An inconclusive proof search never becomes a falsity claim. A counterexample
outcome requires its own kernel-checked Lean declaration.

## Crash and concurrency safety

Project mutation uses an exclusive POSIX file lock. Status remains readable
during a writer through SQLite WAL and atomic files. Database state changes use
full-synchronous transactions; exports use temporary files plus atomic rename.
The next invocation marks an abandoned `RUNNING` row `INTERRUPTED`. Git source
commits, Lean source, certificates, questions, and diagnostics remain available
without a model thread or resume token. TUI navigation is not persisted as
verification truth; resume routing is derived from these authoritative records.
