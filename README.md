# Proof Assistant

Proof Assistant is an interactive formal referee for LaTeX mathematics. Point
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
- Preserve independently checked Lean certificates between manuscript edits.
- Show how one changed statement affects its dependent lemmas and theorems.
- Keep source snapshots, reports, questions, Lean code, and history together in
  one durable project.

Lean—not the language model—decides whether formal evidence is accepted.
Certification requires an independently built declaration with a proof body,
recorded source and environment provenance, and no `sorry`, `admit`, or newly
introduced project axiom.

## Quick start

Requirements: macOS or Linux, Python 3.13, `uv`, Git, Lean/Lake, a native C
compiler, an authenticated Codex CLI, and the tested RepoProver checkout. See
[Installation](docs/INSTALLATION.md) for details.

```bash
git clone https://github.com/vitskov/proof-assistant.git "$HOME/src/proof-assistant"
cd "$HOME/src/proof-assistant"
scripts/install-dev.sh
export PATH="$HOME/.venvs/proof-assistant/bin:$PATH"
proof-assistant
```

The welcome screen offers **New project** and **Resume project**. A new project
defaults to:

```text
$HOME/proof-assistant/<project-name>
```

Select an existing folder containing the LaTeX source. The source may be in
Dropbox: Proof Assistant warns you, waits for a stable multi-file snapshot, and
copies the files into its managed project. The managed project, Python
environment, and Lean cache must remain outside Dropbox.

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
screen and tells you where the full evidence is stored. If clarification is
needed, it identifies the actual input file and source lines, explains the
question, and waits while you edit the original source folder. It then detects
all stable changes, previews their proof-graph impact, and starts the next
iteration only after your explicit confirmation.

## Help topics

| Topic | Guide |
|---|---|
| install or upgrade | [Installation](docs/INSTALLATION.md) |
| start, resume, clarify, and review | [Usage guide](docs/USAGE.md) |
| understand the project-owned verification task | [Task and scope](docs/TASK_FILES.md) |
| understand snapshots, graphs, and certificates | [Incremental verification](docs/INCREMENTAL_VERIFICATION.md) |
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

Authentication remains inside Codex CLI. Proof Assistant never reads
`auth.json` or extracts login tokens. Verification children start with local
MCP servers, apps, plugins, and skills disabled and verified absent. This
independently maintained project does not modify or publish to
`facebookresearch/repoprover`.
