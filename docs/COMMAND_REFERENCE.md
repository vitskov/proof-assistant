# Command reference

Run `proof-assistant COMMAND --help` for the installed version's complete
arguments. The deprecated `repoprover-codex` alias invokes the same parser
during the 0.1 line.

## Interactive interface

```bash
proof-assistant
proof-assistant tui
```

Both launch the Textual application. Bare invocation is the normal user entry
point.

## Persistent manuscript projects

The TUI is preferred for creation because it provides stable source import,
task editing, Dropbox warnings, impact review, and recovery screens.

```bash
proof-assistant manuscript init \
  --manuscript /absolute/path/to/source \
  --main-file paper.tex \
  --project "$HOME/proof-assistant/example"
```

`init` creates the project-owned default `VERIFY.yaml`; it does not accept or
require an external task file. `--main-file` is relative to `--manuscript` and
may name a root that recursively includes other LaTeX files. It is always
required by this non-interactive command. The TUI's one-file shortcut obtains
the same explicit backend value from source inspection; it does not create an
implicit-root project.

```bash
proof-assistant manuscript verify --project PROJECT [OPTIONS]
```

Important options:

- `--ai-driver codex_cli|claude_cli|copilot_cli|openai_api|anthropic_api|gemini_api`
  selects the proof-agent driver for this run;
- `--model MODEL` and `--effort EFFORT` select the provider model/difficulty;
  inspect the exact catalog with `proof-assistant ai models DRIVER`;
- `--concurrency auto|adaptive|fixed` selects adaptive automatic admission or
  fixed reproducible limits;
- `--ai-concurrency N`, `--lean-pool N`, and `--max-builds N` set independent
  exact one-run resource limits;
- `--agents-per-target N` sets the duplicate-attempt ceiling;
- `--codex-plan plus|pro-5x|pro-20x|unknown` selects a Proof Assistant policy
  heuristic, not an official OpenAI service limit;
- `--resource-profile auto|interactive|server` controls the hardware reserve
  policy;
- `--jobs N` controls logical proof-batch worker fan-out (legacy default: `2`),
  while machine AI admission remains authoritative;
- `--batch-size N` bounds claims assigned to one turn;
- `--turn-timeout SECONDS` applies to each AI turn (`86400` is one day);
- `--setup-timeout SECONDS` bounds Lean/cache preparation; and
- `--lean-memory-limit-gb GB` bounds configured Lean worker memory where
  supported.

Project inspection:

```bash
proof-assistant manuscript status --project PROJECT [--json]
proof-assistant manuscript graph --project PROJECT [--format json|dot]
proof-assistant manuscript questions --project PROJECT [--json]
proof-assistant manuscript diff --project PROJECT
proof-assistant manuscript audit --project PROJECT
```

Expert state-management commands:

```bash
proof-assistant manuscript invalidate --project PROJECT --claim CLAIM_ID \
  [--claim CLAIM_ID] [--include-dependents]
proof-assistant manuscript correspondence --project PROJECT [OPTIONS]
```

Use these only when you understand their review/state consequences. The TUI
routes ordinary clarification and source-change handling through higher-level
workflow contracts.

## AI provider setup

Provider connections, credentials, and defaults are machine-wide. Existing
projects may override the provider, model, and difficulty for future runs from
the project's **Settings** panel. `setup` is an alias for `ai`.

```bash
proof-assistant ai status [--driver DRIVER]
proof-assistant ai models DRIVER
proof-assistant ai select DRIVER [--model MODEL] [--difficulty LEVEL]
proof-assistant ai install CLI_DRIVER [--yes]
proof-assistant ai credential API_DRIVER [--stdin | --delete]
proof-assistant ai verify-account copilot_cli [--yes]
```

Driver IDs are `codex_cli`, `claude_cli`, `copilot_cli`, `openai_api`,
`anthropic_api`, and `gemini_api`. Difficulty values are `auto`, `none`, `low`,
`medium`, `high`, `xhigh`, and `max`; each model accepts only the subset printed
by its catalog.

