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

- `--model MODEL` and `--effort EFFORT` select an exact pair advertised by
  `models`;
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
- `--turn-timeout SECONDS` applies to each Codex turn (`86400` is one day);
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

## Installation and provider diagnostics

```bash
proof-assistant compiler-check
proof-assistant doctor
proof-assistant models
proof-assistant smoke --model MODEL --effort EFFORT
```

- `compiler-check` compiles and executes a native C program.
- `doctor` checks Codex app-server initialization and model listing.
- `models` prints exact model IDs and supported reasoning effort.
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
| 21 | Codex provider/authentication/protocol failure |
| 22 | Lean build/extraction/merge failure |

The TUI converts these outcomes into typed workflow states and appropriate
findings, clarification, or recovery screens.
