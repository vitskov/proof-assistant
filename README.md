# Proof Assistant

Proof Assistant is an interactive formal referee for mathematics manuscripts typeset in LaTeX. Point
it at a manuscript folder, describe what should be checked, and it builds a
persistent Lean verification project. When the manuscript changes, it preserves
valid work and revisits only the affected part of the proof graph.

The normal interface is a terminal application. It guides you through starting
or resuming a project, shows verification progress and findings, and presents
clarification requests beside the exact highlighted LaTeX source that needs
attention.

## What it is for

- Check all theorem-like claims, or customize the project task in a simple text
  editor.
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

Lean—not the language model—decides whether formal evidence is accepted.
Certification requires an independently built declaration with a proof body,
recorded source and environment provenance, and no `sorry`, `admit`, or newly
introduced project axiom.

## Quick start

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
configuration and never uses `sudo` or a system package manager. See
[Installation](docs/INSTALLATION.md) for requirements, locations, and the few
supported overrides.

On first launch, Proof Assistant checks its machine-wide primary AI driver. If
it is not ready, **Set up verification AI** opens a focused **Choose provider →
Connect provider → Review eight-role team** flow. You can inspect or install a
CLI, copy its native login step, or choose an API credential source. A CLI
install requires review and explicit approval, and the Copilot quota-consuming
account probe is never sent without separate consent.

Later, press **F3**, choose **Verification AI**, and move among **Role
assignments**, **Provider connection**, and **Provider diagnostics**. Role
assignments shows the complete eight-role verification team with a model and
reasoning effort for each role. Its scope switch makes **Machine defaults** and
**This project** explicit. **Apply provider defaults** fills all eight
assignments for the selected provider, and **Undo defaults** restores the
previous draft before you save. Claude Code defaults use Fable with extra-high
effort for the **Independent prove agent** when the installed CLI advertises
that capability. Credentials remain machine-owned, and each submitted job
freezes its resolved role map.
See
[AI providers and first-time setup](docs/AI_PROVIDERS.md).

The welcome screen then offers **New project** and **Resume project**. A new
project defaults to:

```text
$HOME/proof-assistant/<project-name>
```

Every screen keeps its current keyboard commands in a compact footer. Press
**F1** for the complete command reference, **F2** for the main menu, **F3** for
machine settings, **Ctrl+P** for the searchable command palette, and **Ctrl+T**
to switch between the warm **Proof Ink** dark
theme and **Proof Paper** light theme. Setup and settings screens consistently
use **Esc** to go back, **Ctrl+Enter** to continue, and **Ctrl+S** to save when
those actions are available.

The project list is reconciled by the backend. Resumable projects have a
**Resume** action and a guarded **Delete project** action; older projects
without an unambiguous manuscript root have a **Select main file** action;
incomplete projects and occupied directories remain visible with their
diagnostic instead of being silently omitted. Deletion uses a cancel-first
confirmation dialog with a separate destructive button, is refused while a
backend verification is active, and moves only the managed project to a
recoverable location. The authoritative manuscript source is never moved or
changed.

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
file and source lines, explains the question, and waits while you edit the
original source folder. It then detects all stable changes, previews their
proof-graph impact, and starts the next iteration only after your explicit
confirmation.

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
