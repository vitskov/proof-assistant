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
  --project "$HOME/proof-assistant/example"
```

`init` creates the project-owned default `VERIFY.yaml`; it does not accept or
require an external task file.

```bash
proof-assistant manuscript verify --project PROJECT [OPTIONS]
```

Important options:

- `--model MODEL` and `--effort EFFORT` select an exact pair advertised by
  `models`;
- `--jobs 1|2` limits independent proof workers;
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
