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

## Change detection and invalidation

Every source object has exact statement/proof hashes and a whitespace/comment
normalized statement hash. A pass distinguishes:

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
Ready claims are placed in bounded batches. `--jobs 1` processes batches
sequentially; `--jobs 2` may run two independent Codex processes concurrently.
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

Edit the authoritative manuscript. The workflow service creates a complete
`ChangeImpactPlan` and the TUI shows the file changes, changed claims, and
dependent closure. Only explicit confirmation permits the next iteration. A
changed question source object deterministically supersedes the old question.
The new snapshot, graph slice, and formal type decide what is reused. Questions
never disappear merely because a later agent is silent.

The source file, excerpt span, question category, diagnostics, and blocked
claims come from persisted deterministic data. A separately isolated Codex
presentation pass may improve wording, but its output is schema-validated and
cannot change those facts.

## Claim states and authority

Important states include `DISCOVERED`, `PROVING`, `CERTIFIED`, `DIRTY_SOURCE`,
`INVALIDATED`, `NEEDS_CLARIFICATION`, `BLOCKED_DEPENDENCY`,
`FAILED_TECHNICAL`, `FAILED_FORMALIZATION`, `UNRESOLVED`, `SUSPECT_FALSE`, and
`COUNTEREXAMPLE_FOUND`.

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
