# Cache and storage guarantees

## Goals

The cache manager prevents two failure modes:

1. duplicating a complete 7+ GiB Mathlib/REPL build for every manuscript; and
2. consuming the filesystem's last free space during dependency setup.

The default root is `$HOME/.cache/repoprover-codex`. It must resolve inside the
user home, on a local filesystem, and outside every detected Dropbox root.

## Layout

```text
repoprover-codex/
├── config.json
├── mathlib-downloads/
├── lake/
│   ├── system/
│   ├── dependencies/
│   │   └── deps-<fingerprint>/
│   │       ├── packages/
│   │       ├── lake-manifest.json
│   │       ├── metadata.json
│   │       └── READY
│   └── builds/
│       └── <project>-<path-hash>/
├── locks/
├── fixtures/
├── worktrees/
└── tmp/
```

## Sharing and isolation

The dependency fingerprint includes:

- `lean-toolchain` content;
- `lakefile.lean` or `lakefile.toml` content;
- operating system and architecture; and
- the selected native compiler path.

The first compatible project resolves and builds its dependencies in an
isolated location. The package then moves the complete `packages` tree into a
staging depot, validates it after relocation, marks it ready atomically, and
seals the dependency files read-only.

Later compatible projects copy the locked manifest and link only their
`.lake/packages` directory to that depot. Each project retains a distinct
`.lake/build`, so manuscript modules and concurrent root builds cannot overwrite
one another.

## Concurrency and crash safety

Project builds, dependency depots, and global download caches use POSIX advisory
file locks. A running job holds shared leases for its complete lifetime.
Construction and deletion require an exclusive lease.

The operating system releases these locks when a process exits or is killed,
so stale PID files cannot permanently block cleanup. Garbage collection skips
any entry whose exclusive lease cannot be obtained immediately.

## Disk policy

Defaults:

- managed cache maximum: 16 GiB;
- minimum filesystem free space: 25 GiB;
- cold-depot setup reservation: 10 GiB;
- warm-project setup reservation: 1 GiB.

Before expensive work, inactive entries are considered in least-recently-used
order. A run starts only if its reservation fits under both the cache ceiling
and free-space reserve. If active entries make that impossible, setup fails
before downloading or compiling.

Configure persistent limits:

```bash
repoprover-codex cache init --max-gb 16 --min-free-gb 25
```

Temporary environment overrides are also supported:

```bash
export REPOPROVER_CODEX_CACHE_MAX_GB=16
export REPOPROVER_CODEX_MIN_FREE_GB=25
```

## Operations

```bash
repoprover-codex cache status
repoprover-codex cache doctor
repoprover-codex cache gc
```

Prepare a small Lean project without starting Codex:

```bash
repoprover-codex cache prepare --project /absolute/path/to/project
```

`cache status` reports allocated managed bytes rather than simply summing
logical file lengths. `fixtures/` and `worktrees/` are not automatically
deleted or counted against the managed-build ceiling because they may contain
user source or test evidence.

## Recovery after eviction

A completed output may retain a `.lake` symlink whose isolated root build was
later evicted. `cache attach` repairs a missing managed target. Dependency and
build caches are reproducible; proof source, reports, logs, and Git commits live
in the output folder and are not garbage-collected.
