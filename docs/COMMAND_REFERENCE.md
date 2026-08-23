# Command reference

Run `repoprover-codex COMMAND --help` for the exact installed parser. Global
`--codex` and `--cache-home` options appear before the subcommand.

## Persistent manuscript commands

### `manuscript init`

Creates a new persistent project. Requires `--manuscript`, `--task-file`, and
`--project`. The project must be new/empty, disjoint from the manuscript, on
local storage, inside the user home, and outside Dropbox.

### `manuscript verify`

Runs one incremental pass. Requires `--project` and `--model`; the configured
manuscript/task paths are reused unless overridden.

Controls:

- `--effort`
- `--jobs 1|2`
- `--batch-size COUNT`
- `--lean-pool-size COUNT`
- `--lean-memory-limit-gb GIB`
- `--turn-timeout SECONDS`
- `--request-timeout SECONDS`
- `--setup-timeout SECONDS`
- `--gc-timeout SECONDS`

`--turn-timeout 86400` permits one Codex batch to run for up to one day. It is
not the timeout for Lake setup or an entire multi-batch verification pass.

### `manuscript status`

Shows current snapshot, live mutation state, latest run, certificate count,
claim-state counts, and open questions. Add `--json` for machine-readable
output. Status is safe during a running verification.

### `manuscript graph`

Exports the current manuscript graph. Use `--format json|dot`; add `--output`
to write atomically to a chosen file.

### `manuscript questions`

Lists open structured questions. `--json` emits records. Explicitly close one
with `--resolve QUESTION --reason TEXT` or `--dismiss QUESTION --reason TEXT`;
a source edit normally supersedes it automatically.

### `manuscript correspondence`

Lists manuscript-to-Lean mapping proposals. For a task requiring human review,
use `--approve CLAIM`; use `--reject CLAIM --reason TEXT` for an unfaithful
formal statement. Approval still requires a later independent build.

### `manuscript diff`

Prints the latest source-snapshot patch.

### `manuscript invalidate`

Marks one or more `--claim` IDs invalidated. Add `--include-dependents` to apply
the reverse dependency closure. Lean files and old certificate provenance are
retained.

### `manuscript audit`

Prints a canonical JSON comparison of mapped manuscript and elaborated Lean
dependencies.

## Legacy manuscript/proof commands

### `manuscript-run`

Runs one file-specified task in a new/empty `--output`. It remains compatible
with earlier automation but does not resume incrementally.

Required: `--manuscript`, `--task-file`, `--output`, and `--model`. Effort,
timeouts, Lean workers, memory, compiler, and GC controls mirror the persistent
verify command.

### `repoprover-prove`

Runs one RepoProver `PROVE` task against a prepared Lean project. Requires
`--project`, `--chapter`, `--theorem`, and `--model`; accepts `--lean-path` and
`--source-tex`.

## Environment/provider commands

- `compiler-check`: compile and execute a native program for Lean/Lake.
- `doctor`: verify Codex connectivity, authentication, model inventory, and
  fail-closed MCP/skill isolation.
- `models`: list exact model IDs and supported efforts.
- `smoke`: perform a real isolated Codex dynamic-tool round trip.

## Cache commands

- `cache path`: print the validated cache root.
- `cache init`: create the cache and configure `--max-gb`/`--min-free-gb`.
- `cache doctor`: validate location, layout, policy, usage, and compiler.
- `cache status`: show coarse managed usage, headroom, and reservations.
- `cache attach --project`: move a project `.lake` tree into managed storage.
- `cache prepare --project`: share dependencies and build without Codex.
- `cache gc`: run deadline-bounded coarse eviction; accepts `--gc-timeout`.

See [Cache and storage](CACHE_AND_STORAGE.md) before changing limits or cleaning
an active machine.
