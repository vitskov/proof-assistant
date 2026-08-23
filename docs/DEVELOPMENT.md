# Development and testing

## Non-negotiable local rules

- Use Python 3.13 and `uv` whenever feasible.
- Keep Python environments and Lean/Lake/Mathlib caches outside Dropbox.
- Keep managed Proof Assistant projects outside Dropbox.
- Every installer must compile and execute a native test program.
- Never push to, open a pull request against, or create an issue in
  `facebookresearch/repoprover`.

The external manuscript source may be in Dropbox; it is imported through the
stable-source contract. Do not weaken this distinction into a blanket source
rejection.

## Fast test cycle

```bash
cd "$HOME/src/proof-assistant"
uv pip install --python "$HOME/.venvs/proof-assistant/bin/python" -e '.[dev]'
proof-assistant compiler-check
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
```

## Architectural rules

The TUI, workflow, workspace/presentation, and incremental verification layers
have strict boundaries described in [Architecture](ARCHITECTURE.md).

- `proof_assistant.tui` may render typed view models and send commands; it must
  not implement project or verification authority.
- UI-neutral services must not import Textual or Rich widgets.
- Stable source observation must yield a content-bound inventory/change plan;
  filesystem events alone cannot authorize import.
- Confirmation must reject a stale source inventory or project generation.
- Resume behavior comes from persisted state, not a remembered screen.
- Codex clarification presentation cannot alter deterministic question facts.

Favor contract tests at each boundary and integration tests that use fakes only
at the next external boundary.

## Local integration checks

```bash
proof-assistant cache doctor
proof-assistant cache status
proof-assistant doctor
proof-assistant models
proof-assistant smoke --model MODEL --effort EFFORT
```

Run smoke once with `OPENAI_API_KEY` removed to demonstrate that the existing
Codex login is sufficient.

## TUI and workflow acceptance

Test the Textual app with its pilot/headless driver. Cover:

1. one-file automatic main-file announcement, multi-file explicit root
   selection, and resume without reselection;
2. new-project and resume selection;
3. default and customized project-owned task;
4. external Dropbox-source warning without rejecting the source;
5. rejection of Dropbox managed project destinations;
6. a progress view that lists the main/input closure, shows every typed stage,
   and exposes selectable/copyable read-only text;
7. clarification rendering with the actual multi-file source path and
   highlighted lines;
8. stable multi-file change detection and complete impact preview;
9. explicit confirmation, plus rejection/recomputation of stale plans;
10. no-change resume returning to the existing clarification screen;
11. interrupted/failure recovery and read-only active-project status; and
12. findings categories and evidence paths.

Backend source tests must cover rapid editor-style replace sequences,
simultaneous changes to several `\input` files, adds/deletes/renames, and staged
copy mutation. They must also cover nested/cyclic input closure, alternate and
orphan roots, missing/dynamic/escaping includes, and duplicate labels outside
the selected closure. Do not make wall-clock sleeps the correctness mechanism.

## Incremental verification acceptance

Use the golden manuscript under `tests/fixtures/incremental_manuscript` and a
fresh project outside Dropbox. Acceptance requires:

1. the initial stable import indexes expected claims and edges;
2. dependencies are proved before descendants;
3. workers may change only assigned claim modules;
4. independent `lake build` and environment extraction issue certificates;
5. an unchanged pass starts no Codex app-server and reuses certificates;
6. one changed lemma invalidates its complete reverse-dependent closure but no
   unrelated branch; and
7. project status remains readable during a writer lock.

Source tests should distinguish proof-only edits in theorem/argument-audit
modes, identical formal-type reconciliation, changed assumptions, structured
clarification/supersession, suspected-false state, and certified
counterexamples. Never replace real Lean extraction with source-text inference.

## Cache regression checks

The former pathological case—thousands of Mathlib download files under disk
pressure—must remain one coarse GC candidate and one recursive measurement.
Keep operation-count tests for reservations, active leases, quarantine recovery,
deadline enforcement, and index migration.

Two projects with compatible Lake dependency configuration must reuse one
dependency depot while retaining independent small root builds:

```bash
proof-assistant cache prepare --project /path/to/project-a
proof-assistant cache prepare --project /path/to/project-b
```

The cache path remains `$HOME/.cache/repoprover-codex`; changing it as part of
branding is a regression because it duplicates Mathlib data.

## Release checks

The product version line starts at 0.1.0. Increment it deliberately and keep the
distribution/import/repository names `proof-assistant` / `proof_assistant` /
`vitskov/proof-assistant` aligned.

```bash
git diff --check
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
uv build --python "$HOME/.venvs/proof-assistant/bin/python"
```

Run `scripts/install-dev.sh`; install the wheel in a fresh external Python 3.13
environment; run compiler, package-resource, CLI/TUI smoke, cache doctor, and a
small real Lean acceptance. Audit secrets, credentials, environments, caches,
artifacts, and temporary files before any authorized publication.

Do not publish or create a release without explicit user authorization. Any
authorized push is to the user-owned Proof Assistant repository only.
