# Troubleshooting and operations

## A run appears quiet

First inspect the durable status:

```bash
sed -n '1,120p' "$OUTPUT/RUN_STATUS.json"
```

Then verify that the launcher or its Codex/Lean children still exist:

```bash
ps -axo pid,ppid,etime,state,%cpu,%mem,command | \
  rg 'repoprover-codex manuscript-run|codex app-server|lake|lean'
```

Interpretation:

- `dependency_setup` with a `lake`, `git`, or cache-download child is normal on
  a cold cache;
- `codex_turn` with an app-server child is an active model turn;
- a Lean REPL or `lake build` child means formal checking is in progress; and
- final event/tool-call artifacts may remain absent until the Codex turn ends.

Do not infer a hang from low CPU alone: a process waiting for model or tool I/O
normally sleeps. Compare elapsed time with `--turn-timeout` and look for child
process changes or new Git/Lean files.

## Cache reconciliation is visible

`cache GC reconciling coarse cache index` is a bounded phase. The index has one
row per build, dependency depot, or bulk cache—not one row per Mathlib archive.
Changed units are measured once, and deletion reports progress every 30 seconds.
The default total GC budget is 900 seconds.

If the deadline is reached, the command fails explicitly. A partially deleted
unit remains atomically isolated under `trash/` for the next recovery pass.

## Disk pressure

```bash
repoprover-codex cache status
repoprover-codex cache gc --gc-timeout 900
```

The default policy permits 16 GiB of managed cache while preserving at least 25
GiB of filesystem free space. Active jobs hold reservations and leases; manual
GC skips their cache entries.

Do not brute-force cache deletion while a job is active. If emergency manual
cleanup is unavoidable, stop all jobs first and resolve the exact managed paths
under `repoprover-codex cache path`. Manuscript output folders are separate from
the shared cache.

## A run was interrupted

The output folder is intentionally preserved. Inspect `RUN_STATUS.json`,
`artifacts/`, and the workspace Git history. The OS releases process-held cache
leases automatically; the next package operation clears stale reservation rows
and completes any quarantined cleanup.

To rerun, select a new or empty output directory. Removing an old output does
not remove the shared dependency depot.

## Setup or compiler failure

Run:

```bash
repoprover-codex compiler-check
repoprover-codex cache doctor
```

The compiler check must compile and execute a program. Merely finding `clang`
or another compiler on `PATH` is not sufficient. On supported older macOS
systems, `/usr/bin/clang` may be selected when Lean's bundled compiler cannot
run.

## Codex connectivity or model failure

```bash
repoprover-codex doctor
repoprover-codex models
```

Use an exact model/effort pair printed by `models`. Authentication should come
from `codex login`; the package does not require an API key and does not read
Codex authentication files.

The child app-server disables configured MCP servers, apps, plugins, bundled
skills, and local skill instructions, then verifies the resulting inventory.
Startup fails closed if an external capability remains available.

## Understand a terminal outcome

Read these in order:

1. `RUN_STATUS.json`
2. `artifacts/result.json`
3. `VERIFICATION_REPORT.md`
4. `artifacts/verification-build.log`
5. `artifacts/setup.log` for setup failures

An unverified or incomplete result is not a claim of mathematical falsehood.
See [Results and evidence](USAGE.md#results-and-evidence) for the success
criteria.
