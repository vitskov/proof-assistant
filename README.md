# repoprover-codex

`repoprover-codex` connects
[`facebookresearch/repoprover`](https://github.com/facebookresearch/repoprover)
to the locally authenticated Codex CLI. It can verify a free-form task against
a folder of LaTeX manuscript sources and retain the Lean evidence, report,
logs, and Git history in a caller-selected output folder.

This is an independently maintained project. It does not modify upstream
RepoProver, read Codex authentication files, extract OAuth tokens, or require
an `OPENAI_API_KEY`.

## Quick start

Requirements: macOS or Linux, Codex CLI already logged in, `uv`, Lean/Lake,
Git, and a native C compiler.

```bash
git clone https://github.com/vitskov/repoprover-codex.git "$HOME/src/repoprover-codex"
cd "$HOME/src/repoprover-codex"
scripts/install-dev.sh
```

The installer uses Python 3.13 and uv, creates the environment at
`$HOME/.venvs/repoprover-codex`, performs a real compile-and-execute compiler
check, initializes the bounded cache, and runs the tests. Python environments
and Lean caches are rejected if they resolve into Dropbox.

Check the installation:

```bash
repoprover-codex doctor
repoprover-codex models
repoprover-codex cache doctor
repoprover-codex cache status
```

Run a manuscript verification:

```bash
repoprover-codex manuscript-run \
  --manuscript "$MANUSCRIPT" \
  --task-file "$TASK" \
  --output "$OUTPUT" \
  --model gpt-5.6-sol \
  --effort high \
  --turn-timeout 86400
```

`$TASK` is a UTF-8 text or Markdown file containing the complete free-form
verification request. `$OUTPUT` must be new or empty and outside Dropbox. The
input manuscript is copied and never modified.

## What is implemented

- Persistent `codex app-server` sessions using the existing Codex login.
- Exact model and reasoning-effort validation against `model/list`.
- RepoProver tools exposed as Codex dynamic tools.
- Child-process isolation from local MCP servers, apps, plugins, and skills.
- File-based manuscript, task, and output interface.
- Independent final Lean build and evidence-based result classification.
- Shared content-addressed Mathlib/REPL dependency depots across projects whose
  package names and source roots differ but whose dependency declarations match.
- Isolated per-project root builds with process-held cache leases.
- Transactional capacity reservations and a persistent coarse-grained cache
  index with crash recovery.
- Deadline-bounded garbage collection, a 16 GiB default cache ceiling, and a
  25 GiB default filesystem free-space reserve.
- macOS and Linux local-mode support.

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Verify Codex app-server connectivity and authentication. |
| `models` | List exact models and supported effort levels. |
| `smoke` | Run a real Codex dynamic-tool round trip. |
| `compiler-check` | Compile and execute a native test program. |
| `manuscript-run` | Verify a task file against a manuscript folder. |
| `repoprover-prove` | Run one RepoProver PROVE task on an existing Lean project. |
| `cache status` | Show cache usage, limits, and disk headroom. |
| `cache prepare` | Prepare and build a Lean project without starting Codex. |
| `cache gc` | Enforce limits with bounded, coarse-unit eviction. |

## Storage model

Large Lean dependencies are stored once per compatible dependency fingerprint:

```text
~/.cache/repoprover-codex/
├── cache-index.sqlite3                    # transactional accounting
├── lake/dependencies/deps-<fingerprint>/  # shared Mathlib/REPL depot
├── lake/builds/<project>-<path-hash>/     # small isolated root build
├── mathlib-downloads/
├── lake/system/
├── locks/
├── trash/                                 # atomic GC quarantine
└── config.json                            # compiler and disk policy
```

For standard `lakefile.lean` Git requirements, the fingerprint uses normalized
dependency name/URL/revision triples rather than project package declarations
or source roots. It also includes the Lean toolchain, operating system,
architecture, and native compiler identity. Unknown Lake syntax falls back to
a conservative whole-file hash. Compatible projects share the dependency
depot but never their manuscript/root build products.

Before expensive setup, an admission lock makes cleanup and a conservative
capacity reservation atomic. The index contains one row per build, depot, or
bulk cache—not one row per Mathlib archive. Changed entries are measured once;
the eviction loop never recursively rescans the cache. Deletion first renames a
whole unit into `trash/`, then traverses it once with a deadline and progress
messages. Active entries are protected by advisory locks that the operating
system releases if a process exits or is killed; stale reservations and
interrupted quarantines are recovered automatically.

The default GC deadline is 900 seconds and can be changed per command with
`--gc-timeout`. If accounting or deletion reaches that deadline, the command
fails explicitly and leaves any partial deletion isolated in `trash/` for the
next bounded recovery pass.

## Documentation

- [Installation and upgrades](docs/INSTALLATION.md)
- [Running manuscript verification](docs/MANUSCRIPT_RUNS.md)
- [Cache and storage guarantees](docs/CACHE_AND_STORAGE.md)
- [Architecture and security boundary](docs/ARCHITECTURE.md)
- [Development and testing](docs/DEVELOPMENT.md)
- [Latest tested configuration](TEST_REPORT.md)

`CODEX_HANDOFF.md` is retained as historical development context, not as the
primary operating manual.

## Project status

Version 0.4.0 is tested locally on macOS with Python 3.13, Lean 4.28, Lake 5,
and the pinned RepoProver checkout recorded in `TEST_REPORT.md`.

No pull request, issue, or push has been made to
`facebookresearch/repoprover`.
