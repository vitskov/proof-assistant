# Usage guide

Proof Assistant is designed around a persistent user loop:

```text
start or resume -> verify -> findings or clarification -> author edit
       ^                                                   |
       +--------- review detected impact and confirm ------+
```

## Launch

```bash
proof-assistant
```

`proof-assistant tui` is an explicit equivalent. The welcome screen offers
**New project** and **Resume project**.

## Start a project

### 1. Choose the manuscript source

Select an existing directory containing the LaTeX manuscript. This remains the
author-facing source folder: when Proof Assistant asks for a clarification,
edit this original folder in your normal editor.

Choose **Browse folders** for a terminal-native directory picker that works over
SSH. Arrow keys move through the folder list, Enter opens the highlighted
folder, and the explicit **Up**, **Home**, **Select current folder**, and
**Cancel** controls are keyboard-focusable. Current and highlighted paths are
selectable and copyable. You may instead type a path in the form field.

The picker starts at the last folder you explicitly selected on this machine,
or at your home directory when that preference is absent, malformed, or stale.
Navigation and cancellation do not change the preference: only **Select current
folder** does. The preference is stored in local configuration outside managed
projects and Dropbox; it does not become project state. Its normal location is
`~/.config/proof-assistant/preferences.json`, or the corresponding safe
`XDG_CONFIG_HOME` location. A Dropbox-backed or managed-project XDG location is
ignored in favor of the home-local path.

The source may be in Dropbox. In that case the TUI warns that synchronization
can expose intermediate multi-file saves. It still permits the source because
the importer waits for a stable inventory, stages a complete copy, re-hashes
it, and asks you to review changes before verification.

### 2. Select the main LaTeX file

After entering the source folder and other setup fields, choose **Continue:
inspect source**. Proof Assistant inspects the selected folder before it creates
any project:

- if the folder contains exactly one `.tex` or `.ltx` file, the review screen
  announces that file as the automatically selected main file without asking a
  redundant question;
- if it contains several LaTeX files, the TUI lists every candidate and requires
  an explicit selection; and
- if it contains none, project creation stops with a validation error.

Select the document root: normally the file containing `\documentclass` whose
compilation produces the paper. The main file may recursively use `\input` and
`\include`; those children may include further children. Proof Assistant
resolves that complete, cycle-safe closure. Plain input forms try the including
file's directory and then the source root; if both resolve to different files,
the backend rejects the ambiguity. `\\import`/`\\subimport` retain their
including-file-relative semantics.

The choice is not a display preference. `main_file` is a required backend
contract and is persisted in the managed project. Only the selected root and
its resolved inputs are theorem-indexed. Other possible roots, old drafts, and
orphaned `.tex` files under the source folder are excluded from verification.
An absolute include, an include escaping the source directory, or a referenced
file that cannot be resolved fails closed instead of silently checking an
incomplete manuscript.

### 3. Choose the managed project

Give the project a name. Unless changed, it is created at:

```text
$HOME/proof-assistant/<project-name>
```

The project must not be in Dropbox. It contains durable Git history, database
state, imported source snapshots, Lean code, certificates, questions, and
reports. Do not edit its managed manuscript copy; edit the external source
selected in step 1.

### 4. Define the task

Choose one of:

- **Use default task** — verify every indexed theorem-like claim under its
  stated assumptions; or
- **Customize task** — edit the seeded instructions in the TUI's multiline
  text editor.

Proof Assistant validates and stores the result as `$PROJECT/VERIFY.yaml`.
There is no external task file to select, keep synchronized, or edit.

### 5. Create and verify

Review the entered source, selected main file, destination, task choice, and
Dropbox notices. No managed project has been created at this point. Choose
**Confirm, create, and verify** to create a stable initial import, resolve the
main file's input closure, and start the first verification pass. Going back
preserves the setup form and custom task; for multi-root manuscripts it also
preserves the selected main file.

## Verification progress

The progress screen always identifies the persisted main file and lists every
resolved `\input`/`\include` file in its manuscript closure. It reports detailed
phases, substage messages, and claim counts rather than flooding the terminal
with raw Lean and Codex logs.

The stage pane displays the complete typed pipeline with pending, active, and
done status: validation, stable source observation, source import, indexing,
impact analysis, cache setup, Lean build, Lean declaration extraction, proof
batches, independent certification, report generation, and completion. Live
substage messages and claim counts appear in a separate event-log pane.

The sources, stages, and event log are separate focusable, read-only text
areas. Focus a pane and use the normal TextArea selection/copy actions
(`Ctrl+A`, then `Ctrl+C`, for all text), so paths and diagnostics do not need to
be retyped from the terminal.

Closing the interface does not erase project state. On the next launch choose
**Resume project**. Proof Assistant recovers interrupted run state before
deciding which screen to show.

### What “Request cooperative cancellation” preserves

