# Architecture and security

## Division of responsibility

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
and proof search. Deterministic host code controls source identity, graph
updates, invalidation, scheduling, state transitions, provenance, and
certificate validation. Lean is the proof authority.

The package never treats an agent completion marker as a certificate. The host
merges only assigned claim modules, runs an independent `lake build`, inspects
elaborated declarations through Lean’s environment API, hashes their structural
types and values, records direct proof-term dependencies and axioms, and only
then changes state to `CERTIFIED`.

## Codex/RepoProver boundary

```text
RepoProver agent tools
    |
    | OpenAI function schemas / validated host handlers
    v
repoprover-codex adapter
    |
    | client-defined dynamic tools over app-server JSONL
    v
isolated Codex app-server -> existing local Codex login
```

RepoProver remains the tool control plane for Lean, Git, files, shell commands,
and Mathlib search. Codex’s own filesystem sandbox is read-only; mutations and
execution occur only through the explicit RepoProver/host tool registry.

Before a turn, child-only configuration disables apps, plugins, bundled skills,
automatic skill instructions, every detected local skill path, and every
configured MCP server. The backend then enumerates effective MCP capabilities
and skills. Startup fails closed if any external capability or enabled skill
remains. The user’s persistent Codex configuration is not changed.

Authentication stays inside Codex. RepoProver Codex does not read
`~/.codex/auth.json`, print tokens, or convert the login into an API key. The
system is not offline: manuscript context and tool results required for the
task are processed by the authenticated Codex service.

## Validated incremental agent tools

Proof workers receive read operations for claims, dependencies, prior
certificates, and source-object diffs. Mutation requests are narrow:

- propose a typed semantic edge (cycle checked);
- propose a claim-to-Lean correspondence;
- request clarification after required diagnostics; or
- record an inconclusive technical/proof-search outcome.

Workers cannot write SQLite, snapshots, graph exports, certificate rows, status
files, the manuscript copy, or host-controlled aggregate modules. Parallel
workers operate in detached Git worktrees and can merge only their assigned
per-claim modules.

## Persistent state and recovery

SQLite uses foreign-key checks, WAL journaling, full synchronous commits, and
explicit immediate transactions. One project-wide POSIX lock serializes
mutation; status uses safe WAL reads while a writer is active. Canonical JSON
exports use atomic rename. Immutable bare-Git source commits and ordinary Lean
Git history remain recoverable even after interruption.

The export manifest records `manuscript_graph_sha256`, `lean_graph_sha256`, and
a `combined_graph_sha256` bound to the source snapshot and package version. The
Lean hash is `null` immediately after initialization and populated after the
first independent Lean extraction.

An interrupted process leaves a `RUNNING` row, which the next invocation marks
`INTERRUPTED`. Open clarification questions are not inferred away. A question
is superseded only by a later snapshot changing its associated source object or
by an explicit user resolution/dismissal.

## Parallelism

The scheduler selects only claims whose manuscript dependencies are certified.
At most two Codex proof processes run concurrently. Each has a separate process,
app-server, RepoProver Lean pool, Git worktree, and isolated root build. Batch
results merge in a deterministic order; conflicts or host-controlled file
changes fail without certification.

## Storage boundary

Large compatible Mathlib/REPL dependencies share a read-only depot. Persistent
projects and ephemeral worktrees have isolated root builds. Transactional
reservations, OS leases, a coarse SQLite index, atomic quarantine, and one
deadline per GC pass prevent active deletion and multiplicative rescans. See
[Cache and storage](CACHE_AND_STORAGE.md) for the complete design.
