# Troubleshooting and operations

## A persistent verification appears quiet

Status is deliberately readable during an active writer:

```bash
repoprover-codex manuscript status --project "$PROJECT"
```

`mutation in progress: yes` plus a `RUNNING` row means the project lock is
owned. Inspect processes without modifying them:

```bash
ps -axo pid,ppid,etime,state,%cpu,%mem,command | \
  rg 'repoprover-codex manuscript verify|codex app-server|lake|lean'
```

Interpretation:

- `lake build` in the main project: independent setup/final certification;
- `lake build` under `~/.cache/repoprover-codex/worktrees/`: isolated batch
  bootstrap or agent checking;
- `codex app-server`: an active semantic/proof-search turn; and
- `DependencyExtractor.lean`: structural type/value/dependency extraction.

The persistent workflow stores batch event/tool artifacts after a Codex turn
returns. An absent `events.json` is not by itself a hang. Compare elapsed time
with `--turn-timeout`, and remember that the timeout applies per Codex batch.

## The command exits 10

This is a planned clarification pause, not a crash.

```bash
sed -n '1,260p' "$PROJECT/CLARIFICATION_REQUEST.md"
repoprover-codex manuscript questions --project "$PROJECT" --json
```

Edit the authoritative manuscript source, then rerun the same verify command.
Do not edit `$PROJECT/manuscript`; it is replaced from the next immutable
snapshot. Certified independent branches are preserved.

## The command exits 11 or 12

Exit 11 is partial/inconclusive and never means false. Read the claim states and
diagnostics in the report/run directory.

Exit 12 requires a kernel-checked counterexample declaration. Review its Lean
type and correspondence before treating it as a counterexample to the intended
prose statement.

## A run was interrupted

Do not delete the project. Operating-system leases are released automatically;
the next invocation marks an abandoned `RUNNING` database row `INTERRUPTED`,
records a new source snapshot, and resumes from persistent state.

If an ephemeral Git worktree remains after a hard kill, inspect it under the
managed cache’s `worktrees/incremental/` path. A later run uses a distinct run
directory. Remove a preserved worktree only after confirming no process uses it
and retaining any desired patch; ordinary completed batches remove theirs.

## Setup, provider, or Lean infrastructure failure

Exit codes distinguish these boundaries:

- 20: project/cache/setup failure;
- 21: Codex authentication, protocol, or provider failure; and
- 22: Lean bootstrap, build, merge, or environment-extraction failure.

Run:

```bash
repoprover-codex compiler-check
repoprover-codex cache doctor
repoprover-codex doctor
repoprover-codex models
```

The compiler check must compile and execute a program. Use an exact model and
effort printed by `models`. Authentication comes from `codex login`; do not
create or expose an API key for this package.

## Cache reconciliation or disk pressure

```bash
repoprover-codex cache status
repoprover-codex cache gc --gc-timeout 900
```

`cache GC reconciling coarse cache index` is bounded. Thousands of Mathlib
archives form one bulk candidate, not thousands of nested rescan candidates.
Active jobs hold leases and reservations; manual GC skips them.

Never brute-force cache deletion while a job is active. If emergency cleanup
is unavoidable, stop all jobs first and resolve exact targets under the path
printed by `cache path`. Persistent verification projects are outside the cache
and must not be deleted as cache cleanup.

## Legacy one-shot runs

For `manuscript-run`, inspect `RUN_STATUS.json`, then
`artifacts/result.json`, `VERIFICATION_REPORT.md`,
`artifacts/verification-build.log`, and `artifacts/setup.log`. A rerun requires
a new/empty output directory. These constraints do not apply to persistent
`manuscript verify`, which deliberately reuses its project.
