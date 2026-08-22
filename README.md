# repoprover-codex

A standalone Codex CLI / `codex app-server` integration for
[`facebookresearch/repoprover`](https://github.com/facebookresearch/repoprover).

The package is intentionally designed so that ChatGPT/Codex authentication stays
inside the installed Codex CLI. It does **not** read `~/.codex/auth.json`, extract
OAuth tokens, or require `OPENAI_API_KEY`.

## Design

```text
RepoProver BaseAgent
      |
      v
repoprover-codex adapter
      |
      v
codex app-server  <---- existing `codex login` / ChatGPT Pro entitlement
      |
      +---- dynamic tool request ----> RepoProver handle_tool_call()
                                      |
                                      +--> lean_check
                                      +--> mathlib grep
                                      +--> controlled file tools
```

RepoProver exposes its existing OpenAI-style function schemas. This package
translates them to Codex `dynamicTools`. When Codex emits `item/tool/call`, the
request is dispatched back to RepoProver's existing `handle_tool_call()`.

## Current scope

This is an independently maintained experimental bridge. Its internal macOS
acceptance phase is complete, and it is published in the user-owned
[`vitskov/repoprover-codex`](https://github.com/vitskov/repoprover-codex)
repository. It has not been proposed to or merged into upstream RepoProver.

Implemented:

- persistent stdio connection to `codex app-server`;
- app-server `initialize`;
- `model/list` discovery and exact model/effort validation;
- `thread/start` with dynamic RepoProver tools;
- `turn/start` with explicit model and reasoning effort;
- handling of `item/tool/call`;
- auditable dynamic-tool call/result records;
- collection of assistant output and `turn/completed`;
- immediate app-server crash detection and structured timeout failures;
- fail-closed approval handling for accidental shell/file approval requests;
- child-process isolation for inherited MCP/app/plugin tools and local skills;
- small compatibility adapter for a RepoProver agent;
- final theorem/source verification through RepoProver's real `lean_check`;
- a file-based manuscript interface with isolated input snapshots, generated
  Lean workspaces, durable reports, and machine-readable run artifacts;
- a conservative process-level limit of two active Codex turns;
- protocol simulator tests that require neither Codex nor network access;
- a real-Codex smoke-test command.

Not implemented intentionally:

- OAuth/token extraction;
- a fake OpenAI HTTP endpoint;
- upstream modifications to `facebookresearch/repoprover`;
- direct use of Codex shell/file mutation as a replacement for RepoProver tools.

## Tested configuration

The final local acceptance run used:

- macOS 12.7.6 (21H1320), x86_64;
- Python 3.13.15 and uv 0.9.26;
- Codex CLI 0.149.0 with `gpt-5.6-luna` at `low` effort;
- RepoProver commit `386adba3df572cb71df534add2c764e071898a2e`;
- Lean 4.28.0 (`7e01a1bf5c70fc6167d49c345d3bf80596e9a79b`);
- Lake 5.0.0-src+7e01a1b; and
- 71 passing package tests after the manuscript workflow implementation.

See [`TEST_REPORT.md`](TEST_REPORT.md) for the live Codex, RepoProver, Lean,
packaging, isolation, cache, and concurrency evidence.

## Install for internal testing

Python environments must live outside Dropbox. The development installer uses
`uv`, defaults to `$HOME/.venvs/repoprover-codex`, runs a mandatory native
compile-and-execute check, initializes validated home-local cache storage, and
runs the test suite:

```bash
cd repoprover-codex
scripts/install-dev.sh
source "$HOME/.venvs/repoprover-codex/bin/activate"
```

Override the external environment, Python selection, or cache root with
`REPOPROVER_CODEX_VENV`, `REPOPROVER_CODEX_PYTHON`, and
`REPOPROVER_CODEX_CACHE_HOME`. The installer rejects a venv path containing
`Dropbox`; the cache root must resolve inside the user home on a local
filesystem and outside every detected Dropbox root.

RepoProver can be installed separately from a checkout:

```bash
git clone https://github.com/facebookresearch/repoprover
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" \
  -e ./repoprover
```

Verify that Codex is installed and authenticated:

```bash
codex --version
codex login
repoprover-codex doctor
repoprover-codex compiler-check
```

`compiler-check` compiles and executes a C program. On older macOS releases it
also detects when Lean's bundled clang requires a newer system `libc++`, then
sets `LEAN_CC=/usr/bin/clang` for the current RepoProver process. On macOS the
RepoProver REPL address-space limit defaults to disabled because its
`RLIMIT_AS` pre-exec hook is not portable there; Linux retains the 24 GB
default.

## Free-form manuscript verification

`manuscript-run` is the high-level interface for a manuscript directory, a
free-form task supplied as a UTF-8 file, and a dedicated output directory:

```bash
repoprover-codex manuscript-run \
  --manuscript /absolute/path/to/latex-source \
  --task-file /absolute/path/to/verification-task.md \
  --output "$HOME/repoprover-runs/run-001" \
  --model gpt-5.6-luna \
  --effort low
```

The task file is authoritative. It can contain a multi-paragraph natural
language request, assumptions, scope restrictions, desired theorem statements,
and acceptance criteria. See [`examples/verify-task.md`](examples/verify-task.md).

The command accepts either:

- a LaTeX-only directory containing at least one `.tex` or `.ltx` file, for
  which it generates a pinned Lean 4.28/Mathlib/REPL project; or
- an existing Lean/Lake project containing LaTeX sources, whose current file
  snapshot is preserved at the workspace root.

The output must be new or empty. The command never modifies the input folder
and refuses to overwrite an existing nonempty output. It excludes source Git
metadata, `.lake`, Python environments, common caches, LaTeX build state,
`.env*`, and `auth.json` from the copied snapshot. It initializes a fresh local
Git history so every agent change is attributable to the run.

```text
run-001/
├── TASK.md                       # exact task input
├── INPUT_MANIFEST.json           # source/task hashes and snapshot metadata
├── VERIFICATION_REPORT.md        # copied out when the agent creates a report
├── RUN_STATUS.json               # concise machine-readable result
├── workspace/                    # copied source, Lean evidence, and Git commits
└── artifacts/
    ├── setup.log
    ├── setup.json
    ├── final.md
    ├── tool-calls.json
    ├── events.json
    ├── verification-build.log
    └── result.json
```

An agent's `-- VERIFIED` marker is not trusted by itself. Exit status 0 also
requires at least one successful RepoProver `lean_check`, a nonempty
`VERIFICATION_REPORT.md`, a new result commit, a clean workspace, and a separate
successful final `lake build`. `unverified`, `blocked`, `incomplete`, provider
failures, and tool/setup failures remain distinct in `RUN_STATUS.json`.
Failure to verify never implies that a statement is false.

The task deliverables live under `--output`. Large Lean/Mathlib/REPL state is
the deliberate exception: the workspace's `.lake` is a symlink into the
validated `$HOME/.cache/repoprover-codex` hierarchy. Codex account/session state
remains in Codex's own home. The output Lean workspace is rejected if it is in
Dropbox; the read-only manuscript input may be in Dropbox because it is copied
before the run.

## Home-local cache storage

All package-managed large state is rooted at
`$HOME/.cache/repoprover-codex` by default:

```text
~/.cache/repoprover-codex/
├── mathlib-downloads/  # MATHLIB_CACHE_DIR
├── lake/
│   ├── system/         # LAKE_CACHE_DIR
│   ├── dependencies/   # reserved immutable dependency depots
│   └── builds/         # isolated .lake trees, one per project path
├── fixtures/           # reusable test projects
├── worktrees/          # external RepoProver/Lean worktrees
├── locks/              # concurrent cache-operation locks
└── tmp/
```

Initialize and validate it with:

```bash
repoprover-codex cache init
repoprover-codex cache doctor
repoprover-codex cache path
```

The validator canonicalizes symlinks, requires the root to remain inside the
user home, reads Dropbox's registered root metadata without inspecting any
credentials, and rejects Dropbox and remote/network filesystems. An override
through `--cache-home` or `REPOPROVER_CODEX_CACHE_HOME` is accepted only after
the same checks.

Lake has no environment variable that relocates a workspace's complete `.lake`
tree. Before using an external Lean project, attach it explicitly:

```bash
repoprover-codex cache attach --project /path/to/lean-project
```

This moves the existing `.lake` directory into the per-project cache location
and replaces it with a symlink. The operation is locked, refuses to merge two
existing cache trees, refuses Git-tracked `.lake` content, and adds only a
repository-local `/.lake` exclusion in `.git/info/exclude`. It never edits the
tracked `.gitignore`. `repoprover-prove` fails closed if the project or cache is
in Dropbox, or if `.lake` does not resolve into the managed cache. For a source
checkout in Dropbox, create a Git worktree under the printed `worktrees/`
directory instead of attaching the Dropbox checkout.

## Inspect models and reasoning levels

```bash
repoprover-codex models
```

The command queries the *installed Codex instance*. Do not assume that a model
or effort supported by the raw OpenAI API is necessarily available through
Codex/your ChatGPT plan.

## Real app-server smoke test

This exercises authentication, model discovery, thread creation, an actual turn,
and one dynamic tool:

```bash
repoprover-codex smoke \
  --model <MODEL_FROM_MODELS_COMMAND> \
  --effort high
```

The smoke test exposes a harmless `echo` dynamic tool and asks Codex to call it.

## RepoProver adapter

The minimal integration deliberately avoids monkey-patching RepoProver globally:

```python
from repoprover_codex import CodexConfig, run_repoprover_agent

result = run_repoprover_agent(
    agent,
    run_kwargs={"task": ...},
    codex=CodexConfig(
        model="...",
        effort="high",
    ),
)
```

For production use, the cleaner upstream change is to add an LLM backend
protocol to RepoProver's `BaseAgent`, but the standalone adapter is enough to
exercise all essential behavior before proposing that patch.

See `docs/INTERNAL_TESTING.md`.


## Single real RepoProver prover test

Once RepoProver and a toy Lean project are installed:

```bash
repoprover-codex repoprover-prove \
  --project /path/to/project \
  --chapter toy \
  --theorem toy_theorem \
  --lean-path Toy.lean \
  --model MODEL \
  --effort high
```

This is the preferred end-to-end internal test before any coordinator-level
integration or upstream pull request.

Successful exit requires all of the following: Codex invoked a RepoProver tool,
Codex invoked a successful `lean_check`, the named declaration exists without
`sorry`/`axiom`, and a final independent RepoProver `lean_check` accepts the
edited Lean file. Other outcomes are printed distinctly as `unproved`,
`tool_failure`, `formalization_mismatch`, or `provider_failure`. In particular,
an unproved target is never reported as a false theorem.

The backend enumerates configured Codex MCP server names, disables them in the
child app-server configuration, and disables Codex apps and plugins. It also
enumerates every local skill visible from the target workspace, disables those
skills by path, disables bundled skills and automatic skill instructions, and
selects no hosting-platform capability roots or remote environments. It then
checks app-server's effective MCP and skill inventories and fails closed if an
external tool, resource, or enabled local skill remains exposed. It neither
prints server/skill settings nor changes the user's persistent Codex
configuration. Set
`CodexConfig(isolate_external_tools=False)` only for an intentional experiment.

Keep Lean projects and their `.lake`/Mathlib/REPL caches outside Dropbox and
inside the validated home-local cache hierarchy. The development installer and
`repoprover-prove` and `manuscript-run` enforce this for package-managed runs.
