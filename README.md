# RepoProver Codex

RepoProver Codex is an incremental formal referee for mathematical manuscripts.
It gives an isolated Codex agent RepoProver’s LaTeX, Lean, Mathlib, file, and Git
tools, then lets Lean—not the agent—decide which results are certified.

The author keeps editing the original LaTeX folder. A separate verification
project preserves Lean proofs, immutable source snapshots, dependencies,
clarification questions, reports, and certification history across passes.
When one statement changes, only that claim and the claims that depend on it
are reconsidered.

## Why use it?

- Verify one named result or systematically work through a manuscript.
- Pause on a genuine ambiguity, edit the manuscript, and run the same command
  again without discarding completed proofs.
- Reuse a certified theorem when prose changes but the reviewed Lean statement
  remains structurally identical.
- Audit dependencies used by a Lean proof but left implicit in the manuscript.
- Keep “not proved,” “ambiguous,” “technical failure,” “likely false,” and
  “kernel-checked counterexample” as distinct outcomes.

Certification requires a mapped Lean declaration with a proof/value body, no
`sorry` or `admit`, no newly introduced project axiom, a successful independent
project build, and recorded source/environment provenance. A successful Lean
proof certifies the formal statement; you should still review whether that
statement faithfully represents the manuscript.

## Quick start

Requirements: macOS or Linux, Python 3.13, `uv`, Git, Lean/Lake, a native C
compiler, an authenticated Codex CLI, and the tested RepoProver checkout. See
[Installation](docs/INSTALLATION.md) for the complete setup.

```bash
git clone https://github.com/vitskov/repoprover-codex.git "$HOME/src/repoprover-codex"
cd "$HOME/src/repoprover-codex"
scripts/install-dev.sh
export PATH="$HOME/.venvs/repoprover-codex/bin:$PATH"
```

Choose three paths. The manuscript and task remain authoritative; the project
is durable verification state and must be outside Dropbox.

```bash
export MANUSCRIPT=/absolute/path/to/latex-manuscript
export TASK="$HOME/repoprover-tasks/check-manuscript.md"
export PROJECT="$HOME/repoprover-projects/my-paper"

repoprover-codex manuscript init \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --project "$PROJECT"

repoprover-codex manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

Use a Markdown/text task for a free-form request, or `VERIFY.yaml` for explicit
targets, theorem-vs-argument-audit mode, and policy controls. The same `verify`
command snapshots current manuscript edits and resumes from the minimal
uncertified dependency frontier.

Check progress at any time, including during a long verification:

```bash
repoprover-codex manuscript status --project "$PROJECT"
repoprover-codex manuscript questions --project "$PROJECT"
```

Start with `$PROJECT/VERIFICATION_STATUS.md` for the current state,
`$PROJECT/CLARIFICATION_REQUEST.md` for author questions, and
`$PROJECT/VERIFICATION_REPORT.md` for the evidence and dependency audit. Lean
source stays under `$PROJECT/Formalization/`; deterministic state and per-run
artifacts stay under `$PROJECT/.repoprover/`.

## How the feedback loop works

```text
author manuscript -> immutable Git snapshot -> structural claim graph
                                               |
                                               v
certified Lean code <- independent build <- ready claim frontier
       |                                       |
       +------- reuse unaffected results       +-> clarification -> author edit
```

Source identity, graph propagation, cache validity, state transitions, and
certification are deterministic software operations. AI is used only for
semantic interpretation, correspondence proposals, diagnostics, and proof
search. Persistent correctness never depends on an old model conversation.

## Help topics

| Topic | Guide |
|---|---|
| install or upgrade | [Installation](docs/INSTALLATION.md) |
| run, pause, edit, and resume | [Usage guide](docs/USAGE.md) |
| write a free-form or YAML task | [Task files](docs/TASK_FILES.md) |
| understand snapshots, graphs, and certificates | [Incremental verification](docs/INCREMENTAL_VERIFICATION.md) |
| look up every command and exit code | [Command reference](docs/COMMAND_REFERENCE.md) |
| diagnose a quiet or failed run | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| understand disk use and cleanup | [Cache and storage](docs/CACHE_AND_STORAGE.md) |
| review security and Codex isolation | [Architecture and security](docs/ARCHITECTURE.md) |
| develop or release the package | [Development and testing](docs/DEVELOPMENT.md) |
| inspect exact tested versions | [Test report](TEST_REPORT.md) |

> **If you are an AI agent:** read [Working on RepoProver Codex as an AI
> agent](docs/AI_AGENTS.md) before changing files or starting a run.

## Safety and project status

Authentication remains inside the Codex CLI. The package does not read
`auth.json`, extract login tokens, or require an API key. It is not offline:
manuscript context and tool results needed for a task are processed through the
authenticated Codex service. Each RepoProver child starts with MCP servers,
apps, plugins, bundled skills, and local skills disabled and verified absent.

Compatible Mathlib/REPL dependencies share one managed depot. Python
environments, persistent verification projects, and Lean caches are rejected
when they resolve into Dropbox. The installer always compiles and executes a
native program before running tests.

Version 0.4.1 is tested on macOS with Python 3.13, Lean 4.28, Lake 5, Codex CLI
0.149.0, and RepoProver commit
`386adba3df572cb71df534add2c764e071898a2e`. This independently maintained
project does not modify or publish to `facebookresearch/repoprover`.
