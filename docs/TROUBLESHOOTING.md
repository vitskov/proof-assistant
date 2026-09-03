# Troubleshooting and recovery

## The TUI closed or a run was interrupted

Relaunch and choose **Resume project**:

```bash
proof-assistant
```

Do not delete the project. The next backend mutation marks an abandoned
`RUNNING` record `INTERRUPTED` and routes to recovery. Source snapshots,
certificates, open questions, Lean code, and reports remain durable.

## The report button appears to do nothing over SSH

Upgrade and restart the TUI. **View report in terminal** loads the canonical
project report through the backend and displays it inside Textual; it does not
call the operating system's graphical opener. Use the rendered tab for
navigation and the copyable-source tab for exact selection. If the report is
missing, unreadable, invalid UTF-8, or outside the managed project boundary,
the viewer shows a selectable error and preserves the findings screen behind
its **Back** action.

## A project appears quiet

The TUI progress screen names the selected main file, lists its resolved input
closure, and gives phase/substage messages in separate read-only text areas.
Focus any area to select and copy its text (`Ctrl+A`, then `Ctrl+C`, copies that
entire pane). Advanced status remains readable during an active writer:

```bash
proof-assistant manuscript status --project "$PROJECT"
```

`mutation in progress: yes` with a `RUNNING` row means the detached backend
worker currently holds the project's mutation lease. Any TUI can attach to the
durable job and show its progress; closing a TUI does not release or cancel that
backend work.

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
persisted clarification screen. Resume also does not make a paid AI request.
It validates and displays the evidence-bound **Best current guess** recorded
when the clarification was created.

If the screen says **Best current guess unavailable**, the question may predate
clarification analysis or its stored analysis may have failed strict
schema/provenance validation. The immutable observed problem, exact source, and
blocked claims remain authoritative. Do not infer that an unavailable
hypothesis resolved the question, and do not edit the managed database.

If the external manuscript changed after the question was created, **Resume**
opens Change Review first. This is intentional: review or reject the current
source differences before Proof Assistant returns to or supersedes the old
clarification.

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
proof-assistant ai status
proof-assistant ai status --driver DRIVER
proof-assistant ai models DRIVER
```

The compiler check must compile standard C and Lean headers and execute a native
program. If verification reports `Required Lean setup failed: lake update`,
inspect `$PROJECT/.repoprover/runs/RUN_ID/setup.log`; dependency resolution can
succeed while a post-update native build fails.

Do not set `LEAN_CC` to Lean's own `bin/clang`. That bypasses the internal flags
normally supplied by `leanc` on both macOS and Linux. Repair a cache created by
an older Proof Assistant release and prepare the project again with:

```bash
env -u LEAN_CC proof-assistant cache init
proof-assistant cache prepare --project "$PROJECT"
```

If an older run reports `Lean extractor returned invalid value_expr`, update or
reinstall Proof Assistant and reopen the project. That message came from an
older parser rejecting the extractor's structured JSON expression format; it
does not indicate a malformed manuscript. The current parser accepts the
format, and retryable infrastructure reports provide **Retry verification**
without deleting the failed run's evidence.

Schema-1 and schema-2 cache configurations migrate automatically. A deliberate
external compiler remains supported through `--lean-cc /absolute/path/to/cc`.
Select an exact model/difficulty advertised for the chosen driver. A
`curated_fallback` catalog is not proof that the configured account can use
those models.

For a CLI driver, run the native login command shown by `ai status` (`codex
login`, `claude auth login`, or `copilot login`) and recheck. Proof Assistant
never reads the CLI's auth files. Copilot normally remains `unknown` because it
has no documented non-billable status command; use the separate explicit tiny
probe only if you accept one account request:

```bash
proof-assistant ai verify-account copilot_cli --yes
```

For an API driver, select either its environment variable (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`) or the OS keyring. Store a key with a
hidden prompt via `proof-assistant ai credential DRIVER`; never place one in a
project, provider JSON, task, or command argument. See [AI providers and
first-time setup](AI_PROVIDERS.md).

Persistent verification exit codes preserve failure boundaries:

- 20: project/cache/setup failure;
- 21: AI authentication, protocol, or provider failure; and
- 22: Lean bootstrap, build, merge, or extraction failure.

Exit 11 is partial/inconclusive and never means false. Exit 12 is reserved for a
kernel-checked counterexample outcome.

## Bash login shell no longer loads existing setup

Bash reads only the first existing login file among `.bash_profile`,
`.bash_login`, and `.profile`. Proof Assistant must not create `.bash_profile`
merely to add itself to `PATH`, because that can cause a previously active
`.profile`—including its `.bashrc` loader—to be skipped.

Current installers preserve the existing precedence and append to the login
file Bash already uses. If older setup flows created a `.bash_profile` composed
only of one or more Proof Assistant marker/PATH pairs, reinstalling transfers
all managed directories to the next effective login file, moves the original
to `.bash_profile.proof-assistant-backup`, and restores the previous
precedence. No file containing unrelated setup is migrated.

For a legacy file that was subsequently edited and therefore cannot be safely
recognized automatically, restore the prior login chain rather than copying or
replacing the rest of the configuration. On systems where `.profile` is the
established source of truth, a minimal compatibility stanza is:

```bash
if [ -f "$HOME/.profile" ]; then
    . "$HOME/.profile"
fi
```

Inspect the files before making that repair; do not add the stanza if `.profile`
would source `.bash_profile` and create a loop.

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
while a backend verification is active. Any future task edit must update the
managed `$PROJECT/VERIFY.yaml` through a workflow-aware interface so task impact
is reviewed before the next iteration.
