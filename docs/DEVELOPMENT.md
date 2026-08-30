# Development and testing

## Non-negotiable local rules

- Use Python 3.13 and `uv` whenever feasible.
- Keep Python environments and Lean/Lake/Mathlib caches outside Dropbox.
- Keep managed Proof Assistant projects outside Dropbox.
- Every installer must compile and execute a native test program.
- Shell configuration changes must be append-only and idempotent. For Bash,
  update its existing effective login file and `.bashrc`; never create a
  higher-priority login file that shadows `.profile`. Honor `ZDOTDIR` for zsh
  and `XDG_CONFIG_HOME` for fish, use runtime-guarded PATH additions, and refuse
  broken symlinks or non-regular startup targets.
- The reproducible dependency and quality-control tooling does not edit shell
  startup files. `scripts/bootstrap-uv.sh` sets `UV_NO_MODIFY_PATH=1`, installs
  into a task-specific directory, and fails if `.bash_profile`, `.bashrc`,
  `.profile`, `.zprofile`, or `.zshrc` changes. This rule applies on Linux and
  macOS; developers add a desired executable directory to `PATH` themselves.
- Compiler validation must exercise standard headers and `lean/lean.h` through
  `leanc`; never export Lean's bundled `bin/clang` as `LEAN_CC`.
- Resolve Lean's toolchain from the target project's directory so its
  `lean-toolchain` pin, not the caller's current directory, controls validation.
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
"$HOME/.venvs/proof-assistant/bin/python" scripts/check_typing_policy.py
"$HOME/.venvs/proof-assistant/bin/python" -m mypy
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
```

Follow [Python 3.13 development style](PYTHON_STYLE.md) for strict typing,
validated JSON boundaries, selective dataclass slots, and recommendations for
reducing the remaining dynamic surface.

## Reproducible Python 3.13 environment

CI on Linux and macOS uses the committed hash-locked inputs. Bootstrap the
repository-pinned `uv` 0.12.0 binary, create an unseeded external environment,
install the pinned build tools, sync the reviewed development lock, and then
install the checkout without dependency re-resolution:

```bash
cd "$HOME/src/proof-assistant"
work_dir="$(mktemp -d)"
uv_bin="$(scripts/bootstrap-uv.sh "$work_dir/uv")"
"$uv_bin" venv --python 3.13 "$work_dir/venv"
"$uv_bin" pip install --python "$work_dir/venv/bin/python" \
  --require-hashes --only-binary=:all: -r requirements/py313-build.lock
"$uv_bin" pip sync --python "$work_dir/venv/bin/python" \
  --require-hashes --strict --no-build-isolation requirements/py313-dev.lock
"$uv_bin" pip install --python "$work_dir/venv/bin/python" \
  --no-deps --no-build-isolation -e .
