# AI providers and first-time setup

Proof Assistant can use one of six AI drivers. The driver proposes formal work;
the host controls tools and project state, and Lean remains the final proof
authority.

| Driver | Account connection | Model catalog |
|---|---|---|
| OpenAI Codex CLI (`codex_cli`) | native `codex login` session | live, from the authenticated Codex app-server when available |
| Anthropic Claude Code CLI (`claude_cli`) | native `claude auth login` session | curated aliases; the CLI has no documented noninteractive account model-list command |
| GitHub Copilot CLI (`copilot_cli`) | native `copilot login` session | curated `auto` alias; the CLI has no documented noninteractive account model-list command |
| OpenAI API (`openai_api`) | `OPENAI_API_KEY` or OS keyring | live account API, with a labeled curated fallback |
| Anthropic API (`anthropic_api`) | `ANTHROPIC_API_KEY` or OS keyring | live account API, with a labeled curated fallback |
| Google Gemini API (`gemini_api`) | `GEMINI_API_KEY` or OS keyring | live account API, with a labeled curated fallback |

The default primary driver is Codex CLI. This is a default, not a requirement:
first-time setup can select any ready driver.

## First run in the TUI

Launch:

```bash
proof-assistant
```

On a new installation, the backend checks the configured primary driver. If
the machine provider configuration has never been confirmed and that driver is
not ready, the TUI opens **Set up your primary AI driver** before project
creation. The page lets you:

1. choose the primary driver;
2. inspect installation, authentication, model-catalog source, and supported
   difficulty values;
3. review and explicitly approve a supported user-local CLI installation;
4. copy the provider's native login command, or submit an API key once to the
   OS keyring;
5. assign a model and reasoning difficulty to each RepoProver role, or use
   **Use recommended defaults for selected provider** to populate the complete
   role matrix; and
6. recheck the sanitized status before continuing.

The screen cannot read provider credential files and never receives a stored
credential value. Provider status, paths, commands, catalogs, and task-policy
explanations are displayed in selectable text.

Provider settings are available later under **Settings → AI Providers**. Press
**F3** from an ordinary screen to open Settings and **F2** to return to the
main menu. These are global shortcuts. On a modal dialog, the first F2/F3 press
dismisses the dialog; press the key again to navigate. Opening Settings from a
running verification detaches only that TUI observer. It does not cancel the
backend job.

## Command-line setup

`setup` is an alias for `ai` in the following commands.

```bash
# Sanitized readiness for all drivers, or one driver
proof-assistant ai status
proof-assistant ai status --driver claude_cli

# Live or explicitly labeled fallback catalog
proof-assistant ai models openai_api

# Select the machine-wide primary, regenerate role defaults, and optionally
# set the provider-level fallback
proof-assistant ai select anthropic_api \
  --model claude-sonnet-4-6 \
  --difficulty high

# Preview first; --yes approves the exact allowlisted plan
proof-assistant ai install claude_cli
proof-assistant ai install claude_cli --yes

# Hidden prompt by default; --stdin is available for controlled automation
proof-assistant ai credential gemini_api
proof-assistant ai credential gemini_api --delete

# Copilot only: sends no request without --yes
proof-assistant ai verify-account copilot_cli
proof-assistant ai verify-account copilot_cli --yes
```

Exit status `0` means the requested check is ready or completed, `2` means the
operation failed, and `3` means setup, consent, or readiness is still needed.

## CLI drivers

### Codex CLI

Use the CLI's own login flow:

```bash
codex login
# On a remote/headless machine, if appropriate:
codex login --device-auth
```

Proof Assistant uses `codex login status` for a non-billable account check. It
uses the isolated Codex app-server `model/list` operation for live model and
reasoning-effort discovery. If that discovery fails, the UI labels the shown
models as a curated fallback instead of presenting them as account results.

### Claude Code CLI

Use the CLI's own account flow:

```bash
claude auth login
```

Proof Assistant checks `claude auth status --text`. Claude Code does not expose
a documented noninteractive account model-list operation, so the current
catalog contains clearly labeled account aliases (`best`, `fable`, `opus`,
`sonnet`, and `haiku`) rather than claiming a live inventory. Selecting `fable`
explicitly requests Fable and can fail when the account is not entitled.

Fable support requires Claude Code 2.1.170 or newer. Anthropic describes Fable
as the option for the hardest and longest-running work; it can require usage
credits and is not available to every account or organization. Proof Assistant
therefore reserves `fable` for the hardest independent proof attempt and uses
`best` for the primary proof lane. The packaged Claude role defaults are:

