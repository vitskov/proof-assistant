# CODEX HANDOFF — repoprover-codex internal development

## Mission

Continue development and live testing of `repoprover-codex`, a standalone Codex CLI / `codex app-server` backend for `facebookresearch/repoprover`.

This is INTERNAL TESTING ONLY.

**DO NOT open a pull request to `facebookresearch/repoprover`.**
**DO NOT push changes upstream.**
**DO NOT create or modify upstream issues.**

The user's standing environment rules are non-negotiable:

- use Python 3.13 for this work;
- never create or store Python environments in Dropbox;
- use `uv` whenever feasible for Python environment, dependency, and build work;
- never create or store Lean caches in Dropbox; and
- every package install script must run a real compiler compile-and-execute
  preflight, not merely check whether a compiler command exists.

You may modify this local package, run tests, add tests, clone RepoProver for inspection/testing, create toy Lean projects, and make local Git commits.

The target machine is expected to already have Codex CLI installed and authenticated through the user's ChatGPT Pro plan.

## Architecture that must be preserved

```text
RepoProver
   |
   v
repoprover-codex
   |
   v
persistent `codex app-server`
   |
   v
existing Codex CLI / ChatGPT authentication
```

Authentication must stay inside Codex. Do NOT extract OAuth tokens from `~/.codex/auth.json`, do NOT turn a ChatGPT credential into `OPENAI_API_KEY`, and do NOT require a billable API key.

RepoProver already exposes OpenAI-style function tools and dispatches them through `handle_tool_call()`. Codex app-server supports client-defined dynamic tools. The intended bridge is:

```text
RepoProver get_tools()
      -> schema translation
      -> Codex dynamicTools
      -> item/tool/call
      -> RepoProver handle_tool_call()
      -> lean_check / file / mathlib / git tools
```

Use one persistent Codex thread per logical RepoProver agent/session where practical. Do not use `codex exec` as the normal provider backend.

## Exact model and reasoning effort

The integration must support explicit exact model and reasoning-effort selection. Query `model/list`, validate the requested pair against the local catalog, then send model and effort explicitly in the app-server request/turn. Do not assume every raw OpenAI API model/effort is available through Codex.

## Linux AND macOS support

The standalone package should support both Linux and macOS local execution.

The current implementation intentionally uses portable Python (`pathlib`, `subprocess.Popen`, stdio JSONL) and no Linux-only syscalls. Codex CLI supports both Linux and macOS, including Apple Silicon.

For macOS acceptance testing, require:

```bash
python3 --version
codex --version
repoprover-codex doctor
repoprover-codex models
pytest -q
lake --version
lean --version
```

Then run the same dynamic-tool smoke test and one real RepoProver + Lean single-agent proof.

Do NOT require SLURM/distributed RepoProver execution on macOS. RepoProver's HPC/distributed paths are Linux-oriented; local mode is the macOS target. Avoid GNU-specific shell assumptions and hard-coded `/home/...` paths.

## Current files

- `src/repoprover_codex/protocol.py` — bidirectional JSONL stdio client for app-server.
- `src/repoprover_codex/tools.py` — RepoProver/OpenAI function tools -> Codex dynamic tools.
- `src/repoprover_codex/models.py` — model catalog and effort validation.
- `src/repoprover_codex/backend.py` — initialize/model-list/thread/turn/tool callbacks/event completion.
- `src/repoprover_codex/integration.py` — narrow adapter for an already-created RepoProver agent.
- `src/repoprover_codex/cli.py` — `doctor`, `models`, `smoke`, `repoprover-prove`.
- `tests/` — unit tests, fake backend, bidirectional stdio simulator.
- `docs/INTERNAL_TESTING.md` — staged testing notes.

## Verified before handoff

At packaging time the internal test suite passed with **10 tests**. The package also built as a wheel, installed in the packaging environment, imported as version `0.1.0`, and exposed the four CLI commands above.

A real Codex call was NOT run in the packaging environment because that environment had no `codex` executable. The first major task on this machine is therefore to test the real installed Codex app-server.

# Required work

Work through these stages in order. Fix problems autonomously rather than merely reporting them.

## Stage 0 — inspect environment

