# Installation

## System requirements

The installer refuses to run on hardware or an OS below these floors, before
downloading or building anything:

| Resource | Minimum        | Recommended     |
| -------- | -------------- | ---------------- |
| OS       | macOS 12 Monterey (Darwin 21) or newer, on Intel or Apple Silicon; or Linux with glibc 2.31+ (Ubuntu 20.04-equivalent) | Latest macOS or Ubuntu LTS |
| CPU      | 4 physical cores | 8+ physical cores |
| Memory   | 16 GiB RAM       | 32+ GiB RAM       |
| Disk     | 25 GiB free (see [cache disk policy](CACHE_AND_STORAGE.md#disk-policy)) | more, for larger caches |

Lean/Mathlib builds are memory- and CPU-heavy; hardware at the minimum floor
works but compiles more slowly and supports less build concurrency (see
[Concurrency](CONCURRENCY.md)). Intel Macs remain fully supported: Lean 4 and
`uv` both still ship Intel builds, but Apple only security-patches macOS
13/14/15 on Intel hardware going forward (macOS 26 "Tahoe" dropped Intel
support), so Intel users should expect to be capped at macOS 15 Sequoia.

Override the CPU/memory floor only for a site policy that has verified its
own hardware:

```bash
export PROOF_ASSISTANT_MIN_CPU_CORES=4    # default: 4
export PROOF_ASSISTANT_MIN_MEMORY_GIB=16  # default: 16
```

The OS/kernel floor (Darwin 21 / glibc 2.31) is not configurable.

## Required software

Proof Assistant supports macOS and Linux local execution. Install:

- Git;
- Lean 4 and Lake compatible with the tested RepoProver/Mathlib checkout;
- a native C compiler; and
- access to at least one supported AI driver: Codex, Claude Code, or GitHub
  Copilot CLI, or an OpenAI, Anthropic, or Gemini API key.

The development installer uses an existing working
[uv](https://docs.astral.sh/uv/) when available. If uv is missing or broken, it
bootstraps uv with Astral's official standalone installer. That bootstrap needs
either `curl` (preferred) or `wget`; no downloader is needed when uv already
works.

Python 3.13 is the default runtime. uv uses an existing compatible interpreter
or provisions one when needed, so neither a preinstalled Python nor a
system-package-manager Python is required by the development installer.

CLI authentication stays inside the provider CLI. Do not copy anything from a
CLI auth store or manufacture an API key from a subscription login. API keys
are read only from their documented environment variable or the OS keyring;
see [AI providers and first-time setup](AI_PROVIDERS.md).

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

If no working uv is available, the installer:

1. downloads `https://astral.sh/uv/install.sh` with `curl`, falling back to
   `wget` only when `curl` is unavailable;
2. installs into `$HOME/.local/bin` by default;
3. sets `UV_NO_MODIFY_PATH=1`, so Astral's installer cannot edit shell startup
   files;
4. prepends that directory only to the running installer process;
5. verifies the installed executable with `uv --version`; and
6. captures that exact executable path and uses it for every remaining uv
   command.

The script never uses `sudo` or invokes Homebrew, apt, another system package
manager, or a Rust build. If neither `curl` nor `wget` is present, it stops with
a clear error and leaves system configuration untouched.

The defaults are:

```text
source             $HOME/src/proof-assistant
Python environment $HOME/.venvs/proof-assistant
new projects       $HOME/proof-assistant/<project-name>
managed cache      $HOME/.cache/repoprover-codex
provider settings  $HOME/.config/proof-assistant/providers.json
```

Add the command to the current shell:

```bash
export PATH="$HOME/.venvs/proof-assistant/bin:$PATH"
```

The installer also adds this path automatically to the startup files for the
shell named by `$SHELL`: `.zprofile`/`.zshrc` for zsh, `.bash_profile`/`.bashrc`
for bash, `~/.config/fish/config.fish` for fish, or `~/.profile` for other
POSIX shells. Existing entries are not duplicated. Open a new terminal, or
source the relevant file, for the command to become available in the current
shell.

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
export PROOF_ASSISTANT_UV_INSTALL_DIR="$HOME/.local/bin"
scripts/install-dev.sh
```

`PROOF_ASSISTANT_UV_INSTALL_DIR` controls only where a missing or broken uv is
bootstrapped. It does not relocate the Python environment or the large managed
Lean cache. A previously bootstrapped executable in that directory is reused
even if the directory is not on the invoking shell's `PATH`.

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
proof-assistant ai status
```

`compiler-check` must report that the probe compiled and ran. `doctor` and the
top-level `models` command are Codex-specific compatibility diagnostics.
`ai status` is the provider-neutral readiness check and prints no credential
values.

Launch the interface with either form:

```bash
proof-assistant
proof-assistant tui
```

The former `repoprover-codex` executable is a deprecated alias during the 0.1
line. New documentation, shell configuration, and automation should use
`proof-assistant`.

### First-time AI setup

On first launch, the TUI opens provider setup when the default primary driver
is not ready. You may instead configure it from the shell:

```bash
proof-assistant ai status
proof-assistant ai models codex_cli
proof-assistant ai select codex_cli --difficulty high
```

For a missing CLI, `proof-assistant ai install DRIVER` only previews an exact
user-local npm plan. Re-run with `--yes` to approve that unchanged plan. This
path requires Node.js/npm; Claude Code and Copilot require Node.js 22 or newer.
The install uses `$HOME/.local` without `sudo`, verifies the resulting
executable, and adds `$HOME/.local/bin` to the current and future shell PATH.
It does not perform the provider's interactive account login.

For an API driver, either export `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`GEMINI_API_KEY`, or submit it to the OS keyring with the password-masked TUI
field or `proof-assistant ai credential DRIVER`. No API key is accepted in a
command argument or provider settings file.

Codex and Claude expose non-billable CLI status checks. Copilot does not expose
a documented equivalent: its optional account check sends one tiny request
only after an explicit cancel-first TUI confirmation or
`proof-assistant ai verify-account copilot_cli --yes`.

On first project creation, select the external source folder and then its main
LaTeX file. A sole `.tex`/`.ltx` file is announced and adopted automatically;
several candidates require an explicit selection. The managed project persists
that root and resolves its recursive `\input`/`\include` closure.

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
