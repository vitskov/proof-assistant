# Running manuscript verification

## Inputs and outputs

A run has three caller-controlled paths:

- `--manuscript`: folder containing `.tex` or `.ltx` sources;
- `--task-file`: UTF-8 file containing the authoritative free-form request;
- `--output`: new or empty folder for all durable results.

Example environment variables:

```bash
export MANUSCRIPT=/absolute/path/to/manuscript
export TASK="$HOME/repoprover-tasks/check-all-claims.md"
export OUTPUT="$HOME/repoprover-runs/manuscript-001"
```

Run:

```bash
repoprover-codex manuscript-run \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --output "$OUTPUT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

`86400` is one day in seconds. It limits the Codex turn, not dependency setup.

## Task-file example

```markdown
Read the complete manuscript. Identify every claimed lemma, proposition, and
theorem. Formalize and check each claim in Lean, preserving every stated
assumption. Report which claims have complete Lean proofs and clearly separate
unverified claims from false claims. Do not use sorry, admit, or new axioms.
```

The task file may contain multiple paragraphs, assumptions, exclusions,
preferred formal statements, and acceptance criteria.

## Input handling

The command copies the manuscript into an isolated Git workspace. It excludes
source `.git`, `.lake`, Python environments, common caches, LaTeX build output,
`.env*`, and `auth.json`. The source folder is never modified.

For a LaTeX-only manuscript, the package creates a pinned Lean 4.28 project.
For a folder that is already a Lean/Lake project, its source layout is retained.

## Output layout

```text
output/
├── TASK.md
├── INPUT_MANIFEST.json
├── VERIFICATION_REPORT.md
├── RUN_STATUS.json
├── workspace/                    # sources, Lean evidence, and Git history
└── artifacts/
    ├── setup.log
    ├── setup.json
    ├── final.md
    ├── tool-calls.json
    ├── events.json
    ├── verification-build.log
    └── result.json
```

Large Mathlib and REPL state is not copied into the output. The workspace's
`.lake` points to a small isolated project build, whose dependency directory
points to a compatible shared depot.

## Result semantics

An agent's `-- VERIFIED` marker is insufficient by itself. Exit status 0 also
requires:

- a successful RepoProver `lean_check`;
- a nonempty verification report;
- a new result commit;
- a clean result workspace; and
- an independent successful final `lake build`.

`unverified`, `blocked`, `incomplete`, `provider_failure`, `setup_failure`, and
`tool_failure` remain distinct. Failure to verify is never reported as proof
that a mathematical statement is false.

## Before a large run

```bash
repoprover-codex cache status
repoprover-codex cache doctor
```

The run performs its own mandatory capacity preflight even if these commands
are not run manually.
