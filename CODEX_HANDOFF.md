# CODEX HANDOFF — Proof Assistant

This file is a compact maintainer handoff. The maintained user and architecture
documentation under `docs/` is authoritative.

## Mission and identity

Proof Assistant is a persistent, incremental formal referee for multi-file
LaTeX manuscripts. It combines a replaceable Textual interface, UI-neutral
workflow/project services, isolated Codex/RepoProver proof workers, and
independent Lean certification.

Current identity:

```text
product             Proof Assistant
distribution        proof-assistant
import package       proof_assistant
primary command      proof-assistant
deprecated alias     repoprover-codex (0.1 migration window)
repository           vitskov/proof-assistant
version              0.1.0
source               $HOME/src/proof-assistant
environment          $HOME/.venvs/proof-assistant
new projects         $HOME/proof-assistant/<project-name>
cache                $HOME/.cache/repoprover-codex
```

The cache name intentionally does not follow the product rename. Reusing it
prevents duplication of the shared multi-gigabyte Mathlib dependency depot.

## Non-negotiable rules

- Use Python 3.13 and `uv` whenever feasible.
- Never place Python environments, managed projects, or Lean/Lake/Mathlib
  caches in Dropbox.
- External manuscript sources may be in Dropbox; warn and use stable staged
  import.
- Every installer must compile and execute a native test program.
- Authentication stays inside Codex CLI. Never read/print `auth.json` or turn
  its credential into an API key.
- Verification Codex children start with MCP servers, apps, plugins, bundled
  skills, and local skills disabled, then verify this fail-closed.
- Never push to, open a pull request against, or create an issue in
  `facebookresearch/repoprover`.
- Never publish Proof Assistant without explicit user authorization.

## User workflow contract

Bare `proof-assistant` (or `proof-assistant tui`) launches the TUI.

For a new project it selects an external source folder, creates a managed
project outside Dropbox, and creates a project-owned `VERIFY.yaml`. The user
chooses the default task or customizes seeded instructions in the built-in text
editor. There is no user-facing external task-file workflow.

If verification needs clarification, show the actual input file and exact
highlighted LaTeX span. The user edits the external source. Detect all stable
multi-file changes, stage/re-hash them, calculate proof-tree impact, and require
explicit confirmation before the next iteration. Revalidate the source
inventory at confirmation time.

Resume routing is based on persistent backend state:

- unchanged open question -> existing clarification screen;
- stable source change -> change review;
- completed run -> findings;
- interrupted run -> recovery;
- provider/Lean failure -> diagnostics; and
- active external writer -> read-only progress.

## Architectural boundary

```text
proof_assistant.tui
        | typed commands/view models/events only
        v
workflow + workspace + presentation services
        | validated specs, inventories, plans, results
        v
proof_assistant.incremental
        | Git/SQLite/graphs/Codex workers/certificates
        v
Lean kernel
```

The backend must not import Textual. The TUI must not copy source, write
SQLite, calculate invalidation, invoke Codex directly, or assign certificate
state. Filesystem notifications only wake the observer; stable full inventories
and staged re-hashing establish source identity.

Codex may improve clarification wording under a validated schema, but the host
owns the claim, source path/span, quotation, diagnostics, affected graph, and
possible-resolution facts. Invalid output falls back deterministically.

## Verification invariants

- The author-facing source and managed project cannot contain one another.
- Immutable filtered Git snapshots identify source input.
- Stable manuscript objects and explicit/semantic edges form the manuscript
  graph; elaborated Lean expressions form the Lean graph.
- Changed statements invalidate the complete reverse-dependent closure and no
  unrelated branch.
- Only ready claims whose dependencies are certified may run.
- Parallel workers modify assigned claim modules in isolated worktrees.
- The host merges deterministically, independently builds, extracts structural
  type/value/dependency/axiom evidence, and only then certifies.
- Inconclusive, ambiguous, technical failure, suspected false, and
  kernel-checked counterexample remain distinct states.
- Questions and completed evidence survive process interruption without model
  conversation state.

## Validation before handoff/publication

```bash
cd "$HOME/src/proof-assistant"
scripts/install-dev.sh
proof-assistant compiler-check
proof-assistant cache doctor
proof-assistant doctor
proof-assistant models
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
git diff --check
```

Also run a fresh Python 3.13 wheel smoke outside Dropbox and one small real
manuscript/TUI feedback-loop acceptance. Validate default/custom task, Dropbox
warning, exact multi-file clarification location, stable change review, stale
confirmation rejection, resume behavior, certificate reuse, and no duplicate
cache depot.

Record exact Python/uv/Codex/Lean/Lake/Mathlib/RepoProver/compiler versions and
all actual evidence in `TEST_REPORT.md`. Never claim a gate that was not run.
