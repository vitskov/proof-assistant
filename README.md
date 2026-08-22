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
- collection of assistant output and `turn/completed`;
- fail-closed approval handling for accidental shell/file approval requests;
- small compatibility adapter for a RepoProver agent;
- protocol simulator tests that require neither Codex nor network access;
- a real-Codex smoke-test command.

Not implemented intentionally:

- OAuth/token extraction;
- a fake OpenAI HTTP endpoint;
- upstream modifications to `facebookresearch/repoprover`;
- direct use of Codex shell/file mutation as a replacement for RepoProver tools.

## Install for internal testing

```bash
git clone <your-repoprover-codex-checkout>
cd repoprover-codex
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

RepoProver can be installed separately from a checkout:

```bash
git clone https://github.com/facebookresearch/repoprover
pip install -e ./repoprover
```

Verify that Codex is installed and authenticated:

```bash
codex --version
codex login
repoprover-codex doctor
```

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