- primary proof: `best` / `high`;
- independent duplicate proof: `fable` / `xhigh`;
- clarification, diagnosis, and review: `opus` / `high`;
- proof sketch and maintenance/fix: `sonnet` / `medium`; and
- progress/reporting: `haiku` / `low`.

These defaults are capability-gated. Claude Code older than 2.1.170 does not
advertise `best` or `fable`, and any model whose catalog does not support the
preferred difficulty receives the nearest role-appropriate supported value.
Proof Assistant never automatically chooses `none`.

See Anthropic's [Claude Code model
configuration](https://code.claude.com/docs/en/model-config) and [model
overview](https://platform.claude.com/docs/en/models/overview). Proof
Assistant does not send a paid test request merely to discover Fable access.

### GitHub Copilot CLI

Use the CLI's own account flow:

```bash
copilot login
```

Copilot CLI does not expose a documented, non-billable authentication-status
or model-list command suitable for this setup path. Automatic inspection
therefore reports authentication as **unknown** and does not consume quota.
The **Verify Copilot account** action is separate, cancel-first, and explains
that it sends one tiny, no-tools request. The equivalent CLI operation sends
nothing unless `--yes` is present. A successful result is associated with the
installed Copilot CLI version. After that version changes, readiness returns to
**unknown** until the user explicitly approves a new tiny account probe.

## Installing a missing CLI

Proof Assistant never installs a driver merely because it is selected. It
first returns an exact, copyable installation plan and requires explicit
approval for that unchanged plan.

The current supported installer uses the provider's official npm package in a
user-local prefix under `$HOME/.local`, without `sudo`. It requires working
Node.js and npm; Claude Code and Copilot require Node.js 22 or newer. After an
approved installation, the backend:

- adds `$HOME/.local/bin` to the current process path;
- records that path idempotently in the appropriate zsh, bash, or POSIX shell
  startup files;
- runs the driver's version and executable-identity checks; and
- reports the provider's native login step if authentication is still needed.

If those prerequisites are absent, setup reports the limitation and makes no
installation attempt. Interactive account login is always performed by the
user in the provider CLI, never automated by the TUI.

## API credentials

API drivers support exactly two credential sources:

- the provider-specific environment variable; or
- the operating-system credential store through Python `keyring`.

For an environment-sourced key, export it before launching Proof Assistant:

```bash
export OPENAI_API_KEY='...'
export ANTHROPIC_API_KEY='...'
export GEMINI_API_KEY='...'
```

Set only the variable for the selected provider. For the keyring path, use the
password-masked TUI input or `proof-assistant ai credential DRIVER`. The
submitted value is consumed once, the TUI input is immediately cleared, and
only the non-secret credential-source choice is persisted.

Never put a key in a project, task, command argument, provider JSON file, log,
or manuscript `.env`. Proof Assistant rejects secret-shaped provider settings,
writes the machine provider file with mode `0600`, and filters common private
files when importing a manuscript. Filtering is defense in depth, not a reason
to keep secrets beside manuscript source.

## Models, task classes, and difficulty

Proof Assistant distinguishes these task classes:

```text
clarification  diagnostic  proof  sketch
maintenance    review      duplicate_proof  reporting
```

Automatic policy prefers a stronger available model for clarification,
diagnosis, proof, review, and duplicate proof; a lighter model for reporting;
and a middle-tier model for sketching and maintenance. The normal recommended
difficulty is `high`, with `xhigh` for the independent duplicate proof,
`medium` for sketch/maintenance, and `low` for reporting. If a model lacks the
preferred level, the resolver chooses the nearest role-appropriate advertised
level and never selects `none` automatically. These are Proof Assistant
policies, not provider promises.

The UI names the same roles in RepoProver terms: author clarification; scan /
triage diagnostics; primary prove agent; sketch agent; maintain / fix agent;
math and engineering reviewers; independent prove agent; and progress /
reporting agent. Proof Assistant's current incremental execution actively uses
the primary proof and clarification assignments. It freezes all eight so later
RepoProver role dispatch cannot silently inherit one global model.

Supported difficulty names are:

```text
auto  none  low  medium  high  xhigh  max
```

The exact allowed subset is attached to each model descriptor and is shown in
the TUI/catalog output. A selection unsupported by that driver/model is
rejected before execution. In particular, named levels are not assumed to map
identically between providers.

Selection precedence is deterministic:

```text
explicit settings supplied for one run
  > project provider + per-role model/difficulty override
  > machine task/provider policy

task-specific driver       > machine primary driver
task-specific model        > provider model       > task recommendation
task-specific difficulty   > provider difficulty  > task recommendation
```

The TUI edits the machine's primary driver, provider connections, per-role
model/difficulty matrix, provider fallback, and credential source. The
provider-aware default button regenerates all role assignments from the current
capability catalog. From an existing project's dashboard, the same panel can
save a project-only provider plus complete role matrix or reset that project to
the machine policy. The override is stored in
`.repoprover/verification-settings.json`; it never contains credentials. It is
resolved when a verification is submitted, and the resulting settings are
frozen in the durable job, so changing the panel does not mutate a running job.
If a saved model later disappears, a CLI is downgraded, or authentication
expires, Settings keeps the stored revision visible and allows **Use machine
defaults**; Proof Assistant blocks a new run instead of silently substituting a
different model.
The noninteractive `manuscript verify` command also accepts explicit
`--ai-driver`, `--model`, and `--effort` values for that run.

### Live and curated catalogs

A catalog always reports its source:

- `live_account` means the provider or CLI returned models available through
  the configured account;
- `curated_fallback` means a conservative packaged list or alias set is being
  shown because live discovery is unavailable or failed; and
- `unavailable` means no usable catalog exists.

Never interpret a curated fallback as proof that an account can access a
model. Account authorization is established only by a successful live catalog,
a documented CLI status check, or the explicitly approved Copilot probe.

## Execution and security boundary

CLI login remains entirely inside the native CLI. Proof Assistant never reads
`~/.codex/auth.json`, Claude/Copilot credential stores, browser sessions,
private SSH keys, or other provider authentication files. It does not convert
a CLI subscription login into an API key.

Provider subprocesses receive a small allowlisted environment instead of the
parent process environment. API keys are stripped from CLI setup, discovery,
execution, account checks, and npm installation; only Copilot-specific GitHub
authentication variables may cross the Copilot boundary. This prevents an API
key for one provider from silently changing the identity or billing path of a
different CLI driver.

For proof turns:

- Codex runs through the established isolated app-server adapter with external
  MCP servers, apps, plugins, and skills disabled and checked absent;
- Claude and Copilot receive an ephemeral, mode-`0600` prompt/MCP bundle and
  are restricted to the supplied Proof Assistant MCP tools, with their general
  shell/file mutation surface disabled; and
- direct APIs use provider-native function calling in the Proof Assistant
  process.

Every dynamic tool call returns through the same allowlisted host. Lean checks
and recognized Lake builds also pass through their independent admission
controllers. AI, Lean, and build concurrency remain separate even when the AI
provider changes.

## RepoProver reuse decision

Proof Assistant reuses the tested RepoProver proof prompts, tool schemas, and
tool handlers where they remain useful. It does **not** use RepoProver's legacy
provider configuration, static model table, raw API-key fields, generic
OpenAI-compatible client, retry policy, or provider-owned concurrency as the
source of truth.

Provider selection, credential indirection, catalog provenance, task/model
policy, isolated execution, and admission are owned by
`proof_assistant.ai`. RepoProver tools remain below that boundary and cannot
override it. The upstream `facebookresearch/repoprover` repository is not
modified by this integration.

## Machine scope and storage

Provider policy is currently machine-wide:

```text
$XDG_CONFIG_HOME/proof-assistant/providers.json
# or, normally:
$HOME/.config/proof-assistant/providers.json
```

The file is atomic, revision-checked, mode `0600`, and cannot be placed in a
Dropbox tree. It contains driver IDs, model/difficulty choices, credential
source, and sanitized runtime-verification metadata—but never credential
values. Project-specific provider settings are not enabled in 0.1; the backend
contracts keep machine policy separate so a future project overlay can be
added without moving provider authority into the TUI.

## Troubleshooting checklist

```bash
proof-assistant ai status
proof-assistant ai status --driver DRIVER
proof-assistant ai models DRIVER
```

Then check the displayed state:

- **missing/broken CLI:** review `proof-assistant ai install DRIVER` or install
  the provider CLI independently, then recheck;
- **authentication required:** run the displayed native login command, then
  recheck;
- **Copilot unknown:** this is expected until you explicitly approve its tiny
  account probe;
- **API credential required:** export the selected environment variable or
  store the key in the OS keyring;
- **curated fallback:** the listed models are not live account evidence; fix
  connectivity/authentication or choose an actually accessible model; and
- **unsupported difficulty:** choose one of the exact values printed beside
  that model.

See [Troubleshooting](TROUBLESHOOTING.md) for verification failures and
[Architecture and security](ARCHITECTURE.md) for the broader project boundary.