Cancellation is cooperative, so an active Codex/Lean batch is allowed to stop
at a host-controlled boundary. If cancellation is already requested when the
workers return, their temporary candidates are discarded before merge. If a
merge has begun, Proof Assistant finishes that round's independent Lean build
and kernel certification before stopping. This prevents a half-merged round
from being mistaken for verified work.

Every certificate completed before the boundary remains durable in the
project. Claims that were only marked `PROVING` are reset to the retryable
`INVALIDATED` state, never to `CERTIFIED`; the next run schedules them again.
The run is recorded as `INTERRUPTED`, a cancellation summary is written under
`.repoprover/runs/`, status/exports are refreshed when possible, and temporary
batch worktrees are removed. A sudden process or machine interruption receives
the same `PROVING`-to-`INVALIDATED` recovery on the next project open.

## When no manuscript change is needed

The findings screen groups human-readable outcomes such as:

- verified and newly certified;
- verified using a reusable certificate;
- formal statement reconciled;
- unresolved without evidence of falsity;
- suspected false;
- kernel-checked counterexample;
- dependency/correspondence discrepancy; and
- provider, Lean, or setup failure.

It also displays the project directory and the paths to status, report,
evidence, and run artifacts. “Not verified” never means “false.” Only a
kernel-checked counterexample with reviewed statement correspondence supports
the counterexample outcome.

Choose **View report in terminal** to open the canonical report without leaving
the TUI. The **Rendered** tab displays Markdown with headings, code blocks,
tables, scrolling, and a table of contents. The **Copyable source** tab exposes
the exact read-only Markdown; focus it and use `Ctrl+A`, then `Ctrl+C`. The
report path and any load error are selectable as well. This is the supported
viewer over SSH—no browser, Finder, or desktop file handler is required.

For an incomplete or failed check, choose **Inspect failure dependencies**. The
backend identifies a deterministic first blocking reason and preserves the
exact run in which it was observed. The normal **Proof tree** tab is expandable
and uses both text and color: `[FAIL]` is a direct failure, `[BLOCKED]` depends
on unfinished or failed work, and `[OK]` has a certificate. Highlight a node to
preview its reason; press Enter for its complete, copyable incident details,
source location, path from the verification target, and artifact/log paths.
The **Copyable full outline** tab contains the same information as selectable
plain text for SSH sessions and issue reports.

Several claims may share one prerequisite. The tree marks a repeated occurrence
as a shared reference and does not expand it indefinitely. If the manuscript
contains an actual dependency cycle, the map automatically changes to a finite
**Cycle components** table plus the component edge list. This fallback is used
only for a detected cycle; ordinary acyclic manuscripts retain the more natural
tree presentation.

Copyability is a TUI-wide rule. Paths, candidate main files, commands,
progress values, findings, warnings, errors, and source excerpts appear in
read-only selectable panes. When Rich or Markdown rendering is useful, an exact
selectable source twin is provided.

## When clarification is needed

The clarification screen displays:

- the affected claim and question category;
- the actual relative and absolute input-file path;
- a line-numbered, syntax-highlighted LaTeX excerpt;
- highlighted source lines requiring attention;
- the reason the verifier cannot safely guess;
- possible resolutions; and
- the blocked portion of the proof tree.

The deterministic backend chooses the source file, byte/line span, quotation,
claim, diagnostics, and affected graph. An isolated Codex presentation pass may
make the wording clearer, but cannot change those facts. Invalid presentation
output falls back to a deterministic explanation.

Use the open-file/folder action or your normal editor and edit the external
source. For manuscripts with `\input` or `\include`, edit the exact file shown,
not necessarily the root `main.tex`.

## Detecting and approving author changes

Proof Assistant uses filesystem notifications only as a wake-up signal. It
accepts a source update only after two complete inventories agree and the
staged copy re-hashes successfully. The change review includes additions,
modifications, deletions, and renames in the previously or currently resolved
main-file closure, even if several input files changed together. The complete
filtered source container is still hashed to reject a confirmation if any file
changes after review, but alternate roots do not become verification claims.

It then previews:

- changed source files and detailed diffs;
- statement changes versus proof-only changes;
- directly changed claims;
- the complete affected descendant closure;
- certificates expected to remain reusable; and
- clarification requests the import would supersede.

Choose **Start next iteration** or **Keep waiting for more edits**. You can also
open the authoritative source folder from this screen.

Starting is always explicit. Immediately before acceptance, Proof Assistant
checks that the external inventory still equals the plan you reviewed. If the
source changed again, it discards the stale plan and computes a new one.

## Resume behavior

The project database, not the TUI widget tree, is authoritative. On resume:

| Persistent condition | Screen |
|---|---|
| active detached verification job | attached live progress |
| open clarification, no external change | existing clarification |
| open clarification, stable external change | change review |
| completed run | findings |
| interrupted run | recovery/retry |
| provider or Lean failure | diagnostic/retry |
| legacy verification worker still active | attached coarse progress/status |

