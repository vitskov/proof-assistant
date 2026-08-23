# RepoProver Codex

Use a locally authenticated Codex CLI to investigate mathematical claims in a
LaTeX manuscript and retain the resulting Lean evidence, reports, logs, and Git
history in an output folder you control.

RepoProver Codex is for authors and researchers who want more than a prose
review. It gives Codex the formal tools exposed by
[`facebookresearch/repoprover`](https://github.com/facebookresearch/repoprover),
lets it formalize and test manuscript claims in Lean, and then independently
builds the final Lean project before reporting success.

## What it does

Given a manuscript folder, a plain-text task, and an empty output folder, the
package:

1. copies the manuscript into an isolated Git workspace without modifying the
   original;
2. runs one RepoProver agent through the existing Codex login, with external
   MCP servers, apps, plugins, and skills disabled for that child process;
3. records the formalization, tool calls, logs, report, and commits; and
4. requires independent Lean evidence before classifying a run as verified.

The tool distinguishes verified, unverified, incomplete, setup failure, and
tool failure outcomes. Failure to verify a claim is never presented as proof
that the claim is false.

A successful Lean build proves the generated formal statements, not by itself
that those statements faithfully encode the manuscript's prose. Review the
report's claim-to-formalization mapping and assumptions before relying on the
result.

## Quick start

Requirements: macOS or Linux, Python 3.13, `uv`, Git, Lean/Lake, a native C
compiler, and a Codex CLI session authenticated with `codex login`.

```bash
git clone https://github.com/vitskov/repoprover-codex.git "$HOME/src/repoprover-codex"
cd "$HOME/src/repoprover-codex"
scripts/install-dev.sh
export PATH="$HOME/.venvs/repoprover-codex/bin:$PATH"

git clone https://github.com/facebookresearch/repoprover.git "$HOME/src/repoprover"
git -C "$HOME/src/repoprover" checkout 386adba3df572cb71df534add2c764e071898a2e
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" \
  -e "$HOME/src/repoprover"
```

The installer creates the Python environment and Lean cache outside Dropbox,
compiles and executes a native test program, and runs the complete package test
suite. RepoProver is installed separately from its pinned source checkout; it
is not modified by RepoProver Codex.

Check the installation:

```bash
repoprover-codex doctor
repoprover-codex models
repoprover-codex cache doctor
```

Choose your three paths and run a verification:

```bash
export MANUSCRIPT=/absolute/path/to/manuscript
export TASK="$HOME/repoprover-tasks/check-manuscript.md"
export OUTPUT="$HOME/repoprover-runs/manuscript-001"

repoprover-codex manuscript-run \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --output "$OUTPUT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

`$TASK` is a UTF-8 text or Markdown file containing the complete free-form
verification request. Start from [the example task](examples/verify-task.md) or
write your own. `$OUTPUT` must be new or empty and outside Dropbox.

When the command finishes, begin with:

- `$OUTPUT/VERIFICATION_REPORT.md` for the human-readable findings;
- `$OUTPUT/RUN_STATUS.json` and `$OUTPUT/artifacts/result.json` for the outcome;
- `$OUTPUT/workspace/` for the manuscript snapshot, Lean source, and Git
  history; and
- `$OUTPUT/artifacts/` for setup, tool-call, event, and final-build evidence.

## Help topics

| If you want to… | Read… |
| --- | --- |
| install, verify, or upgrade the package | [Installation](docs/INSTALLATION.md) |
| run a manuscript task from start to finish | [Usage guide](docs/USAGE.md) |
| understand commands and timeout options | [Command reference](docs/COMMAND_REFERENCE.md) |
| interpret outcomes and output files | [Usage: results and evidence](docs/USAGE.md#results-and-evidence) |
| monitor a run or diagnose a failure | [Troubleshooting and operations](docs/TROUBLESHOOTING.md) |
| understand or clean disk usage | [Cache and storage](docs/CACHE_AND_STORAGE.md) |
| understand authentication and isolation | [Architecture and security](docs/ARCHITECTURE.md) |
| develop, test, or release the package | [Development and testing](docs/DEVELOPMENT.md) |
| see the exact tested versions and evidence | [Test report](TEST_REPORT.md) |
| navigate all documentation | [Documentation index](docs/README.md) |

> **If you are an AI agent:** read [Working on RepoProver Codex as an AI
> agent](docs/AI_AGENTS.md) before running commands or changing files.

## Safety and storage

Authentication remains inside the Codex CLI. This package does not read Codex
authentication files, extract OAuth tokens, or require an `OPENAI_API_KEY`.
This is not an offline verifier: manuscript context and tool results needed for
the task are processed through the authenticated Codex service. Apply the same
confidentiality policy you would apply to other Codex CLI work.

Large compatible Mathlib/REPL dependencies are shared rather than copied for
every manuscript. Transactional reservations, active-process leases, a
coarse-grained accounting index, and deadline-bounded garbage collection keep
managed storage predictable. The default cache is
`$HOME/.cache/repoprover-codex`; Python environments and Lean caches are
rejected if they resolve into Dropbox.

## Project status

Version 0.4.0 is tested on macOS with Python 3.13, Lean 4.28, Lake 5, and the
pinned RepoProver checkout recorded in [TEST_REPORT.md](TEST_REPORT.md). Linux
is supported by the implementation but was not exercised in the latest local
acceptance pass.

This is an independently maintained project. No pull request, issue, or push
has been made to `facebookresearch/repoprover`.
