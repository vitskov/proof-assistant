# Installation and upgrades

## Supported local setup

RepoProver Codex currently uses a source-backed local installation. The Python
environment, source checkout, and large Lean cache are separate so the source
repository remains small and the cache can be managed independently.

Requirements:

- macOS or Linux;
- Python 3.13;
- [uv](https://docs.astral.sh/uv/);
- Codex CLI, authenticated with `codex login`;
- Git and Lean/Lake; and
- a working native C compiler.

Neither the Python environment nor any Lean/Lake/Mathlib cache may reside in
Dropbox. The package rejects a managed cache or Lean workspace that resolves
there.

## Default locations

| Purpose | Default |
| --- | --- |
| package source | `$HOME/src/repoprover-codex` |
| RepoProver source | `$HOME/src/repoprover` |
| Python environment | `$HOME/.venvs/repoprover-codex` |
| managed Lean cache | `$HOME/.cache/repoprover-codex` |
| installed command | `$HOME/.venvs/repoprover-codex/bin/repoprover-codex` |

All five paths should resolve outside Dropbox.

## Install RepoProver Codex

```bash
git clone https://github.com/vitskov/repoprover-codex.git "$HOME/src/repoprover-codex"
cd "$HOME/src/repoprover-codex"
scripts/install-dev.sh
export PATH="$HOME/.venvs/repoprover-codex/bin:$PATH"
```

The installer always:

1. verifies that uv is available;
2. creates or reuses an external Python 3.13 environment;
3. installs RepoProver Codex in editable mode with uv;
4. compiles and executes a native test program;
5. initializes the validated local cache; and
6. runs the complete package test suite.

The compiler step is an execution test, not merely a command lookup. On older
supported macOS systems it can select `/usr/bin/clang` when Lean's bundled
compiler is incompatible with the operating system.

To override the defaults, use absolute local paths inside the user home:

```bash
REPOPROVER_CODEX_VENV=/absolute/local/path \
REPOPROVER_CODEX_CACHE_HOME=/absolute/local/cache \
REPOPROVER_CODEX_PYTHON=3.13 \
scripts/install-dev.sh
```

## Install the tested RepoProver dependency

RepoProver is installed from its own source checkout; it is not currently
resolved from the Python package registry by the installer. Install the commit
recorded in [the test report](../TEST_REPORT.md):

```bash
git clone https://github.com/facebookresearch/repoprover.git "$HOME/src/repoprover"
git -C "$HOME/src/repoprover" checkout 386adba3df572cb71df534add2c764e071898a2e
uv pip install --python "$HOME/.venvs/repoprover-codex/bin/python" \
  -e "$HOME/src/repoprover"
```

This is a local dependency installation. Do not modify or publish to upstream
RepoProver as part of ordinary RepoProver Codex use.

## Verify the installation

```bash
repoprover-codex compiler-check
repoprover-codex cache doctor
repoprover-codex doctor
repoprover-codex models
```

Expected results:

- the compiler check reports a selected executable and `compile/run smoke
  check: OK`;
- the cache doctor reports local storage inside the user home and outside
  Dropbox;
- `doctor` initializes an isolated Codex app-server using the existing login;
  and
- `models` prints exact model/effort pairs.

If any check fails, stop before starting a manuscript and consult
[Troubleshooting](TROUBLESHOOTING.md).

## Configure disk limits

Defaults are a 16 GiB managed-cache admission limit and a 25 GiB filesystem
free-space reserve:

```bash
repoprover-codex cache init --max-gb 16 --min-free-gb 25
repoprover-codex cache status
```

See [Cache and storage](CACHE_AND_STORAGE.md) before changing these values.

## Upgrade

Fetch only the user-owned package repository and reinstall:

```bash
cd "$HOME/src/repoprover-codex"
git pull --ff-only
scripts/install-dev.sh
```

The installer reuses the external Python environment and preserves compatible
dependency depots. A changed Lean, Mathlib, REPL, platform, architecture, or
compiler identity selects a different depot; bounded GC can later remove
inactive old data. The accounting index migrates in place, and stale process
reservations are recovered through operating-system leases.

Upgrade the RepoProver checkout only to a commit explicitly tested with this
package or after completing the integration checks in
[Development and testing](DEVELOPMENT.md).

## Removal

Stop all active runs first. The source checkout, Python environment, installed
command, and managed cache are independent paths; remove only the exact
ones you intend to discard. Deleting the managed cache is irreversible but does
not delete manuscript output folders. Deleting an output folder does not delete
the shared dependency depot.
