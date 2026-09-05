# Verification AI setup

Proof Assistant supports two AI connections:

| Provider | Driver ID | Authentication |
|---|---|---|
| Codex CLI | `codex_cli` | the existing `codex login` session |
| Claude CLI | `claude_cli` | the existing `claude auth login` session |

Proof Assistant does not bundle either subscription. Install and authenticate
the CLI you intend to use before starting formal verification.

## First launch

The normal setup path is provider-first:

1. **Choose provider.** Select Codex CLI or Claude CLI. Proof Assistant
   immediately loads a complete recommended model and reasoning-effort preset
   for all eight verification roles.
2. **Connect provider**, when needed. If the selected CLI is missing or logged
   out, review the installation plan or follow the displayed native login
   command, then choose **Recheck provider**.

If the selected CLI is already ready, the first screen offers **Use Codex** or
**Use Claude**. That one action saves the recommended team and continues to the
project list. Users do not need to open the role editor during setup.

First launch remains gated until one ready provider and its complete role team
have been saved. Quit remains available and does not mark setup complete.

## Recommended role presets

Selecting a provider assigns these defaults:

| Verification role | Codex CLI model | Claude CLI model | Effort |
|---|---|---|---|
| Author clarification | `gpt-5.6-terra` | `sonnet` | `medium` |
| Scan / triage diagnostics | `gpt-5.6-sol` | `opus` | `high` |
| Primary prove | `gpt-5.6-sol` | `best` | `high` |
| Sketch | `gpt-5.6-terra` | `sonnet` | `medium` |
| Maintain / fix | `gpt-5.6-terra` | `sonnet` | `medium` |
| Math and engineering review | `gpt-5.6-sol` | `opus` | `high` |
| Independent prove | `gpt-5.6-sol` | `fable` | `xhigh` |
| Progress / reporting | `gpt-5.6-luna` | `haiku` | `low` |

The defaults reserve the strongest reasoning profiles for proving, diagnosis,
review, and independent duplicate proofs; use balanced models for interactive
or maintenance work; and use the lighter model for progress narration.

Model catalogs are checked against the installed CLI. Claude's `best` alias
uses Fable when the account is entitled and otherwise uses Opus, so ordinary
proofs retain a portable high-capability default. The independent-proof role
selects Fable explicitly at extra-high effort. Proof Assistant never fills a
role from the other provider.

## Change provider or customize roles

Open **Menu** (`Ctrl+P`) and choose **Settings → Verification AI**.

The provider selector and **Reset to recommended preset** action are above the
eight-role roster. Selecting a different provider clears the old provider's
models, loads the new recommended team, and keeps **Save** unavailable until all
eight assignments are valid. Models from the previous provider must never
remain visible or selectable.

Most users should save the preset unchanged. Role-by-role model and effort
controls remain available as advanced customization. Each model selector is
limited to the selected provider's current catalog and supported effort values.

Settings has two scopes:

- **Machine defaults** apply to projects that inherit machine policy.
- **This project** can store a complete project-specific provider and role
  team. **Use machine defaults** removes that override.

A saved provider or role change applies to the **next verification run**. A run
already in progress keeps the provider, models, and efforts frozen when that job
was submitted. Switching settings neither mutates nor restarts the active run.

## Command-line setup

These commands match the interactive setup surface:

```bash
proof-assistant ai status
proof-assistant ai status --driver codex_cli
proof-assistant ai status --driver claude_cli
proof-assistant ai models codex_cli
proof-assistant ai models claude_cli
proof-assistant ai select codex_cli
proof-assistant ai select claude_cli
proof-assistant ai install codex_cli
proof-assistant ai install claude_cli
```

`ai select` saves the selected provider and regenerates its complete
recommended eight-role team. Use the Settings panel only when you need to tune
individual roles.

`ai install` prints the exact user-local installation plan and makes no change
without `--yes`. Installation does not authenticate the CLI. Complete the
provider's native login separately:

```bash
codex login
claude auth login
```

Then rerun `proof-assistant ai status` or choose **Recheck provider**.

## Execution and security boundary

Authentication remains inside the native CLI. Proof Assistant checks sanitized
CLI status and model information; it does not read provider authentication
files, print tokens, or copy subscription login state into its settings.

Provider subprocesses receive a small allowlisted environment. Codex runs
through its isolated app-server adapter. Claude receives an ephemeral,
owner-readable prompt/tool bundle. Both routes expose only the Proof Assistant
tool boundary required for verification.

Machine policy is stored at:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/proof-assistant/providers.json
```

A project override is stored at:

```text
PROJECT/.repoprover/verification-settings.json
```

Neither file contains login secrets. Provider and role choices are validated,
locked, and atomically replaced before they can be used by a future job.
Both files must remain outside Dropbox: Dropbox is accepted only as a read-only
external manuscript source, never as a destination for configuration or any
other Proof Assistant-created file.

## Troubleshooting

- **CLI missing:** review and approve the displayed install plan, or install the
  native CLI independently and recheck.
- **Login required:** run `codex login` or `claude auth login` in a terminal,
  then recheck. Proof Assistant cannot complete an interactive provider login.
- **Catalog unavailable:** verify the CLI works independently, then run
  `proof-assistant ai status --driver DRIVER` and
  `proof-assistant ai models DRIVER`.
- **Save disabled after switching:** wait for the recommended roster to finish
  loading. If a role remains invalid, choose **Reset to recommended preset**.
- **Current run still uses the old provider:** this is intentional. The saved
  change applies to the next submitted run.
