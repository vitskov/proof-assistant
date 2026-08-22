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

This repository is for internal testing before any upstream RepoProver pull
request is prepared.

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
- a conservative process-level limit of two active Codex turns;
- protocol simulator tests that require neither Codex nor network access;
- a real-Codex smoke-test command.

Not implemented intentionally:

- OAuth/token extraction;
- a fake OpenAI HTTP endpoint;
- upstream modifications to `facebookresearch/repoprover`;
- direct use of Codex shell/file mutation as a replacement for RepoProver tools.

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
`repoprover-prove` enforce this for package-managed runs.
