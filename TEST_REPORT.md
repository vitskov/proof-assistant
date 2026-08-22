# Acceptance test report — 2026-08-22

## Result

The standalone `repoprover-codex` package passed its internal macOS acceptance
plan with Python 3.13. Codex CLI authentication worked with `OPENAI_API_KEY`
removed, RepoProver completed a real Lean proof through Codex dynamic tools, and
the result passed an independent final `lean_check`.

No upstream RepoProver files were modified, no upstream remote was added to this
package, and no pull request or issue was opened.

## Environment

- macOS 12.7.6 (21H1320), x86_64
- Python 3.13.11
- uv 0.9.26
- Codex CLI 0.149.0
- Git 2.37.1
- Lean 4.28.0, commit `7e01a1bf5c70fc6167d49c345d3bf80596e9a79b`
- Lake 5.0.0-src+7e01a1b
- RepoProver `386adba3df572cb71df534add2c764e071898a2e`

## Storage and installer guarantees

- Active Python environment: `/Users/vui1/.venvs/repoprover-codex`
- Disposable Lean project and `.lake` cache:
  `/private/tmp/repoprover-codex-toy-20260822`
- No `.venv`, `venv`, `.lake`, or `.elan` directory was found in either Dropbox
  source checkout.
- The former Dropbox `.venv` was moved recoverably to the Trash.
- `scripts/install-dev.sh` uses uv and Python 3.13 by default, rejects any venv
  path containing `Dropbox`, and always invokes `repoprover-codex compiler-check`
  after installation and before tests.
- A live installer run compiled and executed a C program successfully, selected
  `/usr/bin/clang` as the working Lean compiler fallback, and then passed all 40
  tests.
- A deliberate Dropbox venv request was rejected before creating the path with
  exit status 2.

The user-wide rules are also recorded in `/Users/vui1/.codex/AGENTS.md`: never
place Python environments or Lean caches in Dropbox, use uv whenever feasible,
and require a real compiler compile/execute preflight in package install scripts.

## Codex capability isolation

RepoProver's registered dynamic tools are the authoritative external tool
surface. For each backend, the package now:

1. enumerates configured local MCP servers and replaces every enabled entry with
   a disabled child-only transport;
2. disables Codex apps and plugins in the child process;
3. disables bundled skills and automatic skill instructions;
4. probes the target workspace for user/repository/admin skills and disables
   every discovered skill by absolute path in the child-only configuration;
5. selects no hosting-platform capability roots or remote environments; and
6. queries the effective MCP and skill inventories, failing startup if any MCP
   tool/resource or enabled skill remains.

Live validation found zero external tools and zero enabled skills. The isolated
app-server process tree contained only the Node launcher and Codex binary; no
configured MCP, app, or plugin subprocess was started. These controls do not
modify the user's persistent Codex configuration.

## Test results

### Simulator and failure tests

`python -m pytest -q` under Python 3.13.11: **40 passed**.

Covered cases include malformed tool arguments, dynamic-tool exceptions,
unknown models/efforts, missing executable, simulated authentication failure,
app-server crash, request and turn timeouts, abnormal turn statuses, Lean tool
failure, semantic proof outcomes, external-tool leaks, local-skill leaks, and
the bidirectional JSONL protocol.

### Compiler and package tests

- Development installer: passed compiler compile/execute and 40 tests.
- `uv build`: built both sdist and wheel successfully.
- Fresh wheel environment: Python 3.13.11 under `/private/tmp`.
- Installed wheel version: 0.1.0.
- Installed wheel `compiler-check`: passed with `/usr/bin/clang`.

### Live Codex connectivity

`doctor` initialized app-server and returned seven models. The exact visible
catalog was:

- `gpt-5.6-sol`: low, medium, high, xhigh, max, ultra
- `gpt-5.6-terra`: low, medium, high, xhigh, max, ultra
- `gpt-5.6-luna`: low, medium, high, xhigh, max
- `gpt-5.5`: low, medium, high, xhigh
- `gpt-5.4`: low, medium, high, xhigh
- `gpt-5.4-mini`: low, medium, high, xhigh
- `gpt-5.3-codex-spark`: low, medium, high, xhigh

The final API-key-free smoke test used `gpt-5.6-luna`/`low`, invoked only the
registered `echo` tool, and completed successfully:

- thread `01a02b51-92be-7383-ae1c-3f17f98f09f0`
- turn `01a02b51-9371-7120-8d78-e3df7933d04c`

### Real RepoProver + Lean proof

The final isolated `ContributorAgent` PROVE run started from a committed
`sorry`, replaced it with `rfl`, used RepoProver's real `lean_check`, and made a
local toy-project commit. The adapter then found the named declaration without
`sorry`/`axiom` and ran a separate final RepoProver `lean_check`.

- outcome: `proved`
- verification: `Compiles successfully`
- Codex thread `01a02b52-82f9-79e0-ad8c-52637db88722`
- Codex turn `01a02b52-83cb-7482-b4f4-5db0f13872c9`
- 12 audited RepoProver dynamic-tool calls, including `lean_check`, file, git,
  and RepoProver's registered `bash` tool
- toy commit `d27b505621d4f91e5998bbefa72a6e6bc2d5c054`
- toy worktree clean after completion

### Concurrency

Exactly two isolated Codex-backed agents ran concurrently in one Python process.
Each created a separate thread and turn, called the registered `echo` tool once,
and completed successfully. This validates the package-level two-turn semaphore;
it is not a claim that two concurrent Lean proof jobs were exercised.

## Repository state and remaining boundary

- Package local integration commit: `8408f7d`
- Capability-isolation commit: `d9fa357`
- RepoProver sibling checkout: clean and unmodified
- Package Git remotes: none
- Upstream PR/issue activity: none

Linux remains a documented target but was not available for this local
acceptance run. Any future Codex protocol/config change that prevents the
inventory checks from proving isolation will now fail closed rather than start a
RepoProver turn with unverified local capabilities.
