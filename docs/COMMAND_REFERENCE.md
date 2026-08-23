# Command reference

Run `repoprover-codex COMMAND --help` for the parser's exact installed options.

## Global options

- `--codex PATH`: override the Codex executable.
- `--cache-home PATH`: override the package cache. It must be a dedicated local
  directory inside the user home and outside Dropbox.

Global options appear before the subcommand.

## Environment checks

### `doctor`

Starts an isolated Codex app-server, validates authentication and protocol
connectivity, verifies that external MCP capabilities and skills are absent,
and confirms that models are available.

### `models`

Prints exact model identifiers and supported reasoning efforts advertised by
the installed Codex CLI. Use one of these pairs for a real run.

### `compiler-check`

Selects a native compiler, compiles a C program, and executes the result. This
is the same mandatory class of check performed by the installer.

### `smoke`

Runs a real Codex dynamic-tool round trip. Requires `--model`; accepts
`--effort`.

## Manuscript verification

### `manuscript-run`

Required options:

- `--manuscript FOLDER`
- `--task-file FILE`
- `--output FOLDER`
- `--model MODEL`

Important optional controls:

- `--effort EFFORT`
- `--turn-timeout SECONDS`
- `--request-timeout SECONDS`
- `--setup-timeout SECONDS`
- `--gc-timeout SECONDS`
- `--lean-pool-size COUNT`
- `--lean-memory-limit-gb GIB`
- `--lean-cc PATH`

See the [usage guide](USAGE.md) for input, output, monitoring, and result
semantics.

### `repoprover-prove`

Runs one RepoProver `PROVE` task against an already prepared Lean project.
Required options are `--project`, `--chapter`, `--theorem`, and `--model`.
`--lean-path` and `--source-tex` identify source files. The timeout, effort,
worker, memory, and compiler controls mirror `manuscript-run`.

## Cache commands

### `cache path`

Print the validated cache root.

### `cache init`

Create the cache layout, run the compiler preflight, and record policy. Use
`--max-gb` for the managed-cache admission limit and `--min-free-gb` for the
filesystem reserve.

### `cache doctor`

Validate local/non-Dropbox storage, required directories, compiler execution,
current usage, limits, and reservations.

### `cache status`

Report managed bytes by category, filesystem headroom, active reservations,
and the accounting-index path.

### `cache attach`

Move an existing project's `.lake` tree into managed storage and leave a
project-local symlink. Requires `--project`.

### `cache prepare`

Attach a project, resolve or reuse its shared dependency depot, and run a full
Lean build without starting Codex. Requires `--project`; accepts setup, GC, and
compiler controls.

### `cache gc`

Run deadline-bounded garbage collection over inactive coarse cache units. Use
`--gc-timeout` to change the default 900-second total budget. Active builds and
depots are skipped.
