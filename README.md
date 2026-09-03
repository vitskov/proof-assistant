# Proof Assistant

Proof Assistant is an interactive proof-checker for mathematics manuscripts typeset in LaTeX. Point
it at a manuscript folder, describe what should be checked, and it builds a
persistent Lean verification project. When the manuscript changes, it preserves
valid work and revisits only the affected part of the proof graph.

The normal interface is a terminal application that can run locally on macOS or
Linux, or remotely over SSH. On macOS, [iTerm2](https://iterm2.com/downloads.html)
is recommended for a more responsive interface.

At its core, Proof Assistant is powered by
[RepoProver](https://github.com/facebookresearch/repoprover), the multi-agent
Lean formalization framework developed by researchers at FAIR, Meta, for the
[Automatic Textbook Formalization](https://github.com/facebookresearch/repoprover/blob/main/auto_textbook_formalization.pdf)
project. RepoProver supplies the central proof-agent scaffolding and Lean tools;
Proof Assistant builds an interactive, incremental manuscript-verification
workflow around them.

## Quick links

1. [What is it for?](#what-it-is-for)
2. [Installation](#installation)
3. [Batteries not included](#batteries-not-included)

## What it is for

- Check all proof-bearing theorem-like claims.
- Distinguish verified, ambiguous, unresolved, suspected-false, technical
  failure, and kernel-checked counterexample outcomes.
- Explain an incomplete check with its first deterministic blocking reason and
  an interactive, copyable proof-dependency map.
- Preserve independently checked Lean certificates between manuscript edits.
- Show how one changed statement affects its dependent lemmas and theorems.
- Auto-tune AI turns, Lean checks, and Lake builds independently for the
  machine, with effective limits and pressure visible in the TUI.
- Keep source snapshots, reports, questions, Lean code, and history together in
  one durable project.

Lean decides whether formal evidence is accepted. Certification requires an
independently built declaration with a proof body, recorded source and
environment provenance.

## Installation

Proof Assistant supports macOS 12+ on Intel or Apple Silicon and Linux with
glibc 2.31+. Git, `curl`, and a native C compiler are required.

```bash
bash -c 'set -o pipefail; curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/vitskov/proof-assistant/main/install.sh | bash'
```

Open a new terminal and launch:

```bash
proof-assistant
```

The installer handles Python, `uv`, elan, Lean/Lake, the tested RepoProver
checkout, compiler validation, cache initialization, tests, and shell PATH
setup on both supported operating systems. It preserves existing shell
configuration. It also ensures that one basic terminal editor is available,
checking `nano`, `pico`, then `micro` and attempting installation in that order
only when all three are absent. See
[Installation](docs/INSTALLATION.md) for requirements, locations, and the few
supported overrides.

## Batteries not included

Proof Assistant does not include access to an AI provider. Effective formal
verification needs a capable paid AI plan with enough usage for sustained proof
work. In practice, the two most common choices are paid plans from Anthropic
and OpenAI; depending on how you connect, usage may come from a provider
subscription or a separately billed API account.

For eligible researchers, Anthropic's limited
[Claude Team plan for scientists](https://claude.com/programs/team-plan-for-scientists#pricing)
is an example of a heavily subsidized plan for verified academics and nonprofit
research groups.  

## Configure verification AI

On first launch, Proof Assistant checks its machine-wide primary AI driver. If
it is not ready, **Set up verification AI** opens a focused **Choose provider →
Connect provider → Review eight-role team** flow. You can inspect or install a
CLI, copy its native login step, or choose an API credential source. A CLI
install requires review and explicit approval, and the Copilot quota-consuming
account probe is never sent without separate consent.

Later, open **Menu** (or press **Ctrl+P**), choose **Settings → Verification
AI**, and move among **Role assignments**, **Provider connection**, and
**Provider diagnostics**. Role assignments shows the complete eight-role
verification team with a model and reasoning effort for each role. Its scope
switch makes **Machine defaults** and **This project** explicit. **Apply
provider defaults** fills all eight assignments for the selected provider, and
**Undo defaults** restores the previous draft before you save. When the
installed Claude Code CLI supports it, Claude defaults use Fable with
extra-high effort for both **Author clarification** and the **Independent prove
agent**. Credentials remain machine-owned, and each submitted job freezes its
resolved role map.
See
[AI providers and first-time setup](docs/AI_PROVIDERS.md).

## Create or resume a project

The welcome screen then offers **New project** and **Resume project**. A new
project defaults to:

```text
$HOME/proof-assistant/<project-name>
```

Every screen keeps its current keyboard commands in a compact footer. Open the
visible **Menu** control or press **Ctrl+P** for a searchable list containing
the current screen's actions plus Help, Projects, Settings, Theme, and Quit.
Use **Tab** or **Shift+Tab** to move focus, arrow keys within lists and text,
**Enter** to activate the focused control, **Esc** to go back once, and
**Ctrl+S** to save settings. Common project actions also use **Ctrl+N**,
**Ctrl+O**, and **Ctrl+R** where shown. Proof Assistant does not require
function keys, unmodified letter commands, or Vim/Emacs command conventions.

The project list is reconciled by the backend. Resumable projects have a
**Resume** action and a guarded **Delete project** action; older projects
without an unambiguous manuscript root have a **Select main file** action;
incomplete projects and occupied directories remain visible with their
diagnostic instead of being silently omitted. Deletion uses a cancel-first
confirmation dialog with a separate destructive button, is refused while a
backend verification is active, and moves only the managed project to a
recoverable location. The authoritative manuscript source is never moved or
changed.

After the project list loads, its first resumable project is focused: press
**Enter** to open it. **Ctrl+O** opens the focused project, with the most
recently active resumable project as the fallback.

## Select a manuscript and task

Select an existing folder containing the LaTeX source, then choose **Continue:
inspect source**. **Browse folders** opens an SSH-safe terminal picker with
copyable paths and explicit **Up**, **Home**, **Select**, and **Cancel**
controls; a path can still be typed directly. Only an explicit selection saves
the last manuscript folder in the machine-local
`~/.config/proof-assistant/preferences.json` (or safe XDG equivalent), outside
managed projects and Dropbox. Proof Assistant establishes one explicit manuscript root
before it creates anything. With one LaTeX file it shows that file as the
automatically selected main file; with several, it lists them and requires you
to select one. A final review screen shows the choice and requires confirmation
before project creation. That main file may recursively `\input` or `\include`
other files.
Only this resolved source closure is interpreted as the manuscript, so an
alternate paper or orphaned draft in the same folder is not silently verified.

The source may be in Dropbox: Proof Assistant warns you, waits for a stable
multi-file snapshot, and copies the files into its managed project. The managed
project, Python environment, and Lean cache must remain outside Dropbox.

Choose **Use default task** to check every theorem-like claim, or **Customize
task** to edit the request inside the TUI. The resulting task is owned and
versioned by the project; there is no external task file to create or maintain.

## The feedback loop

```text
external manuscript
        |
        | stable, reviewed import
        v
managed Git snapshot -> claim/dependency graph -> Lean verification
        ^                                              |
        |                                              v
user edits exact requested source <- clarification or findings
```

If no author action is needed, the TUI finishes with a human-readable findings
screen and tells you where the full evidence is stored. **View report in
terminal** opens a scrollable, rendered Markdown viewer with a table of contents
and a separate selectable source tab, so it works over SSH without a graphical
file opener. For an incomplete or technically failed check, **Inspect failure
dependencies** opens a terminal-native proof tree: direct failures are red,
blocked dependents are yellow, certified nodes are green, and selecting a node
shows its exact persisted reason and supporting log paths. A copyable text
outline is always available. The uncommon case of an actual dependency cycle
uses a cycle-safe component/edge view instead of pretending that the graph is a
tree. If clarification is needed, the TUI identifies the actual input
file and source lines and shows an evidence-grounded **Best current guess** for
why verification stopped. The hypothesis includes confidence, cited evidence,
alternatives, uncertainty, the recommended author check, and the exact
provider/model/effort used. It is clearly separated from the immutable observed
problem and is never presented as a Lean result or confirmed author intent.
Proof Assistant then waits while you edit the original source folder, detects
all stable changes, previews their proof-graph impact, and starts the next
iteration only after your explicit confirmation.

While verification runs, the progress screen names the selected main file and
every resolved input, explains the current preparation/proof/certification
stage, and keeps read-only source, stage, and event panes whose text can be
selected and copied. Verification runs in a detached backend worker: closing
the TUI closes only that client view. Reopening Proof Assistant attaches to the
same job and replays its durable progress; cooperative cancellation is also a
backend request, not a TUI-process signal.

Informational values throughout the TUI—including paths, candidate files,
commands, progress, findings, warnings, and errors—are exposed in selectable
read-only text. Syntax-highlighted or rendered views have an adjacent copyable
source representation.

## Help topics

| Topic | Guide |
|---|---|
| install or upgrade | [Installation](docs/INSTALLATION.md) |
| start, resume, clarify, and review | [Usage guide](docs/USAGE.md) |
| understand the project-owned verification task | [Task and scope](docs/TASK_FILES.md) |
| understand snapshots, graphs, and certificates | [Incremental verification](docs/INCREMENTAL_VERIFICATION.md) |
| connect Codex, Claude, Copilot, OpenAI, Anthropic, or Gemini | [AI providers and setup](docs/AI_PROVIDERS.md) |
| tune AI, Lean, and build resources | [Concurrency and resources](docs/CONCURRENCY.md) |
| use advanced command-line operations | [Command reference](docs/COMMAND_REFERENCE.md) |
| diagnose a quiet or failed run | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| understand disk use and cleanup | [Cache and storage](docs/CACHE_AND_STORAGE.md) |
| review component contracts and security | [Architecture](docs/ARCHITECTURE.md) |
| develop or release the package | [Development and testing](docs/DEVELOPMENT.md) |
| inspect exact validation evidence | [Test report](TEST_REPORT.md) |

> **If you are an AI agent:** read [Working on Proof Assistant as an AI
> agent](docs/AI_AGENTS.md) before modifying files or starting a run.

## Storage and compatibility

The product, Python distribution, import package, executable, and repository are
named **Proof Assistant** / `proof-assistant` / `proof_assistant`. Version 0.1.0
starts the new product version line. `repoprover-codex` remains a deprecated
command alias during the 0.1 line so existing scripts have a migration window.

The cache deliberately remains at:

```text
$HOME/.cache/repoprover-codex
```

Reusing that exact location prevents existing installations from creating a
second multi-gigabyte Mathlib dependency depot after the rename.

CLI authentication remains inside each provider's native CLI. API keys are
read only from the selected environment variable or OS keyring; they are never
stored in a project or provider settings file. Proof Assistant never reads
provider auth files or extracts login tokens. Provider tools remain constrained
to the Proof Assistant host boundary described in
[AI providers and first-time setup](docs/AI_PROVIDERS.md#execution-and-security-boundary).
This independently maintained project does not modify or publish to
`facebookresearch/repoprover`.

Proof Assistant is distributed under the Creative Commons
Attribution-NonCommercial 4.0 International license; see [LICENSE](LICENSE).
