# Installation

## Required software

Proof Assistant supports macOS and Linux local execution. Install:

- Python 3.13;
- [uv](https://docs.astral.sh/uv/);
- Git;
- Lean 4 and Lake compatible with the tested RepoProver/Mathlib checkout;
- a native C compiler; and
- Codex CLI, authenticated with `codex login`.

Authentication stays inside Codex CLI. Do not copy anything from
`~/.codex/auth.json` and do not manufacture an `OPENAI_API_KEY` from Codex
credentials.

## Development/local installation

Clone the user-owned repository outside Dropbox:

```bash
git clone https://github.com/vitskov/proof-assistant.git "$HOME/src/proof-assistant"
cd "$HOME/src/proof-assistant"
scripts/install-dev.sh
```

The supported installer uses Python 3.13 and `uv`, installs the TUI and normal
dependencies, compiles **and executes** a native test program, initializes the
managed cache, and runs the test suite. A compiler-name lookup alone never
counts as a successful installation.

The defaults are:

```text
source             $HOME/src/proof-assistant
Python environment $HOME/.venvs/proof-assistant
new projects       $HOME/proof-assistant/<project-name>
managed cache      $HOME/.cache/repoprover-codex
```

Add the command to the current shell:

```bash
export PATH="$HOME/.venvs/proof-assistant/bin:$PATH"
```

Add that export to the appropriate shell startup file if it should persist.

The cache keeps its historical `repoprover-codex` directory name on purpose.
Changing the default during the product rename would create a second shared
Mathlib/dependency depot and could consume several extra gigabytes.

## Location safety

The installer rejects a Python environment or cache whose resolved path is
inside Dropbox. Proof Assistant also rejects managed verification projects in
Dropbox. These must stay local:

- `$HOME/.venvs/proof-assistant`;
- `$HOME/.cache/repoprover-codex`; and
- `$HOME/proof-assistant/<project-name>`.

The external LaTeX source folder is different: it may live in Dropbox because
Proof Assistant reads it through a stable-snapshot importer and copies it into
the managed project. The TUI shows an explicit warning for such a source.

## Installer configuration

Override defaults only with absolute, safe locations:

```bash
export PROOF_ASSISTANT_VENV="$HOME/.venvs/proof-assistant"
export PROOF_ASSISTANT_CACHE_HOME="$HOME/.cache/repoprover-codex"
export PROOF_ASSISTANT_PYTHON=3.13
scripts/install-dev.sh
```

The 0.1 installer accepts the old `REPOPROVER_CODEX_VENV`,
`REPOPROVER_CODEX_CACHE_HOME`, and `REPOPROVER_CODEX_PYTHON` variables as
lower-priority migration fallbacks. Prefer the new names. In particular, do not
set a new cache directory merely to update the branding.

## Validate the installation

```bash
proof-assistant --version
proof-assistant compiler-check
proof-assistant cache doctor
proof-assistant doctor
proof-assistant models
```

`compiler-check` must report that the probe compiled and ran. `doctor` must be
able to start `codex app-server`, initialize it, and list models.

Launch the interface with either form:

```bash
proof-assistant
proof-assistant tui
```

The former `repoprover-codex` executable is a deprecated alias during the 0.1
line. New documentation, shell configuration, and automation should use
`proof-assistant`.

## RepoProver integration checkout

Keep the integration checkout outside Dropbox, for example at
`$HOME/src/repoprover`, and install the exact tested checkout into the same
environment when required:

```bash
uv pip install \
  --python "$HOME/.venvs/proof-assistant/bin/python" \
  -e "$HOME/src/repoprover"
```

Do not modify, push to, or open a pull request against
`facebookresearch/repoprover` as part of installing this package.

## Upgrade

Fetch only from the user-owned Proof Assistant repository, inspect the update,
then rerun the installer:

```bash
cd "$HOME/src/proof-assistant"
git fetch origin
git status --short
git pull --ff-only
scripts/install-dev.sh
```

Existing verification projects and the established cache remain in place. The
installer updates the editable package in `$HOME/.venvs/proof-assistant`.