Run:

```bash
pwd
uname -a
python3 --version
which python3
codex --version
which codex
git --version
```

On macOS also run:

```bash
sw_vers
uname -m
```

On Linux record `/etc/os-release` if available. Never print auth tokens or private keys.

## Stage 1 — local package baseline

If this unpacked directory is not a Git repo, initialize it locally. Do not add an upstream remote.

```bash
git init -b main   # only if needed
git add -A
git commit -m "Import repoprover-codex internal handoff"   # only if needed
```

Create an isolated Python 3.13 environment outside Dropbox using `uv`:

```bash
uv venv --python 3.13 "$HOME/.venvs/repoprover-codex"
source "$HOME/.venvs/repoprover-codex/bin/activate"
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" -e '.[dev]'
repoprover-codex compiler-check
pytest -q
```

All existing tests should pass. If packaging differs on the real machine, repair the package rather than relying on an ad-hoc environment workaround.

## Stage 2 — real app-server connectivity

Run:

```bash
repoprover-codex doctor
```

Success criteria:
1. `codex app-server` launches;
2. initialize succeeds;
3. `model/list` succeeds;
4. at least one model is returned.

If the installed Codex protocol differs from the code assumptions, inspect the installed Codex version/docs/source and update the bridge to the real schema. Add regression tests for every protocol difference found. Prefer structured protocol fixes over sleeps or stdout scraping.

## Stage 3 — exact model catalog

Run:

```bash
repoprover-codex models
```

Record exact model IDs and supported effort levels. Use only an advertised pair for subsequent tests. If the response shape differs from `models.py`, make parsing robust and add tests.

## Stage 4 — dynamic-tool smoke test

Choose one advertised pair:

```bash
repoprover-codex smoke --model <EXACT_MODEL> --effort <EXACT_EFFORT>
```

Pass criteria:
1. `thread/start` succeeds;
2. `turn/start` succeeds;
3. Codex invokes host dynamic tool `echo`;
4. host returns the result;
5. Codex consumes it;
6. `turn/completed` is observed;
7. command exits zero.

Update event/response handling if the current Codex version differs, and add tests.

## Stage 5 — verify ChatGPT/Codex auth without API key

Confirm the successful smoke test uses the existing Codex CLI login. Do not inspect token contents. If `OPENAI_API_KEY` exists, rerun safely with it absent, e.g. on Linux/macOS:

```bash
env -u OPENAI_API_KEY repoprover-codex smoke --model <MODEL> --effort <EFFORT>
```

The test should still work. Do not modify the user's persistent shell configuration.

## Stage 6 — obtain current RepoProver

Use a sibling checkout:

```bash
cd ..
git clone https://github.com/facebookresearch/repoprover.git
cd repoprover
git rev-parse HEAD
```

Record the SHA and install it into the same external venv:

```bash
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" -e .
```

Inspect the real current APIs before assuming the prototype adapter is correct, especially:

```text
src/repoprover/agents/base.py
src/repoprover/agents/contributor.py
src/repoprover/agents/tools.py
src/repoprover/agents/lean_tools.py
src/repoprover/coordinator.py
```

Adapt `repoprover-codex`; avoid modifying upstream RepoProver unless a temporary local experiment is essential.

## Stage 7 — Lean environment

Run:

```bash
lake --version
lean --version
```

Use a RepoProver toy/example project if suitable, otherwise create a tiny isolated
Lean project outside Dropbox. All `.lake`, Mathlib, REPL, download, and build
caches must remain outside Dropbox. The first theorem must be trivial (`rfl`,
`simp`, or `norm_num`) so provider plumbing rather than theorem difficulty is
being tested.

## Stage 8 — real RepoProver PROVE agent

The intended command surface is:

```bash
repoprover-codex repoprover-prove \
  --project /path/to/toy-project \
  --chapter toy \
  --theorem toy_theorem \
  --lean-path Toy.lean \
  --model <MODEL> \
  --effort <EFFORT>
```

Correct the implementation after inspecting the actual RepoProver APIs.

