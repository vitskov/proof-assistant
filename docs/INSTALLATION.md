# Installation and upgrades

## Requirements

- macOS or Linux
- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Codex CLI, already authenticated with `codex login`
- Git, Lean/Lake, and a working native C compiler

Neither the Python environment nor the Lean cache may reside in Dropbox.

## Development installation

```bash
git clone https://github.com/vitskov/repoprover-codex.git "$HOME/src/repoprover-codex"
cd "$HOME/src/repoprover-codex"
scripts/install-dev.sh
```

The default locations are:

- source: `$HOME/src/repoprover-codex`
- Python environment: `$HOME/.venvs/repoprover-codex`
- cache: `$HOME/.cache/repoprover-codex`
- command link: `$HOME/.local/bin/repoprover-codex`

The installer always performs these steps:

1. Verify that uv is available.
2. Create or reuse a Python 3.13 environment outside Dropbox.
3. Install the package in editable mode with uv.
4. Compile and execute a native test program.
5. Initialize the validated local cache.
6. Run the complete package test suite.

Override locations only with paths inside the user home and outside Dropbox:

```bash
REPOPROVER_CODEX_VENV=/absolute/local/path \
REPOPROVER_CODEX_CACHE_HOME=/absolute/local/cache \
REPOPROVER_CODEX_PYTHON=3.13 \
scripts/install-dev.sh
```

## Verify the installation

```bash
repoprover-codex compiler-check
repoprover-codex cache doctor
repoprover-codex doctor
repoprover-codex models
```

`compiler-check` does not merely locate a compiler: it compiles and executes a
C program. On older macOS releases it can select `/usr/bin/clang` when Lean's
bundled compiler is incompatible with the operating system.

## Upgrade

```bash
cd "$HOME/src/repoprover-codex"
git pull --ff-only
scripts/install-dev.sh
```

The installer preserves compatible dependency depots. A changed Lean,
Mathlib, REPL, platform, architecture, or compiler identity naturally selects
a different depot; bounded garbage collection can later remove the old one.
The cache index is migrated in place and stale process reservations are
recovered from operating-system leases.
