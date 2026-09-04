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

`proof-assistant tui` is an explicit equivalent. On an unconfigured machine,
the backend first checks the primary AI driver and opens the provider setup
screen if it is not ready. Choose one of the CLI or API drivers, complete the
displayed authentication step, and recheck. No project is created until one
primary driver is ready. See [AI providers and first-time setup](AI_PROVIDERS.md).

The welcome screen then offers **New project** and **Resume project**.

### Keyboard commands and themes

The footer is permanent and changes with the active screen and focused control,
so the relevant commands remain visible without memorization. The focusable
**Menu** control in the header and **Ctrl+P** both open a searchable command
menu. With an empty search it shows the current screen's visible actions plus
Help, Projects, Settings, Theme, and Quit. Closing it with **Esc** restores the
exact screen and focused control from which it opened.

The common vocabulary is consistent throughout the application:

- **Tab** and **Shift+Tab** move focus between controls;
- **Enter** activates the focused control;
- arrow keys navigate lists, tables, trees, and text;
- **Esc** goes back once or safely cancels a modal;
- **Ctrl+P** opens the command menu from anywhere;
- **Ctrl+Q** exits from every screen, with the same unsaved-settings guard as
  the Menu's Quit action;
- **Ctrl+N**, **Ctrl+O**, and **Ctrl+R** provide New, Open, and Refresh/Retry
  where the footer shows them;
- **Ctrl+S** saves settings; and
- **Ctrl+A**, followed by the terminal's copy command, selects and copies a
  focused text pane.

Help, Projects, Settings, Theme, Quit, and less frequent screen actions are in
**Menu**. Proof Assistant intentionally avoids function keys, unmodified letter
commands, bracket navigation, `Ctrl+Enter`, and Vim/Emacs command conventions.

On the welcome screen, the first resumable project receives focus after the
catalog loads, so **Enter** opens it directly. **Ctrl+O** opens the project in the
focused row, or the most recently active resumable project when focus is
elsewhere.

Global menu navigation is cancel-first on a modal dialog: dismiss the modal
with **Esc**, then choose the destination from **Menu**. Settings is an overlay,
so closing it restores the exact prior screen. Leaving a progress screen
detaches only the TUI observer; the detached backend job continues.

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

- **Use default task** — verify every indexed proof-bearing theorem-like
  assertion under its stated assumptions; or
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
with raw Lean and AI-provider logs.

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

Cancellation is cooperative, so an active AI/Lean batch is allowed to stop
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
- skipped conjecture or assertion with no attached manuscript proof;
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

When the backend marks an infrastructure incident retryable, the failure screen
enables **Retry verification** and starts a new durable run while retaining the
failed run's evidence. Formalization failures that require manuscript or proof
changes do not enable blind retry.

Several claims may share one prerequisite. The tree marks a repeated occurrence
as a shared reference and does not expand it indefinitely. If the manuscript
contains an actual dependency cycle, the map automatically changes to a finite
**Cycle components** table plus the component edge list. This fallback is used
only for a detected cycle; ordinary acyclic manuscripts retain the more natural
tree presentation.

Copyability is a TUI-wide rule. Paths, candidate main files, commands,
progress values, findings, warnings, errors, and source excerpts appear in
read-only selectable panes. Rich syntax and Markdown views remain selectable
without duplicating the same content in a second pane.

## When clarification is needed

The clarification screen displays:

- a compact **Best current guess** banner above the source and diagnosis panes;
- the affected claim and question category;
- the actual relative and absolute input-file path;
- a line-numbered, syntax-highlighted LaTeX excerpt;
- highlighted source lines requiring attention;
- the immutable observed reason verification stopped;
- the question origin: proof worker, host policy, or legacy/unknown;
- the AI-assisted hypothesis, confidence, evidence-backed reasoning,
  alternatives, uncertainties, and recommended author check;
- the provider, model, and reasoning effort used for that analysis;
- possible resolutions; and
- the blocked portion of the proof tree.

