# Usage guide

## Choose the inputs

Every manuscript run has three caller-controlled paths:

- `--manuscript`: a folder containing `.tex` or `.ltx` manuscript sources;
- `--task-file`: a UTF-8 text or Markdown file containing the authoritative
  free-form request; and
- `--output`: a new or empty folder for all durable results.

The manuscript is treated as read-only. The package copies it into an isolated
Git workspace and excludes source Git metadata, `.lake`, Python environments,
common caches, LaTeX build output, `.env*`, and `auth.json`.

For a LaTeX-only manuscript, the package creates a pinned Lean project. For an
existing Lean/Lake manuscript, it retains that source layout.

## Write the task

The task file can contain multiple paragraphs, assumptions, exclusions,
preferred formal statements, and acceptance criteria. For example:

```markdown
Read the complete manuscript. Identify every claimed lemma, proposition, and
theorem. Formalize and check each claim in Lean, preserving every stated
assumption. Report which claims have complete Lean proofs and clearly separate
unverified claims from false claims. Do not use sorry, admit, or new axioms.
```

The repository also contains [a reusable task file](../examples/verify-task.md).
Be explicit about scope: “all claimed statements” can be substantially more
expensive than checking one named theorem.

## Check the installation and model

```bash
repoprover-codex doctor
repoprover-codex models
repoprover-codex cache doctor
repoprover-codex cache status
```

Choose an exact model and effort pair printed by `models`. `cache status` is
optional because every run performs its own mandatory admission check, but it
is useful before a large job.

## Run the manuscript

```bash
export MANUSCRIPT=/absolute/path/to/manuscript
export TASK="$HOME/repoprover-tasks/check-all-claims.md"
export OUTPUT="$HOME/repoprover-runs/manuscript-001"

repoprover-codex manuscript-run \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --output "$OUTPUT" \
  --model gpt-5.6-sol \
  --effort high \
  --gc-timeout 900 \
  --turn-timeout 86400
```

The command prints the selected output, generated workspace, input mode, cache
location, dependency reuse state, and native compiler before entering the
Codex turn.

## Timeouts and Lean workers

The important limits are independent:

- `--turn-timeout`: maximum Codex turn duration; `86400` is one day;
- `--request-timeout`: timeout for an individual app-server request;
- `--setup-timeout`: timeout for each dependency, bootstrap, or final-build
  command; and
- `--gc-timeout`: total cache admission/reconciliation/deletion budget, 900
  seconds by default.

`--lean-pool-size` controls RepoProver's Lean REPL worker count and defaults to
one. Raising it can improve tool-call concurrency but also increases memory
pressure. The package permits at most two active Codex turns in one process;
independent command processes may run concurrently if disk reservations fit.

## Monitor a run

`RUN_STATUS.json` exists before expensive setup and records one of these live
phases:

1. `cache_preflight`
2. `cache_gc`
3. `dependency_setup`
4. `codex_turn`

Inspect it without modifying the run:

```bash
sed -n '1,120p' "$OUTPUT/RUN_STATUS.json"
```

The file changes to the terminal outcome when the command finishes. During a
long `codex_turn`, final event and tool-call files may not appear until the turn
returns; a quiet artifact directory alone does not prove the process is stuck.
See [Troubleshooting and operations](TROUBLESHOOTING.md) for deeper checks.

## Results and evidence

```text
output/
├── TASK.md
├── INPUT_MANIFEST.json
├── VERIFICATION_REPORT.md
├── RUN_STATUS.json
├── workspace/                    # snapshot, Lean evidence, Git history
└── artifacts/
    ├── setup.log
    ├── setup.json
    ├── final.md
    ├── tool-calls.json
    ├── events.json
    ├── verification-build.log
    └── result.json
```

Start with `VERIFICATION_REPORT.md`, then confirm the outcome in
`artifacts/result.json`. The workspace preserves the exact formalization and
commits used to obtain that result.

An agent-written “verified” marker is not sufficient. Exit status 0 also
requires:

- at least one successful RepoProver `lean_check`;
- a nonempty verification report;
- a new result commit;
- a clean result workspace; and
- an independent successful final `lake build`.

The terminal categories include verified, unverified, incomplete,
formalization mismatch, provider failure, setup failure, and tool failure.
Unverified does not mean false.

Even a verified Lean theorem establishes the generated formal statement, not
automatically the fidelity of its translation from prose. Review each reported
claim, formal statement, assumptions, and source location.

## Stop or rerun

An interrupt stops the launcher and leaves its current output for inspection.
The cache reservation and process-held leases are released by the operating
system; the next cache operation recovers stale accounting automatically.

A new run requires a new or empty output folder. Preserve a stopped output if
you want its partial Git history or logs. Otherwise remove that exact output
folder or choose a new `$OUTPUT`; never delete the shared cache while another
run is active.

## Existing Lean projects

To prepare and build a Lean project without starting Codex:

```bash
repoprover-codex cache prepare --project /absolute/path/to/project
```

To run one named RepoProver `PROVE` task against a prepared project, use
`repoprover-prove`; see the [command reference](COMMAND_REFERENCE.md).
