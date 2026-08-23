# Working on Proof Assistant as an AI agent

This is operational context, not authority to read credentials, delete unrelated
data, contact upstream maintainers, or publish changes.

## Read first

Read the repository [README](../README.md), [Architecture](ARCHITECTURE.md),
[Incremental verification](INCREMENTAL_VERIFICATION.md), [Cache and
storage](CACHE_AND_STORAGE.md), and [Development](DEVELOPMENT.md). The current
source, tests, and maintained docs supersede historical handoff notes.

## Environment rules

- Use Python 3.13 and `uv` whenever feasible.
- Development source: `$HOME/src/proof-assistant`.
- Python environment: `$HOME/.venvs/proof-assistant`.
- Managed projects: `$HOME/proof-assistant/<project-name>` by default.
- Cache: exactly `$HOME/.cache/repoprover-codex` unless explicitly configured.
- Never put Python environments, managed projects, or Lean/Lake/Mathlib caches
  in Dropbox.
- An external manuscript source may be in Dropbox; warn and use stable staged
  import rather than rejecting it.
- Every installer must compile and execute a native program.

Keeping the old cache name is intentional and prevents a duplicate
multi-gigabyte Mathlib depot. Never rename or migrate it merely for branding.

## Product and repository identity

- product/distribution: `Proof Assistant` / `proof-assistant`;
- import package: `proof_assistant`;
- primary executable: `proof-assistant`;
- deprecated 0.1 command alias: `repoprover-codex`;
- user-owned repository: `vitskov/proof-assistant`; and
- version line: 0.1, starting at 0.1.0.

RepoProver is an integration dependency. Never push to, open a pull request
against, or create an issue in `facebookresearch/repoprover`. Do not publish
Proof Assistant or create a release without explicit authorization.

## Interface/backend contracts

Preserve the component silos:

- `proof_assistant.tui` renders and handles input only;
- `proof_assistant.workflow.contracts` defines immutable boundary values and
  `workflow.service.ProofAssistantWorkflow` owns UI-neutral flow/resume
  decisions;
- `proof_assistant.workspace.management` is the distinct backend authority for
  project discovery, destination occupancy, catalog reconciliation, and legacy
  migration; adjacent workspace modules own source observation and staged
  import;
- `proof_assistant.presentation` owns source excerpts and presentation view
  models; and
- `proof_assistant.incremental` owns verification state and certificates.

The main manuscript file is mandatory backend state. Discover candidates
through `WorkflowServiceContract.inspect_source`; pass a validated
source-relative `main_file` in `NewProjectRequest`; and preserve `main_file` and
the resolved `input_files` closure in project summaries, change plans, resume,
progress, and verification. A single-file TUI shortcut may auto-select that
file, but must still call the backend with the explicit value. Never restore an
all-`.tex` implicit index or make the TUI independently resolve `\input` and
`\include` topology.

Do not let a TUI inspect managed directories or parse project configuration.
Use `inspect_project_destination` before creation, render every tagged
`ProjectCatalogEntry`, and call `select_project_main_file` for ambiguous legacy
recovery. The backend must remain the only owner of defaults, occupancy,
migration, and persistence.

No Textual import may cross into backend/workflow code. Do not let TUI widgets,
filesystem notifications, unchecked paths, or model prose become state
authority. Plans must bind the complete source inventory and project generation;
confirmation fails closed when either changed.

The project owns `VERIFY.yaml`. The TUI creates the default task or edits it
internally. Do not reintroduce a user-facing external task-file workflow.

## Provider and proof authority

- Authentication remains inside Codex CLI. Never read/print `auth.json` or
  extract tokens into an API key.
- Validate model/effort against `model/list`.
- Verification children must start without existing MCP servers, apps, plugins,
  bundled skills, or local skills, and startup must fail closed if any remain.
- RepoProver/host tools remain the mutation control plane.
- Only an independently built Lean declaration can create a certificate.
- Unsuccessful proof search is not evidence of falsity.
- Clarification presentation may improve wording but cannot change the selected
  source path/span, quotation, diagnostics, claim, or affected proof graph.

## Safe workflow

1. Resolve and validate paths without printing secrets.
2. Inspect candidates and establish one explicit, validated main file.
3. Observe the full filtered external source until inventories stabilize.
4. Stage, re-hash, resolve the root's input closure, and bind the copy into a
   `ChangeImpactPlan`.
5. Require explicit user confirmation; revalidate immediately before import.
6. Let the incremental engine snapshot/index/invalidate/verify/certify only the
   selected main-file closure.
7. Preserve SQLite, Git snapshots, questions, reports, and Lean history across
   interruptions.
8. Derive resume screens from persisted state; do not repeat unchanged
   clarification questions or start an unrequested iteration.

Never brute-force cache cleanup while jobs are active. Resolve exact targets and
respect reservations/leases.

## Code map

- `src/proof_assistant/tui/` — Textual screens and widgets.
- `src/proof_assistant/workflow/` — UI-neutral state machine and contracts.
- `src/proof_assistant/workspace/` — source/project/task management.
- `src/proof_assistant/presentation/` — findings/clarification view models.
- `src/proof_assistant/incremental/` — snapshots, graphs, state, scheduling,
  certification, and reports.
- `src/proof_assistant/backend.py` — Codex app-server isolation and turns.
- `src/proof_assistant/cache.py` and `cache_index.py` — bounded shared storage.
- `src/proof_assistant/cli.py` — `proof-assistant` command surface.
- `tests/` — contract, unit, pilot TUI, integration, and regression tests.

Some directories may be consolidated during implementation, but the dependency
direction and contracts must remain explicit and testable.

## Validation and handoff

```bash
cd "$HOME/src/proof-assistant"
uv pip install --python "$HOME/.venvs/proof-assistant/bin/python" -e '.[dev]'
proof-assistant compiler-check
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
git diff --check
```

For packaging/install changes, also run the supported installer and a fresh
Python 3.13 wheel check outside Dropbox. For workflow/TUI changes, run headless
screen tests plus a small real manuscript loop.

Report actual tests, versions, paths, active state, Git status, and whether
anything was committed/pushed. Explicitly state that no upstream RepoProver PR
was created.
