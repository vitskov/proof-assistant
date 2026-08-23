# Working on RepoProver Codex as an AI agent

This page is operational context for coding agents. It does not grant authority
to read credentials, delete unrelated data, contact upstream maintainers, or
publish changes without the user's instruction.

## Read first

Before acting, read:

1. the repository [README](../README.md);
2. [Installation](INSTALLATION.md);
3. [Usage](USAGE.md);
4. [Incremental verification](INCREMENTAL_VERIFICATION.md);
5. [Cache and storage](CACHE_AND_STORAGE.md); and
6. [Development and testing](DEVELOPMENT.md) for code changes.

`CODEX_HANDOFF.md` is historical context. Current source, tests, and maintained
documentation take precedence when they differ from that handoff.

## Non-negotiable environment rules

- Use Python 3.13.
- Use `uv` whenever feasible for environments, installation, dependencies, and
  builds.
- Never place a Python environment in Dropbox.
- Never place a Lean, Lake, Mathlib, or package cache in Dropbox.
- Keep the development source at a local path such as
  `$HOME/src/repoprover-codex`.
- Keep the Python environment at a local path such as
  `$HOME/.venvs/repoprover-codex`.
- Keep managed cache data at a local path such as
  `$HOME/.cache/repoprover-codex`.
- Every installation script must compile and execute a native test program; a
  compiler lookup alone is not acceptable.

Resolve and verify actual paths before deleting anything. Never recursively
delete a home directory, workspace root, Dropbox root, or unresolved variable.

## Authentication and provider boundary

- Authentication stays inside the installed Codex CLI.
- Do not read or print `~/.codex/auth.json` or token contents.
- Do not extract an OAuth token or convert it into `OPENAI_API_KEY`.
- Do not require a billable API key when the existing Codex login works.
- Do not describe the system as offline: manuscript context and tool results
  needed for the run are processed through the authenticated Codex service.
- Use persistent `codex app-server` and client-defined dynamic tools; do not
  replace the normal backend with `codex exec`.
- Validate exact model and effort choices against `model/list`.

RepoProver runs must start the Codex child without existing local MCP servers,
apps, plugins, or skills. The implementation disables them in child-only
configuration and verifies the effective inventory. Preserve this fail-closed
behavior.

## Repository and upstream boundary

The user-owned repository is `vitskov/repoprover-codex`. The RepoProver checkout
is an integration dependency and must remain untouched unless the user
explicitly requests otherwise.

Never push to, open a pull request against, or create an issue in
`facebookresearch/repoprover`. Do not prepare an upstream pull request as a
side effect of local testing.

Do not push this repository or create releases without explicit authorization.
Before any authorized publication, fetch the user-owned origin, inspect remote
history, run the full suite, inspect Git status, audit secrets and artifacts,
and verify the pushed branch and SHA.

Keep development releases in the `0.4.x` series and advance patch versions
conservatively. Do not change the minor version to `0.5` or later without the
user's explicit instruction.

## Safe manuscript workflow

1. Validate the install with `doctor`, `models`, and `cache doctor`.
2. Use an exact advertised model/effort pair.
3. Require a real task file and a persistent project outside Dropbox.
4. Keep the source manuscript read-only.
5. Let cache admission create a transactional reservation before Lake work.
6. Monitor with `manuscript status` and process state without mutating an active run.
7. Treat only independently built Lean evidence as verified.
8. Never equate “not verified” with “false.”
9. Preserve the project, its SQLite state, snapshots, Lean Git history, and run
   evidence unless the user explicitly requests deletion.

Do not run manual cache cleanup during an active job. Active reservations and
leases are part of the disk-safety design.

## Code map

- `src/repoprover_codex/protocol.py` — bidirectional app-server JSONL client.
- `src/repoprover_codex/backend.py` — initialization, isolation checks, threads,
  turns, events, and dynamic-tool callbacks.
- `src/repoprover_codex/tools.py` — RepoProver tool-schema translation.
- `src/repoprover_codex/integration.py` — adapter for an existing RepoProver
  agent.
- `src/repoprover_codex/manuscript.py` — manuscript snapshot, generated Lean
  workspace, evidence, and legacy one-shot result evaluation.
- `src/repoprover_codex/incremental/` — persistent snapshots, structural index,
  graphs, SQLite state, agent tools, scheduler, certification, and reports.
- `src/repoprover_codex/lean/DependencyExtractor.lean` — mechanical elaborated
  declaration dependency/type/value/axiom extraction.
- `src/repoprover_codex/cache.py` — storage policy, leases, reservations,
  dependency sharing, and bounded GC.
- `src/repoprover_codex/cache_index.py` — transactional SQLite accounting.
- `src/repoprover_codex/cli.py` — public command surface and run lifecycle.
- `tests/` — unit, golden manuscript, state, certification, protocol, failure,
  cache, manuscript, CLI, and installer tests.

## Required validation for code changes

Use the external Python 3.13 environment and uv:

```bash
cd "$HOME/src/repoprover-codex"
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" -e '.[dev]'
repoprover-codex compiler-check
"$HOME/.venvs/repoprover-codex/bin/python" -m pytest -q
git diff --check
```

For installation, cache, or packaging changes, also run
`scripts/install-dev.sh`, a fresh-wheel test in a disposable external Python
3.13 environment, `cache doctor`, and an appropriately small real Lean
acceptance. Remove the disposable environment and build artifacts afterward.

Do not weaken operation-count, deadline, concurrency, crash-recovery, child
isolation, compiler-execution, Dropbox-path, or evidence-classification tests
to make a change pass.

## Handoff checklist

Report:

- what changed and why;
- tests and real checks actually run;
- installed version, interpreter, source, cache, and compiler locations;
- active run state and any preserved partial output;
- Git status and whether changes were committed or pushed;
- exact RepoProver SHA tested; and
- explicitly that no upstream RepoProver PR was created.
