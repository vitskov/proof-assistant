# Internal testing plan

The goal of this phase is to establish that a Pro-authenticated local Codex CLI
can act as RepoProver's LLM runtime without an API key and with exact
model/reasoning-effort selection.

## Stage 1 — package tests

```bash
scripts/install-dev.sh
source "$HOME/.venvs/repoprover-codex/bin/activate"
```

The script uses `uv`, refuses to put a Python environment in Dropbox, performs
an actual compiler compile/run smoke test, initializes
`$HOME/.cache/repoprover-codex`, and runs all simulator tests. Set
`REPOPROVER_CODEX_VENV` to another non-Dropbox path if needed. Cache overrides
must remain inside the user home, outside registered Dropbox roots, and on a
local filesystem.

Validate cache policy separately when diagnosing an installation:

```bash
repoprover-codex cache path
repoprover-codex cache doctor
```

## Stage 2 — installed Codex connectivity

```bash
codex --version
repoprover-codex doctor
repoprover-codex models
repoprover-codex compiler-check
```

`doctor` proves that app-server starts and responds. `models` is authoritative
for the model/effort pairs that should be tested.

## Stage 3 — actual dynamic tool

Pick an exact pair printed by `models`:

```bash
repoprover-codex smoke --model MODEL --effort EFFORT
```

Pass criteria:

1. `thread/start` succeeds;
2. Codex invokes the host `echo` dynamic tool;
3. the host result is returned to Codex;
4. `turn/completed` is observed;
5. the command exits zero.

## Stage 4 — RepoProver single-agent test

Install the current RepoProver checkout into the same external venv with `uv`.
Place the disposable Lean project under the `fixtures/` directory printed by
`repoprover-codex cache path`, or create an external worktree under its
`worktrees/` directory. If the project already has `.lake`, run
`repoprover-codex cache attach --project PATH`. Construct one of its agent
classes on that project, then call `run_repoprover_agent(...)`.

The first target should be deliberately trivial, e.g. proving a theorem reducible
by `rfl` or `simp`. This tests RepoProver tool schemas and actual Lean dispatch
without conflating provider integration with mathematical difficulty.

On macOS, `repoprover-prove` disables RepoProver's non-portable `RLIMIT_AS`
pre-exec limit. It also checks Lean's native compiler and automatically uses
`/usr/bin/clang` through `LEAN_CC` when Lean's bundled clang cannot execute on
the installed macOS version. The cache extraction environment is normalized
from unsupported `C.UTF-8` to `C`.

## Stage 5 — failure modes

Test each of the following deliberately:

- invalid model;
- valid model + invalid effort;
- unauthenticated Codex;
- Codex executable missing;
- app-server crash;
- app-server request timeout;
- dynamic tool exception;
- malformed dynamic-tool arguments;
- Codex turn timeout;
- Lean tool failure;
- cancelled/interrupted/failed turns.

The adapter distinguishes `provider_failure`, `tool_failure`,
`formalization_mismatch`, and `unproved`. None of these mean that the theorem is
false; `disproved` would require separate formal evidence and is never inferred
from failure to complete a proof.

## Stage 6 — concurrency

Only after stages 1–5 pass, test 2 concurrent RepoProver agents. Do not initially
use RepoProver's API-oriented high concurrency defaults on a single ChatGPT Pro
entitlement. The backend has a package-level semaphore allowing at most two
active turns in one process.

## Stage 7 — upstream design decision

After successful internal testing, choose between:

1. minimal RepoProver hook allowing an external backend to replace
   `run_tool_loop`; or
2. a first-class `LLMBackend` protocol upstream.

No upstream PR should be opened before this decision is reviewed.


## Concrete single-Prover command

After installing RepoProver into the same environment and preparing a valid
RepoProver/Lean project:

```bash
repoprover-codex repoprover-prove \
  --project /path/to/toy-project \
  --chapter toy \
  --theorem toy_theorem \
  --lean-path Toy.lean \
  --model MODEL_FROM_REPOPROVER_CODEX_MODELS \
  --effort high
```

This constructs RepoProver's actual `ContributorAgent` in PROVE mode and exposes
all of its registered RepoProver tools to Codex as dynamic tools. Codex remains
read-only at its own filesystem layer; modifications must therefore occur
through RepoProver's explicit file/git tool handlers. This is intentional and
makes RepoProver, not Codex's native shell, the authoritative control plane.
Inherited Codex MCP servers are replaced with disabled child-only config entries;
Codex apps, plugins, bundled/local skills, automatic skill instructions,
hosting-platform capability roots, and remote environments are disabled by
default. The backend checks the resulting MCP and skill inventories and fails
closed if an external tool, resource, or enabled local skill is still exposed.
None of these child-only controls change the user's persistent Codex
configuration.
The command exits zero only after it observes a successful Codex-requested
`lean_check`, confirms the named declaration no longer contains `sorry` or
`axiom`, and performs a final RepoProver `lean_check` over the resulting file.
It also refuses to start unless the project is outside Dropbox and its `.lake`
tree resolves into the validated home-local cache.
