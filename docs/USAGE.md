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
| open clarification, no external change | existing clarification |
| open clarification, stable external change | change review |
| completed run | findings |
| interrupted run | recovery/retry |
| provider or Lean failure | diagnostic/retry |
| another process owns the project | read-only progress/status |

Proof Assistant does not regenerate a question or start another verification
when the source has not changed.

The selected main file is persistent project identity. Resuming a current
project never asks you to select it again; the backend loads it and resolves its
input closure before planning or verifying. For a legacy project that predates
this contract, the backend can migrate an unambiguous source (one LaTeX file or
one uniquely identifiable document root). If several possible roots remain,
resume enters recovery and verification stops rather than guessing. Create a
new managed project for that source and make the explicit root selection; the
legacy project and its existing evidence remain intact.

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

Use `--jobs 2` only when two independent ready claims should run concurrently.
Merge order and final certification remain host-controlled and deterministic.

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
