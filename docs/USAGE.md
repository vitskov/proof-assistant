# Usage guide

## Choose the three durable inputs

- `MANUSCRIPT`: the authoritative folder containing `.tex` or `.ltx` source;
- `TASK`: a UTF-8 free-form file or structured `VERIFY.yaml`; and
- `PROJECT`: a new persistent verification folder outside Dropbox.

The project is not disposable output. It is the resume handle and contains the
reusable Lean library, source history, questions, and certificates.

```bash
export MANUSCRIPT=/absolute/path/to/paper
export TASK="$HOME/repoprover-tasks/paper.md"
export PROJECT="$HOME/repoprover-projects/paper"
```

See [Task files](TASK_FILES.md) for free-form and YAML examples.

## Check the installation

```bash
repoprover-codex compiler-check
repoprover-codex cache doctor
repoprover-codex doctor
repoprover-codex models
```

Choose an exact model/effort pair printed by `models`.

## Initialize once

```bash
repoprover-codex manuscript init \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --project "$PROJECT"
```

Initialization validates disjoint paths, filters runtime files and secrets,
creates a private content-addressed source snapshot, structurally indexes
mathematical objects, extracts explicit LaTeX references, creates stable
per-claim Lean modules, initializes SQLite state, and commits the project.

Use `manuscript graph` to see generated IDs before writing a targeted YAML task:

```bash
repoprover-codex manuscript graph --project "$PROJECT" --format dot
```

## Verify and resume

```bash
repoprover-codex manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

The project remembers the manuscript and task paths, so they are optional on
later passes. Supplying `--manuscript` or `--task-file` updates the project’s
authoritative configuration.

Every pass:

1. records a new source snapshot or detects byte-identical source;
2. computes statement/proof changes and the affected dependency slice;
3. independently rebuilds and revalidates unchanged certificates;
4. schedules only ready uncertified claims;
5. runs each proof batch in an isolated Git worktree;
6. merges allowed claim modules in deterministic order;
7. independently builds the merged project and extracts Lean declarations;
8. records certificates, questions, diagnostics, and the dependency audit.

No model resume token is needed. The project is the persistent state.

## Monitor a live pass

```bash
repoprover-codex manuscript status --project "$PROJECT"
repoprover-codex manuscript questions --project "$PROJECT"
```

`status` remains readable while a verification writer owns the project lock.
It reports `mutation in progress: yes`, the active run, certificate count, and
claim states. Per-run setup, agent, tool-call, diff, environment, and build
artifacts appear under `.repoprover/runs/NNNNNN/`.

A quiet process may be compiling a small isolated root, waiting for Codex, or
extracting elaborated proof terms. See [Troubleshooting](TROUBLESHOOTING.md)
before deciding a run is stuck.

## Respond to a clarification

When the command exits 10, read:

```bash
sed -n '1,240p' "$PROJECT/CLARIFICATION_REQUEST.md"
```

Edit the original `$MANUSCRIPT` source to make the intended statement,
assumption, notation, or proof step explicit. Then rerun the exact same verify
command. The changed source object supersedes its old question; unchanged
certified branches remain available.

Explicit dismissal is available when no source edit is appropriate:

```bash
repoprover-codex manuscript questions \
  --project "$PROJECT" \
  --dismiss Q0007 \
  --reason "The theorem is intentionally outside the verification scope."
```

## Review formal correspondence

Most tasks permit agent-proposed claim-to-Lean mappings, which the host still
validates mechanically. For mandatory human review, set
`require_statement_correspondence_review: true` in YAML.

```bash
repoprover-codex manuscript correspondence --project "$PROJECT"
sed -n '1,240p' "$PROJECT/Formalization/Claims/Claim_...lean"
repoprover-codex manuscript correspondence \
  --project "$PROJECT" \
  --approve thm:main
repoprover-codex manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol
```

Approval does not itself create a certificate. The next pass must independently
build and extract the approved declaration.

## Parallel proof batches

`--jobs 1` is the conservative default. `--jobs 2` runs up to two independent
ready batches concurrently, matching the package’s Codex concurrency limit.
`--batch-size` controls how many independent ready claims one agent receives.

```bash
repoprover-codex manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol \
  --jobs 2 \
  --batch-size 4
```

Each process has its own Lean REPL pool and root build. Raising
`--lean-pool-size` can increase memory pressure; it does not increase Codex
batch concurrency.

## Results and exit codes

Read these files in order:

1. `VERIFICATION_STATUS.md` — concise current state;
2. `CLARIFICATION_REQUEST.md` — open author questions;
3. `VERIFICATION_REPORT.md` — claim table, reuse, and dependency audit;
4. `.repoprover/exports/certificates.json` — canonical certificate ledger; and
5. `.repoprover/runs/NNNNNN/run.json` — one pass’s terminal result.

Incremental exit codes:

| Code | Meaning |
|---:|---|
| 0 | all selected targets certified |
| 10 | clarification required; completed work preserved |
| 11 | partial or inconclusive verification; not a falsity conclusion |
| 12 | kernel-checked counterexample for a selected target |
| 20 | setup/project failure |
| 21 | Codex provider/protocol failure |
| 22 | Lean infrastructure/build/extraction failure |

## Manual inspection and invalidation

```bash
repoprover-codex manuscript diff --project "$PROJECT"
repoprover-codex manuscript audit --project "$PROJECT"
repoprover-codex manuscript invalidate \
  --project "$PROJECT" \
  --claim lem:restriction \
  --include-dependents
```

Invalidation changes certificate state but does not delete Lean proof source.

## Legacy one-shot workflow

`manuscript-run` remains available as a low-level compatibility primitive. It
requires a new or empty `--output` and does not provide cross-run claim graphs,
clarification resume, or statement-level certificate reuse. Use the persistent
`manuscript init`/`verify` workflow for new work.