"$uv_bin" pip check --python "$work_dir/venv/bin/python"
```

`scripts/bootstrap-uv.sh` selects the reviewed Linux x86-64, macOS arm64, or
macOS x86-64 release artifact and verifies it against
`requirements/uv-0.12.0-sha256.txt`. It does not run Astral's remote installer,
invoke an updater, or edit shell startup files. Refresh the two lock files only
as a deliberate dependency change:

```bash
scripts/refresh-dev-lock.sh
git diff -- requirements/py313-build.lock requirements/py313-dev.lock
```

Do not use `pip sync` on a shared or active environment that contains unrelated
packages or an editable RepoProver checkout. Use the clean environment above;
deployment to an existing environment requires a separately reviewed
preserving install.

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
- AI clarification presentation cannot alter deterministic question facts.
- `proof_assistant.ai` owns provider selection, credential indirection,
  catalog provenance, task policy, and provider execution. Do not route around
  it through RepoProver's legacy provider client or a TUI-only setup path.

Favor contract tests at each boundary and integration tests that use fakes only
at the next external boundary.

## Local integration checks

```bash
proof-assistant cache doctor
proof-assistant cache status
proof-assistant doctor
proof-assistant models
proof-assistant smoke --model MODEL --effort EFFORT
proof-assistant ai status
```

Run smoke once with `OPENAI_API_KEY` removed to demonstrate that the existing
Codex login is sufficient for the Codex compatibility path. Provider setup and
execution tests must fake CLI/HTTP/keyring boundaries by default; do not consume
Copilot quota or API credits in an ordinary test suite. A real provider test
requires explicit authorization and must record which provider traffic it used.

Provider regression coverage must prove that settings cannot contain secrets,
credential submissions are one-shot/redacted, model catalogs retain their
live-versus-fallback source, install plans require exact consent, and all
provider tool calls return through the common host admission boundary.

## TUI and workflow acceptance

The reviewed lock resolves Textual 8.2.8 and textual-dev 1.8.0. The declared
bounds are Textual `>=8.2.8,<9` and `textual-dev>=1.8,<2`; visual regression
tests pin `pytest-textual-snapshot==1.1.0`.

Run the deterministic, service-free responsive settings fixture with Textual's
live console from two terminals:

```bash
# Terminal 1
textual console
```

```bash
# Terminal 2, from the repository root
textual run --dev tests/textual_dev_app.py
```

The fixture shows the eight-role Claude layout, including Fable / extra-high
for the Independent prove agent, without connecting to a provider. Use it to
inspect focus, resize behavior, and Textual log messages. Record diagnostics
with:

```bash
python -m textual_dev diagnose
```

The snapshot gate covers Proof Ink and Proof Paper at 80×24, 120×40, and
140×48. Review SVG changes rather than updating them blindly:

```bash
python -m pytest -q tests/test_tui_layout.py tests/test_tui_snapshots.py
# After deliberate visual review only:
python -m pytest -q tests/test_tui_snapshots.py --snapshot-update
```

Test the Textual app with its pilot/headless driver. Cover:

1. one-file automatic main-file announcement, multi-file explicit root
   selection, and resume without reselection;
2. reconciled resumable, needs-main-file, incomplete, and occupied catalog rows;
3. backend-only legacy main-file recovery and destination preflight;
4. new-project and resume selection;
5. default and customized project-owned task;
6. external Dropbox-source warning without rejecting the source;
7. rejection of Dropbox managed project destinations;
8. a progress view that lists the main/input closure, shows every typed stage,
   and exposes selectable/copyable read-only text;
9. clarification rendering with the actual multi-file source path and
   highlighted lines;
10. stable multi-file change detection and complete impact preview;
11. explicit confirmation, plus rejection/recomputation of stale plans;
12. no-change resume returning to the existing clarification screen;
13. interrupted/failure recovery and read-only active-project status; and
14. findings categories and evidence paths; and
15. an 80×24 terminal-native report viewer with rendered Markdown, a selectable
    source tab, no OS opener call, normalized load errors, and Back/Close
    navigation; and
16. a permanent context-sensitive command footer, F1 shortcut reference,
    editable-field-safe `?`, command palette, and WCAG-checked light/dark
    semantic themes at both ordinary and 80×24 terminal sizes; and
17. all eight AI role rows at 120×40, explicit machine/project scope,
    provider-default and one-level Undo behavior, selection preservation, and
    removal of secret input values from the DOM when leaving the connection
    view.

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
"$HOME/.venvs/proof-assistant/bin/python" scripts/check_typing_policy.py
"$HOME/.venvs/proof-assistant/bin/python" -m mypy
"$HOME/.venvs/proof-assistant/bin/python" -m pytest -q
uv build --python "$HOME/.venvs/proof-assistant/bin/python"
```

Run `scripts/install-dev.sh`; install the wheel in a fresh external Python 3.13
environment; run compiler, package-resource, CLI/TUI smoke, cache doctor, and a
small real Lean acceptance. Audit secrets, credentials, environments, caches,
artifacts, and temporary files before any authorized publication.

Do not publish or create a release without explicit user authorization. Any
authorized push is to the user-owned Proof Assistant repository only.
