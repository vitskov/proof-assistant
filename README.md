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
- Shared content-addressed Mathlib/REPL dependency depots.
- Isolated per-project root builds with process-held cache leases.
- LRU garbage collection, a 16 GiB default cache ceiling, and a 25 GiB
  default filesystem free-space reserve.
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
| `cache gc` | Enforce limits by evicting inactive LRU entries. |

## Storage model

Large Lean dependencies are stored once per compatible dependency fingerprint:

```text
~/.cache/repoprover-codex/
├── lake/dependencies/deps-<fingerprint>/  # shared Mathlib/REPL depot
├── lake/builds/<project>-<path-hash>/     # small isolated root build
├── mathlib-downloads/
├── lake/system/
├── locks/
└── config.json                            # compiler and disk policy
```

The fingerprint includes the Lake configuration, Lean toolchain, operating
system, architecture, and native compiler identity. Compatible projects share
the dependency depot but never their manuscript/root build products. Active
entries are protected by advisory locks that the operating system releases if
a process exits or is killed.

Before expensive setup, the package evicts inactive least-recently-used cache
entries until both configured limits are satisfied. If active entries prevent
that, the run fails before downloading or compiling rather than filling the
disk.

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

Version 0.3.0 is tested locally on macOS with Python 3.13, Lean 4.28, Lake 5,
and the pinned RepoProver checkout recorded in `TEST_REPORT.md`.

No pull request, issue, or push has been made to
`facebookresearch/repoprover`.
