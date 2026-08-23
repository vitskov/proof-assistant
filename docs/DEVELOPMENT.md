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

## Real RepoProver/Codex checks

Use a model/effort pair printed by `models`, then run one deliberately trivial
RepoProver proof and one file-based manuscript task. Successful manuscript exit
requires the separate final Lean build and all evidence conditions documented
in [MANUSCRIPT_RUNS.md](MANUSCRIPT_RUNS.md).

Only after single-agent success, run exactly two concurrent Codex-backed
agents. This is a local-mode package target; SLURM is not required on macOS.

## Release checks

```bash
git diff --check
"$HOME/.venvs/repoprover-codex/bin/python" -m pytest -q
uv build
```

Install the wheel into a fresh external Python 3.13 environment, run
`compiler-check`, `cache doctor`, and the tests, then audit the repository for
credentials, environments, caches, build artifacts, and temporary files before
pushing only to the user-owned repository.
