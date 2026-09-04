# Installation

## Install or upgrade

On macOS or Linux, run:

```bash
bash -c 'set -o pipefail; curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/vitskov/proof-assistant/main/install.sh | bash'
```

Open a new terminal and start the application with:

```bash
proof-assistant
```

Rerunning the same command performs a safe upgrade. It refuses to replace a
managed checkout containing local changes.

### Start with fresh machine settings

A normal reinstall preserves settings. To make the next launch run with fresh
machine settings, exit Proof Assistant and archive its machine-setting files
before reinstalling:

```text
Provider roles      ${XDG_CONFIG_HOME:-$HOME/.config}/proof-assistant/providers.json
Concurrency         ${XDG_CONFIG_HOME:-$HOME/.config}/proof-assistant/settings.yaml
Local preferences   ${XDG_CONFIG_HOME:-$HOME/.config}/proof-assistant/preferences.json
Project override    $PROJECT/.repoprover/verification-settings.json
```

```bash
reset_backup="$HOME/proof-assistant-settings-backup-$(date +%Y%m%d-%H%M%S)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/proof-assistant"
mkdir -p "$reset_backup"
for name in providers.json providers.json.lock settings.yaml settings.yaml.lock preferences.json; do
  [[ ! -e "$config_dir/$name" ]] || mv "$config_dir/$name" "$reset_backup/$name"
done
home_preferences="$HOME/.config/proof-assistant/preferences.json"
if [[ "$home_preferences" != "$config_dir/preferences.json" && -e "$home_preferences" ]]; then
  mv "$home_preferences" "$reset_backup/preferences-home.json"
fi
```

This archives the provider settings (`providers.json`), concurrency policy
(`settings.yaml`), local preferences (`preferences.json`), and their lock files
while preserving `projects.json`, the managed-project catalog. If
`XDG_CONFIG_HOME` points into Dropbox or the managed projects directory, local
preferences instead use `~/.config/proof-assistant/preferences.json`; the same
command archives that fallback too. The next launch recreates defaults even if
the same app version is already installed, so running the installer again is
optional for a settings-only reset.

This does not delete managed projects, project-specific AI overrides, native
CLI login sessions, or the Lean/Mathlib cache. Use **Use machine defaults**
inside a project to remove that project's override. The backup can be restored
if the reset was unintended, or deleted after the fresh setup is verified.

### What an upgrade downloads

The standard one-line upgrade fetches the requested Proof Assistant and
RepoProver Git revisions on every run. It reuses a working `uv`, the existing
Python environment, elan, the pinned Lean toolchain, and the Lean/Mathlib cache.
The editable Python packages are refreshed on every run; `uv` may contact
package indexes and reuses cached artifacts where possible. Elan is bootstrapped
when it is absent or unusable, the pinned Lean toolchain is downloaded when it
is absent, and the pinned `uv` bootstrap runs only when no usable `uv` is
already available.

## Requirements

| Resource | Minimum | Recommended |
|---|---:|---:|
| OS | macOS 12+ on Intel or Apple Silicon, or Linux with glibc 2.31+ | Current macOS or Ubuntu LTS |
| CPU | 4 physical cores | 8+ cores |
| Memory | 16 GiB | 32+ GiB |
| Disk | 25 GiB free | More for large Mathlib caches |

Git, `curl`, and a working native C compiler are required. On macOS,
install Apple's Command Line Tools if `cc` is unavailable. On Linux, install
the compiler toolchain supplied by the distribution. At least one supported AI
provider is required before verification: Codex CLI or Claude CLI.

The installer also ensures that a basic terminal editor is available. It checks
for `nano`, `pico`, and `micro`, in that order. If none is present, it attempts
to install them in the same order. Proof Assistant does not launch this editor
from the TUI; clarification source remains inline and read-only.

Python, `uv`, elan, Lean, Lake, Proof Assistant, and the tested RepoProver
checkout are installed or updated by `install.sh`.

## What the installer does

The single installer:

1. checks the operating system, hardware floor, and safe local paths before
   downloading anything;
2. installs the Proof Assistant source under `~/.local/share/proof-assistant`;
3. checks for `nano`, `pico`, then `micro`, and attempts installation in that
   order only when all three are absent;
4. installs the pinned RepoProver checkout under
   `~/.local/share/proof-assistant`;
5. bootstraps elan, the repository's pinned Lean toolchain, Python 3.13, and
   checksum-verified `uv` when necessary;
6. installs the Python environment at `~/.venvs/proof-assistant`;
7. compiles and runs native and Lean-header probes, initializes the shared
   cache, and runs the test suite; and
8. adds only guarded PATH entries for elan and Proof Assistant to the startup
   files selected by the user's shell.

Editor provisioning supports Homebrew or MacPorts on macOS and apt, dnf, yum,
pacman, or zypper on Linux. It never bootstraps a package manager or performs a
package-index update or system upgrade. If installation needs administrative
access that is unavailable, the installer exits with a direct explanation.

## Shell and data safety

The installer appends to shell startup files; it never replaces their existing
content. For Bash, it updates `.bashrc` and the login file Bash already uses. It
does not create a `.bash_profile` that could hide `.profile` or prevent an
existing `.bashrc` loader from running. Repeated installations are idempotent.

An obsolete `.bash_profile` is migrated only when every nonblank line is a
recognized Proof Assistant PATH marker. The original is retained as
`.bash_profile.proof-assistant-backup`. Any unrelated content prevents that
migration.

The Python environment, toolchains, managed source, RepoProver checkout,
managed verification projects, and cache must remain outside Dropbox. A LaTeX
source folder may be in Dropbox because Proof Assistant imports a stable copy
into its local project.

Default locations:

```text
Proof Assistant source  ~/.local/share/proof-assistant/source
RepoProver source       ~/.local/share/proof-assistant/repoprover
Python environment      ~/.venvs/proof-assistant
Managed projects        ~/proof-assistant
Lean/Mathlib cache      ~/.cache/repoprover-codex
Verified uv             ~/.local/share/proof-assistant/uv
Provider settings       ~/.config/proof-assistant/providers.json
```

The historical `repoprover-codex` cache name is intentional: retaining it
avoids duplicating a multi-gigabyte cache during upgrades.

## Existing RepoProver checkout

The default installation uses its own managed, push-disabled integration
checkout. To reuse
an existing clean checkout at the tested commit, run:

```bash
PROOF_ASSISTANT_REPOPROVER_SOURCE=/absolute/path/to/repoprover bash -c 'set -o pipefail; curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/vitskov/proof-assistant/main/install.sh | bash'
```

The installer validates the exact commit and does not modify an explicitly
supplied checkout. It never pushes to the upstream RepoProver repository.

Other optional location overrides are `PROOF_ASSISTANT_SOURCE_DIR`,
`PROOF_ASSISTANT_VENV`, `PROOF_ASSISTANT_CACHE_HOME`,
`PROOF_ASSISTANT_ELAN_HOME`, and `PROOF_ASSISTANT_UV_HOME`. Override paths must
be absolute and resolve outside Dropbox.

## Validate

```bash
proof-assistant --version
proof-assistant compiler-check
proof-assistant cache doctor
proof-assistant ai status
```

`compiler-check` must report successful standard-C, Lean-header, and execution
probes. Provider login remains inside the native CLI; Proof Assistant never
reads a CLI authentication store or copies its login state. Continue with
[Verification AI setup](AI_PROVIDERS.md) if `ai status` reports
that no provider is ready.