- `ai status` probes install/auth/catalog readiness and prints only sanitized
  values.
- `ai models` labels the catalog `live_account`, `curated_fallback`, or
  `unavailable`.
- `ai select` persists the primary driver and optional provider defaults.
- A project's Settings panel can persist or reset its own
  provider/model/difficulty override; credentials stay machine-owned and
  running jobs keep their submitted settings.
- `ai install` prints the exact allowlisted user-local plan and changes
  nothing unless `--yes` approves that plan.
- `ai credential` uses a hidden prompt by default. `--stdin` consumes one line
  for controlled automation; a key is never accepted in an argument. The value
  goes to the OS keyring, not the provider settings file.
- `ai verify-account copilot_cli` sends nothing unless `--yes` explicitly
  approves one tiny no-tools entitlement request.

Native CLI login remains separate: run `codex login`, `claude auth login`, or
`copilot login` as directed, then recheck. API environment variables are
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY`.

See [AI providers and first-time setup](AI_PROVIDERS.md) for catalog,
precedence, authentication, and isolation details.

## Installation and Codex compatibility diagnostics

```bash
proof-assistant compiler-check
proof-assistant doctor
proof-assistant models
proof-assistant smoke --model MODEL --effort EFFORT
```

- `compiler-check` compiles a standard-header and Lean-header probe, then
  executes a native C program. Lean's bundled toolchain is driven through
  `leanc` with `LEAN_CC` unset; an explicit or fallback compiler is validated
  before it is exported to Lake.
- `doctor` checks Codex app-server initialization and model listing.
- top-level `models` prints Codex model IDs and supported reasoning effort;
  use `ai models DRIVER` for provider-neutral setup.
- `smoke` runs one real client-defined dynamic-tool round trip.

The `smoke` and `repoprover-prove` commands accept the same concurrency
overrides as `manuscript verify`.

## Concurrency calibration

```bash
proof-assistant benchmark codex-concurrency
proof-assistant benchmark lean-concurrency [--project PROJECT]
proof-assistant benchmark build-concurrency [--project PROJECT]
```

The default Codex action sends no Codex traffic. Add
`--allow-codex-traffic` only to explicitly authorize the small harmless probe;
the result always reports whether traffic was used. A Lean action with
`--project` measures disposable representative project REPLs under an exclusive
Lean admission lease and persists their RSS summary; without a project it
records the conservative uncalibrated policy. The build action records its
machine recommendation without starting concurrent builds. None modifies proof
state, and recommendations are never silently applied.

Machine settings, the exact precedence rules, environment variables, policy
formulas, and run-provenance fields are documented in [Concurrency and resource
management](CONCURRENCY.md).

## Managed cache

```bash
proof-assistant cache path
proof-assistant cache init [--max-gb N] [--min-free-gb N]
proof-assistant cache status
proof-assistant cache doctor
proof-assistant cache gc [--gc-timeout SECONDS]
proof-assistant cache prepare --project LEAN_PROJECT
proof-assistant cache attach --project LEAN_PROJECT
```

The default path intentionally remains `$HOME/.cache/repoprover-codex` after
the product rename, so the existing shared Mathlib depot is reused.

## Direct RepoProver operation

```bash
proof-assistant repoprover-prove \
  --project LEAN_PROJECT \
  --chapter CHAPTER \
  --theorem THEOREM \
  --lean-path FILE.lean \
  --model MODEL \
  --effort EFFORT
```

This is an advanced integration diagnostic, not the normal manuscript UI.

## Persistent verification exit codes

| Code | Meaning |
|---:|---|
| 0 | selected scope verified |
| 10 | clarification required |
| 11 | partial/inconclusive; not evidence of falsity |
| 12 | kernel-checked counterexample outcome |
| 20 | setup/project/cache failure |
| 21 | AI provider/authentication/protocol failure |
| 22 | Lean build/extraction/merge failure |

The TUI converts these outcomes into typed workflow states and appropriate
findings, clarification, or recovery screens.