The deterministic backend chooses the source file, byte/line span, quotation,
claim, observed problem, diagnostics, and affected graph. For a newly generated
clarification it also builds a deterministic evidence packet containing the
question, full relevant claims/proofs, direct dependency endpoints and paths,
certificates, diagnostics, and failure artifacts. An isolated, tool-free AI
analysis turn uses the exact diagnostic-role provider/model/effort frozen into
that verification job; optional author-facing narration separately uses the
frozen clarification role. Every factual reasoning statement must cite an
evidence ID. The resulting hypothesis is advisory: it is not a Lean result,
proof, certificate, or confirmed statement of author intent.

Presentation and analysis output are schema- and provenance-validated before
they are displayed or loaded. Unknown evidence IDs, mismatched evidence hashes,
and malformed stored rows are rejected. In that case the screen preserves the
authoritative observed problem and says that the best current guess is
unavailable instead of inventing one.

The exact affected segment stays on the clarification screen as read-only,
line-numbered, syntax-highlighted LaTeX, with the relevant lines emphasized.
Edit it with your normal tools, then choose **Check all files for changes**.
**Esc** returns to the project menu. **Open source folder** remains available
for desktop workflows. For manuscripts with `\input` or `\include`, edit the
exact file shown, not necessarily the root `main.tex`. Proof Assistant never
rewrites the manuscript itself.

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

Resume is cache-only for clarification analysis: it validates and displays the
analysis already bound to that question and evidence, but never starts a paid
AI turn merely because the project was opened. A historical clarification that
predates stored analysis therefore shows **Best current guess unavailable**.
If the external manuscript changed, Change Review takes precedence so the user
reviews the actual new source before returning to or superseding the question.

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

## AI provider settings

Open **Menu** (or press **Ctrl+P**), choose **Settings**, then open
**Verification AI**. This destination is split into
**Role assignments**, **Connections & credentials**, and **Provider
diagnostics**, so the complete role team is not buried below connection and
diagnostic controls. Connections & credentials shows machine-owned
installation/authentication state, credential source, and provider fallback.
Provider diagnostics shows exact catalog provenance and resolved task-policy
details. The connection view can review a missing CLI's exact user-local
install plan, show copyable native login instructions, and submit an API key
once to the OS keyring. It never reads a provider auth file or displays a
stored key.

Automatic model discovery is live for Codex and the three API drivers when the
configured account is reachable. Claude and Copilot CLI catalogs are explicitly
labeled curated aliases because those CLIs do not document a noninteractive
account model-list command. Copilot's authentication check also remains
unknown until the user explicitly approves one tiny no-tools request; normal
startup and refresh never send that request.

Provider connections and credentials remain machine-owned. In **Role
assignments**, an explicit scope switch selects **Machine defaults** or **This
project**. Both scopes show all eight roles with separate model and reasoning
effort values: author clarification, scan / triage diagnostics, primary prove,
sketch, maintain / fix, math and engineering review, independent prove, and
progress / reporting. The provider selector and **Use recommended _provider_
defaults for all 8 roles** button are together above the roster. Changing the
provider immediately removes the previous provider's rows and loads a complete
recommended matrix for the new provider. Neutral placeholders remain visible
while it loads, Save stays disabled, and stale or foreign model names never
appear under the selected provider. The defaults button resets later
same-provider customization; **Undo defaults** restores only that provider's
previous complete draft. Role model and effort choices are limited to the
selected provider's capabilities.
In project scope, **Use machine defaults** removes the project override. The
complete role map affects future submissions only and is frozen into each job,
including the diagnostic and clarification roles. A selected driver still
shares the same global AI admission controller with all other AI task classes. See
[AI providers and first-time setup](AI_PROVIDERS.md) for commands, credential
handling, model policy, and execution isolation.

## Concurrency and machine resources

Open **Settings** from the welcome screen or a project dashboard, then choose
**Concurrency / Resources**. Proof Assistant controls active AI turns, Lean
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

Concurrency and resource settings apply to every project on this machine. The
project-specific AI override described above covers only provider, model, and
difficulty; it does not create independent per-project resource limits. See
[Concurrency and resource management](CONCURRENCY.md) for formulas,
precedence, CLI/environment overrides, pressure behavior, benchmarks,
provenance, and the upstream RepoProver migration map.

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
  --ai-driver codex_cli \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

Two logical proof-batch workers are the legacy default. `--jobs` controls batch
process fan-out, not active provider turns: the machine AI controller is
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