The welcome screen uses the same backend project classifier as creation. It
shows resumable projects, legacy projects needing a main-file choice,
incomplete project directories, and non-project occupied directories. The last
two are never deleted or adopted automatically; use **Open folder** to inspect
them. A new-project destination is checked before source inspection, and a
conflict returns to the preserved setup form without importing anything.

### Delete a managed project

A resumable row also has **Delete project**. This is a recoverable,
backend-owned operation, not a recursive deletion performed by the TUI:

1. the backend reclassifies the exact path and tests its project lock;
2. the confirmation dialog shows selectable managed-project and external-source
   paths;
3. **Cancel** is focused by default, while deletion requires activating the
   separate red **Delete managed project (recoverable)** button; and
4. the backend tests the lock again, atomically moves only the managed project
   into a collision-safe recovery area, then refreshes the catalog.

The external manuscript folder is never moved or modified, including when it
is in Dropbox. A running project is reported as **BUSY** and cannot be moved.
Incomplete projects, unrelated occupied paths, overlapping source/project
configurations, Dropbox-managed paths, and unsafe recovery locations are
refused. On macOS the recovery area is the user's `~/.Trash`; on other systems
it is the Proof Assistant-owned
`$XDG_DATA_HOME/proof-assistant/recoverable-trash`. The result screen shows the
exact recovery path. The project remains recoverable until that returned path
is manually removed (or, on macOS, Trash is emptied).

Proof Assistant does not regenerate a question or start another verification
when the source has not changed.

The TUI never owns a project or its verification lock. Starting verification
submits a durable job to a detached backend worker. Its request, progress-event
cursor, heartbeat, cancellation request, worker log, and terminal state live in
the managed project. You may close the TUI or lose an SSH session without
cancelling the job; another TUI attaches to the active job and replays events.
Two simultaneous starts of the same request attach to one worker. A genuinely
different request receives an explicit active-job conflict instead of creating
a second writer. **Request cooperative cancellation** persists a backend
request; it is reported as safely stopped only after the worker reaches a
consistency boundary and records its cancellation report.

The selected main file is persistent project identity. Resuming a current
project never asks you to select it again; the backend loads it and resolves its
input closure before planning or verifying. For a legacy project that predates
this contract, the backend can migrate an unambiguous source (one LaTeX file or
one uniquely identifiable document root). If several possible roots remain,
the project remains visible as **NEEDS_MAIN_FILE**. Choose **Select main file**,
make a deliberate selection from the backend-provided candidates, and review
the resulting proof-impact plan before starting another iteration. Existing
evidence is preserved; the TUI never edits project metadata itself.

## Concurrency and machine resources

Open **Settings** from the welcome screen or a project dashboard, then choose
**Concurrency / Resources**. Proof Assistant controls active Codex turns, Lean
checks, and Lake builds with three independent machine-wide admission
controllers. The page shows each configured value beside its effective value,
the source of the setting, and live CPU, memory, swap, queue, and throttle
information. Automatic/adaptive mode is the default.

Changes are previewed before they are saved. Unsafe-looking manual values show
a warning and recommendation. Lowering a live limit stops new admissions but
allows already-running work to finish safely. **Reset all to Auto** returns the
machine to automatic policy. The separate **Legacy settings** page contains
logical proof-worker count, batch size, and the compatibility per-worker Lean
pool; these do not replace the three resource controllers.

Settings apply to every project on this machine. A project-overlay scope is
reserved in the backend contracts but is not enabled. See [Concurrency and
resource management](CONCURRENCY.md) for formulas, precedence, CLI/environment
overrides, pressure behavior, benchmarks, provenance, and the upstream
RepoProver migration map.

## Advanced status commands

The TUI is the primary interface. These commands are useful for monitoring or
automation against an existing project:

```bash
proof-assistant manuscript status --project "$PROJECT"
proof-assistant manuscript questions --project "$PROJECT"
proof-assistant manuscript graph --project "$PROJECT"
proof-assistant manuscript diff --project "$PROJECT"
```

To run verification without the TUI after a project exists:

```bash
proof-assistant manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

Two logical proof-batch workers are the legacy default. `--jobs` controls batch
process fan-out, not active Codex turns: the machine AI controller is
authoritative across all workers, and the Lean and build controllers enforce
their own separate limits. For fully reproducible resource limits, use
`--concurrency fixed` with explicit AI, Lean, and build values. Merge order and
final certification remain host-controlled and deterministic.

## Project results

Start with:

```text
$PROJECT/VERIFICATION_STATUS.md
$PROJECT/VERIFICATION_REPORT.md
$PROJECT/CLARIFICATION_REQUEST.md
$PROJECT/VERIFY.yaml
$PROJECT/Formalization/
$PROJECT/.repoprover/exports/
$PROJECT/.repoprover/runs/
```

The exact on-disk internal names are backend data, not a TUI contract. Other
front ends can drive the same workflow service without changing the persistent
verification engine.
