# Troubleshooting and recovery

## The TUI closed or a run was interrupted

Relaunch and choose **Resume project**:

```bash
proof-assistant
```

Do not delete the project. The next backend mutation marks an abandoned
`RUNNING` record `INTERRUPTED` and routes to recovery. Source snapshots,
certificates, open questions, Lean code, and reports remain durable.

## A project appears quiet

The TUI progress screen names the selected main file, lists its resolved input
closure, and gives phase/substage messages in separate read-only text areas.
Focus any area to select and copy its text (`Ctrl+A`, then `Ctrl+C`, copies that
entire pane). Advanced status remains readable during an active writer:

```bash
proof-assistant manuscript status --project "$PROJECT"
```

`mutation in progress: yes` with a `RUNNING` row means another process owns the
project lock. A second TUI should enter read-only progress instead of mutating
the project.

For diagnosis only:

```bash
ps -axo pid,ppid,etime,state,%cpu,%mem,command | \
  rg 'proof-assistant manuscript verify|codex app-server|lake|lean'
```

- a main-project `lake build` is setup or independent certification;
- a managed-cache worktree build is an isolated proof batch;
- `codex app-server` is an active semantic/proof turn; and
- `DependencyExtractor.lean` is structural declaration extraction.

Compare elapsed time with `--turn-timeout`; that timeout applies to each Codex
batch, not the complete manuscript.

## The selected main file cannot be indexed

Proof Assistant fails closed when `\input` or `\include` names a missing file,
an absolute path, a path outside the selected source folder, or a dynamic path
it cannot resolve deterministically. Fix the command in the author source and
try again. Do not copy an external file into the managed manuscript snapshot by
hand.

Only the selected main file and its recursive input closure are indexed. If an
expected theorem is absent, first confirm that its file is reachable from the
persisted main file. Conversely, a theorem in another root or an orphaned draft
is intentionally excluded.

Projects created before the mandatory-main-file contract can resume
automatically when their source has one LaTeX file or one uniquely identifiable
document root. If several roots remain possible, resume enters recovery instead
of guessing. The welcome screen keeps the project visible as
**NEEDS_MAIN_FILE**; choose **Select main file** and make the explicit choice.
The backend records it and presents a change-impact review before verification.
The legacy project and its existing evidence remain intact.

## A project destination says it must be new or empty

The welcome screen and new-project preflight use the same backend classifier.
An existing valid project appears with **Resume**, an ambiguous legacy project
with **Select main file**, and incomplete or unrelated occupied directories with
their diagnostic and **Open folder**. Proof Assistant never deletes or adopts
an occupied directory automatically. Return to the preserved setup form and
choose another managed-project path if the directory is unrelated.

## Clarification returns after resume

That is expected when the external manuscript has not changed. Proof Assistant
does not regenerate the question or start a redundant run; it returns to the
persisted clarification screen.

Edit the exact external file shown. Do not edit `$PROJECT/manuscript` or the
generated clarification report. After the source stabilizes, review the full
multi-file change/impact plan and explicitly start the next iteration.

Advanced inspection:

```bash
proof-assistant manuscript questions --project "$PROJECT" --json
```

## Dropbox source warning

An external source in Dropbox is allowed. The warning explains that an editor
and Dropbox may expose intermediate saves. Proof Assistant waits for matching
complete inventories, verifies a staged copy, and confirms the plan again before
import. If files keep changing, choose **Keep waiting for more edits** rather
than forcing a partial iteration.

A managed project, Python environment, or cache in Dropbox is an error. Move or
recreate it under the defaults in [Installation](INSTALLATION.md).

## Provider or Lean failure

Run:

```bash
proof-assistant compiler-check
proof-assistant cache doctor
proof-assistant doctor
proof-assistant models
```

The compiler check must compile and execute a program. Select an exact
model/effort advertised by `models`. Authentication comes from `codex login`;
do not expose an API key or token to Proof Assistant.

Persistent verification exit codes preserve failure boundaries:

- 20: project/cache/setup failure;
- 21: Codex authentication, protocol, or provider failure; and
- 22: Lean bootstrap, build, merge, or extraction failure.

Exit 11 is partial/inconclusive and never means false. Exit 12 is reserved for a
kernel-checked counterexample outcome.

## Disk pressure or cache reconciliation

```bash
proof-assistant cache status
proof-assistant cache gc --gc-timeout 900
```

The cache remains at `$HOME/.cache/repoprover-codex` after the rename so
existing Mathlib data is reused. Do not create a new “proof-assistant” cache to
solve pressure; that duplicates the largest data.

GC treats thousands of Mathlib archives as one coarse candidate and never
rescans the full tree inside its eviction loop. Active entries are protected by
leases and reservations. Do not brute-force delete caches while any job is
active. Persistent verification projects are outside the cache and are not GC
candidates.

## The external source moved

The resolved source location is part of persistent project identity. Restore it
at that location or create a new project selecting the new source folder. Do
not hand-edit SQLite or copy files over `$PROJECT/manuscript`. The old managed
project and its evidence remain intact.

## The task needs to change

The TUI task editor creates the project-owned task during setup. For an existing
project, do not introduce a separate user-supplied task file or edit task state
while verification owns the project lock. Any future task edit must update the
managed `$PROJECT/VERIFY.yaml` through a workflow-aware interface so task impact
is reviewed before the next iteration.
