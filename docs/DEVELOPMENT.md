# Development and testing

## Non-negotiable local rules

- Use Python 3.13 and uv.
- Keep Python environments and Lean caches outside Dropbox.
- Every installer run must compile and execute a native test program.
- Never push to or open a pull request against `facebookresearch/repoprover`.

## Fast test cycle

```bash
cd "$HOME/src/repoprover-codex"
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" -e '.[dev]'
repoprover-codex compiler-check
"$HOME/.venvs/repoprover-codex/bin/python" -m pytest -q
```

## Local integration checks

```bash
repoprover-codex cache doctor
repoprover-codex cache status
repoprover-codex doctor
repoprover-codex models
repoprover-codex smoke --model MODEL --effort EFFORT
```

Run smoke once with `OPENAI_API_KEY` removed to prove that Codex's existing
login is sufficient.

## Real cache-sharing acceptance

Create two small Lean projects with identical `lean-toolchain` and lakefile
dependency configuration but different absolute paths. Each should contain a
trivial theorem such as `2 + 2 = 4` proved by `norm_num`.

```bash
repoprover-codex cache prepare --project /path/to/project-a
repoprover-codex cache prepare --project /path/to/project-b
```

Acceptance criteria:

1. Both commands finish with `lake build: OK`.
2. Project B prints `dependency depot reused: yes`.
3. Both `.lake/packages` links resolve to the same depot.
4. Their top-level `.lake` links resolve to different project-build paths.
5. The second project adds only its small root build, not another Mathlib tree.
6. `cache status` remains below both disk limits.
7. A generated manuscript workspace with the same dependency revisions prints
   the same dependency key despite its different package name and source roots.

## Cache-GC regression checks

`tests/test_cache.py` includes the former pathological shape directly: 10,000
Mathlib download files plus a capacity violation. The test asserts one coarse
candidate, one recursive measurement, and an empty recreated download root.
Additional cases cover:

- two simultaneous reservations that would exceed the configured maximum;
- stale reservation recovery through a released OS lease;
- active entries that must not be evicted;
- interrupted quarantine recovery;
- index schema migration and dirty-entry crash state; and
- expired accounting and deletion deadlines.

These are operation-count assertions, not timing-only benchmarks, so a fast
machine cannot hide a reintroduction of the nested-rescan algorithm.

## Real RepoProver/Codex checks

Use a model/effort pair printed by `models`, then run one deliberately trivial
RepoProver proof and one file-based manuscript task. Successful manuscript exit
requires the separate final Lean build and all evidence conditions documented
in the [Usage guide](USAGE.md#results-and-evidence).

Only after single-agent success, run exactly two concurrent Codex-backed
agents. This is a local-mode package target; SLURM is not required on macOS.

## Incremental feedback-loop acceptance

Use the golden manuscript under `tests/fixtures/incremental_manuscript` and a
fresh persistent project outside Dropbox. Acceptance requires:

1. initialization indexes two claims and the explicit theorem-to-lemma edge;
2. the first pass schedules the lemma before its dependent theorem;
3. each proposal is merged only in its assigned claim module;
4. independent `lake build` plus the Lean environment extractor produces two
   certificates with structural type/value hashes;
5. an unchanged second pass starts no Codex app-server and reports two reused
   certificates; and
6. `manuscript status` remains readable while the writer lock is active.

For source-change tests, cover independent branches, proof-only edits in both
modes, identical formal-type reconciliation, changed assumptions, structured
clarification/supersession, and a certified counterexample fixture. Do not
replace real Lean extraction with source-text dependency inference.

## Release checks

The current release line is `0.4.x` (currently 0.4.1). Increment patch versions conservatively;
do not enter the `0.5.x` series without explicit user authorization.

```bash
git diff --check
"$HOME/.venvs/repoprover-codex/bin/python" -m pytest -q
uv build --python "$HOME/.venvs/repoprover-codex/bin/python"
```

Install the wheel into a fresh external Python 3.13 environment, run
`compiler-check`, `cache doctor`, and the tests, then audit the repository for
credentials, environments, caches, build artifacts, and temporary files before
pushing only to the user-owned repository.
