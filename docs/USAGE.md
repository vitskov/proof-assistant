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

### 2. Choose the managed project

Give the project a name. Unless changed, it is created at:

```text
$HOME/proof-assistant/<project-name>
```

The project must not be in Dropbox. It contains durable Git history, database
state, imported source snapshots, Lean code, certificates, questions, and
reports. Do not edit its managed manuscript copy; edit the external source
selected in step 1.

### 3. Define the task

Choose one of:

- **Use default task** — verify every indexed theorem-like claim under its
  stated assumptions; or
- **Customize task** — edit the seeded instructions in the TUI's multiline
  text editor.

Proof Assistant validates and stores the result as `$PROJECT/VERIFY.yaml`.
There is no external task file to select, keep synchronized, or edit.

### 4. Create and verify

Review the entered source, destination, task choice, and Dropbox notices, then
choose **Create and verify**. The backend creates a stable initial import before
starting the first verification pass.

## Verification progress

The progress screen reports phases and claim counts rather than flooding the
terminal with raw Lean and Codex logs. It may show:

- stable source import;
- source/claim indexing;
- affected dependency calculation;
- cache and Lean preparation;
- current ready proof frontier;
- independent build and certification; and
- report generation.

Closing the interface does not erase project state. On the next launch choose
**Resume project**. Proof Assistant recovers interrupted run state before
deciding which screen to show.

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
staged copy re-hashes successfully. The change review includes all additions,
modifications, deletions, and renames across the source tree, even if several
files changed.

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