Pass criteria:
1. construct a real RepoProver `ContributorAgent` in PROVE mode;
2. enumerate its real tools;
3. expose them as Codex dynamic tools;
4. Codex invokes at least one RepoProver tool;
5. a real Lean check is performed through RepoProver;
6. Lean accepts the proof;
7. no OpenAI API key is needed.

If worktrees, recorder state, or the global Lean pool are required, configure them correctly rather than bypassing them unsafely.

## Stage 9 — failure modes

Add real or simulated regression tests for:
- unknown model;
- unsupported effort;
- missing Codex executable;
- unauthenticated Codex;
- app-server crash;
- request timeout;
- turn timeout;
- dynamic-tool exception;
- malformed arguments;
- Lean tool failure;
- interrupted/cancelled turn.

Critical semantic rule: **failure to prove is not evidence that the theorem is false**. Provider/resource/tool failures, formalization mismatch, unproved theorem, and genuine mathematical disproof must remain distinct outcomes.

## Stage 10 — concurrency

Only after single-agent success. Test exactly 2 concurrent Codex-backed agents first. Do not use RepoProver's API-scale concurrency defaults. If needed, add a process/package-level semaphore with a conservative default (2 or 3 active turns). Respect ChatGPT Pro limits; do not try to bypass them.

## Stage 11 — explicit macOS validation when applicable

On macOS rerun:

```bash
pytest -q
repoprover-codex doctor
repoprover-codex models
repoprover-codex smoke --model <MODEL> --effort <EFFORT>
```

Then run the same real RepoProver + Lean single-agent proof. Record macOS version, architecture, Codex version, Python, Lean and Lake versions. Local mode is the acceptance target; SLURM is not.

# Design constraints

## RepoProver remains the control plane

Initially keep Codex-native filesystem/shell mutation restricted. RepoProver's explicit file, Lean, Git and Mathlib tools should be authoritative. Avoid two competing mutation control planes in one worktree. If a native Codex capability later becomes necessary, make it explicit/configurable and document why.

## Context handling

RepoProver has manual context compaction for its OpenAI-style loop. For a native persistent Codex backend, investigate whether Codex should own its own thread context. Do not globally alter RepoProver behavior before the provider path is validated.

## Logging

Log enough to reproduce failures: Codex version, model, effort, thread ID, turn ID, dynamic tool names, success/failure, completion status. Never log auth tokens, `auth.json` contents, private keys, or credential headers.

# Deliverables to leave locally

Before finishing, leave:
1. working source;
2. passing tests;
3. real `doctor` result;
4. real model discovery;
5. real dynamic-tool smoke result;
6. real RepoProver + Lean single-agent result if the environment permits;
7. updated README and testing docs;
8. `TEST_REPORT.md` with OS/arch, Python, Codex, RepoProver SHA, Lean/Lake, exact model/effort, commands/results, protocol changes and limitations;
9. sensible LOCAL Git commits.

Do not include secrets.

# Git policy

Allowed: local `git status`, `git diff`, `git add`, `git commit`, `git log`.

Forbidden unless the user explicitly changes this instruction:
- pushing to `facebookresearch/repoprover`;
- opening an upstream PR;
- opening an upstream issue;
- merging anything upstream.

If a user-owned `vitskov/repoprover-codex` repository is created later, wait for explicit permission before pushing.

# Expected final report

Report:

```text
STATUS
- unit tests:
- package installation:
- real app-server doctor:
- model discovery:
- dynamic-tool smoke:
- ChatGPT/Codex auth without API key:
- RepoProver import/integration:
- real Lean proof:
- two-agent concurrency:
- macOS local-mode test (if applicable):
- upstream PR: NOT CREATED

TESTED CONFIGURATION
- OS:
- architecture:
- Python:
- Codex:
- model:
- effort:
- RepoProver commit:
- Lean:
- Lake:

CHANGES MADE
- ...

KNOWN LIMITATIONS
- ...

LOCAL COMMITS
- ...
```

Do not claim a stage passed unless it was actually executed successfully.

# First action

Read this entire file and `README.md`. Then execute Stage 0 through Stage 5, fixing issues autonomously. Continue through RepoProver and Lean integration if the environment permits. Ask the user only if a genuinely external credential/permission is required.

Again: **NO UPSTREAM PULL REQUEST.**
