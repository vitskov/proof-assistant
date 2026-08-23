# Documentation

This index separates ordinary use from implementation and maintenance details.

## Start here

1. [Installation](INSTALLATION.md) — prerequisites, local installation,
   validation, and upgrades.
2. [Usage guide](USAGE.md) — prepare a task, run a manuscript, monitor it, and
   interpret the evidence.
3. [Troubleshooting and operations](TROUBLESHOOTING.md) — quiet runs, stopped
   runs, output recovery, disk pressure, and common failure modes.

## Reference

- [Command reference](COMMAND_REFERENCE.md) — command purposes and important
  options.
- [Cache and storage](CACHE_AND_STORAGE.md) — sharing, reservations, bounded
  garbage collection, and recovery.
- [Architecture and security](ARCHITECTURE.md) — Codex/RepoProver boundary,
  authentication, child isolation, and storage control plane.
- [Latest test report](../TEST_REPORT.md) — exact versions, test counts, and
  real Lean acceptance evidence.

## Maintainers and automation

- [Development and testing](DEVELOPMENT.md) — local development, regression
  tests, integration checks, and release checks.
- [Working as an AI agent](AI_AGENTS.md) — mandatory safety constraints,
  repository map, task workflow, and handoff requirements for coding agents.
- [Historical handoff](../CODEX_HANDOFF.md) — retained development history;
  not the current operating guide.

The former `MANUSCRIPT_RUNS.md` and `INTERNAL_TESTING.md` paths remain as short
redirect pages so older links continue to work.
