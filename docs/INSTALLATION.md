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
provider is required before verification: Codex, Claude Code, or Copilot CLI,
or an OpenAI, Anthropic, or Gemini API credential.

Python, `uv`, elan, Lean, Lake, Proof Assistant, and the tested RepoProver
checkout are installed or updated by `install.sh`.

## What the installer does

The single installer:

1. checks the operating system, hardware floor, and safe local paths before
   downloading anything;
2. installs the Proof Assistant source and pinned RepoProver checkout under
   `~/.local/share/proof-assistant`;
3. bootstraps elan, the repository's pinned Lean toolchain, Python 3.13, and
   checksum-verified `uv` when necessary;
4. installs the Python environment at `~/.venvs/proof-assistant`;
5. compiles and runs native and Lean-header probes, initializes the shared
   cache, and runs the test suite; and
6. adds only guarded PATH entries for elan and Proof Assistant to the startup
   files selected by the user's shell.

It never uses `sudo`, Homebrew, apt, or another system package manager.

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
reads a CLI authentication store or copies its credentials. Continue with
[AI providers and first-time setup](AI_PROVIDERS.md) if `ai status` reports
that no provider is ready.
