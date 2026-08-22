# Internal testing plan

The goal of this phase is to establish that a Pro-authenticated local Codex CLI
can act as RepoProver's LLM runtime without an API key and with exact
model/reasoning-effort selection.

## Stage 1 — package tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Expected: all simulator tests pass without Codex or network access.

## Stage 2 — installed Codex connectivity

```bash
codex --version
repoprover-codex doctor
repoprover-codex models
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

Install the current RepoProver checkout into the same venv. Construct one of its
agent classes on a toy Lean project, then call `run_repoprover_agent(...)`.

The first target should be deliberately trivial, e.g. proving a theorem reducible
by `rfl` or `simp`. This tests RepoProver tool schemas and actual Lean dispatch
without conflating provider integration with mathematical difficulty.

## Stage 5 — failure modes

Test each of the following deliberately:

- invalid model;
- valid model + invalid effort;
- unauthenticated Codex;
- Codex executable missing;
- dynamic tool exception;
- Codex turn timeout;
- Lean tool failure.

The adapter should distinguish all of these from "the theorem is false."

## Stage 6 — concurrency

Only after stages 1–5 pass, test 2 concurrent RepoProver agents. Do not initially
use RepoProver's API-oriented high concurrency defaults on a single ChatGPT Pro
entitlement.

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
