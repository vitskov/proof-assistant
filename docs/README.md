# Proof Assistant documentation

This index separates ordinary use from implementation and maintenance details.

Dropbox may be used only as a read-only manuscript source. Proof Assistant
never writes work directories, managed projects, generated or copied state,
caches, Lake artifacts, worktrees, reports, logs, snapshots, temporary files,
exports, configuration, environments, or installation source into Dropbox; a
Dropbox work or output destination is prohibited by design.

The historical incident and its root cause are documented in
[DROPBOX_POSTMORTEM.md](DROPBOX_POSTMORTEM.md).

## Start here

1. [Installation](INSTALLATION.md) — prerequisites, local installation,
   validation, and upgrades.
2. [Usage guide](USAGE.md) — select a main LaTeX file, then use the TUI to
   verify, clarify, edit, and resume a persistent project.
3. [Verification AI setup](AI_PROVIDERS.md) — connect Codex CLI or Claude CLI
   and apply the recommended eight-role preset.
4. [Project task and scope](TASK_FILES.md) — the default task, the built-in task
   editor, targets, modes, and policies.
5. [Troubleshooting and operations](TROUBLESHOOTING.md) — quiet runs, stopped
   runs, output recovery, disk pressure, and common failure modes.

## Reference

- [Command reference](COMMAND_REFERENCE.md) — TUI launch and advanced
  non-interactive operations.
- [Incremental verification model](INCREMENTAL_VERIFICATION.md) — snapshots,
  graphs, invalidation, scheduling, questions, and certificates.
- [Concurrency and resource management](CONCURRENCY.md) — independent AI,
  Lean, and build admission, automatic tuning, machine settings, telemetry, and
  legacy migration boundaries.
- [Cache and storage](CACHE_AND_STORAGE.md) — sharing, reservations, bounded
  garbage collection, and recovery.
- [Architecture and security](ARCHITECTURE.md) — AI/RepoProver boundary,
  authentication, child isolation, and storage control plane.
- [Latest test report](../TEST_REPORT.md) — exact versions, test counts, and
  real Lean acceptance evidence.

## Maintainers and automation

- [Development and testing](DEVELOPMENT.md) — hash-locked Python 3.13 setup,
  Textual live debugging and snapshots, regression tests, integration checks,
  and release checks.
- [Python 3.13 development style](PYTHON_STYLE.md) — strict typing, validated
  data boundaries, measured runtime optimization, and future recommendations.
- [Working as an AI agent](AI_AGENTS.md) — mandatory safety constraints,
  repository map, task workflow, and handoff requirements for coding agents.
- [Maintainer handoff](../CODEX_HANDOFF.md) — compact invariants and validation
  checklist; maintained topic guides remain authoritative.

The former `MANUSCRIPT_RUNS.md` and `INTERNAL_TESTING.md` paths remain as short
redirect pages so older links continue to work.
