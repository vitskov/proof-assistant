# Cache and storage

## Guarantees and boundary

The cache manager is designed around three invariants:

1. compatible manuscripts share one complete Mathlib/REPL dependency depot;
2. concurrent jobs cannot independently admit overlapping disk reservations;
3. GC work is proportional to cache contents, never the product of entry count
   and total cache size.

The default root deliberately remains `$HOME/.cache/repoprover-codex` after the
Proof Assistant rename. Reusing it avoids a second multi-gigabyte Mathlib
depot. It must resolve inside the
user home, on a local filesystem, and outside every detected Dropbox root.

Dropbox is read-only input storage: an external manuscript source may be read
there, but Proof Assistant never writes work directories, managed projects,
generated or copied state, caches, Lake artifacts, worktrees, reports, logs,
snapshots, temporary files, exports, configuration, environments, or
installation source into Dropbox. A requested Dropbox work or output
destination is prohibited by design.

The limits are conservative admission controls, not an operating-system quota.
External processes and unexpectedly oversized build outputs remain outside the
package's control.

## Layout

```text
repoprover-codex/
├── config.json
├── cache-index.sqlite3
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
├── tmp/
└── trash/
```

`fixtures/` may contain maintained acceptance projects. `worktrees/` contains
ephemeral incremental proof batches. A normally completed batch removes its
worktree after preserving run artifacts and merging or rejecting its commits;
a hard-killed process can leave a recoverable worktree for inspection. Neither
directory is treated as an ordinary GC eviction unit.

## Dependency sharing and build isolation

For the standard generated `lakefile.lean`, the dependency fingerprint includes:

- `lean-toolchain` content;
- normalized `require NAME from git "URL" @ "REV"` declarations;
- operating system and architecture; and
- any validated external `LEAN_CC` override. The default bundled compiler is
  identified by `lean-toolchain` and runs with `LEAN_CC` unset.

Project package names, `lean_lib` roots, and absolute workspace paths are not
dependency inputs. A generated manuscript workspace and a small acceptance
project therefore reuse the same depot when they pin the same Mathlib and REPL
revisions. If the parser does not recognize the dependency form, it fails
conservatively by hashing the complete lakefile; this can reduce reuse but
cannot alias two different unknown configurations.

The first compatible project resolves and builds dependencies in an isolated
location. The package moves the complete `packages` tree into a staging depot,
validates it after relocation, marks it ready atomically, and seals its files
read-only.

Later projects copy the locked manifest and link only `.lake/packages` to that
depot. Each project retains a distinct `.lake/build`, so manuscript modules and
concurrent root builds cannot overwrite one another.

Persistent verification projects live at the caller's `--project` path and are
not cache entries. Their Lean source, Git history, source-snapshot repository,
SQLite state, reports, and certificates are never removed by cache GC. Only
their reproducible `.lake` root build is managed through the cache symlink.
Both the persistent project and every managed cache target must be outside
Dropbox.

## Admission and reservations

Every managed operation performs these steps before Lake can download or build:

1. acquire the exclusive `admission` lease;
2. recover reservation rows whose process-held OS lease is gone;
3. reconcile the coarse cache index;
4. enforce the managed-size and filesystem-free limits while including all
   live reservations;
5. write its own reservation transactionally; and
6. release `admission`, while retaining the reservation lease for the job.

This serializes only admission accounting, not the later Lean or Codex work.
The cold reservation is 10 GiB and the warm-project reservation is 1 GiB. A
second concurrent job is rejected before growth if its reservation cannot fit.

## Cache index and eviction units

The SQLite index uses WAL journaling and full synchronous commits. It has one
row for each eviction unit:

| Storage | Eviction unit |
| --- | --- |
| `lake/builds/` | one project build directory |
| `lake/dependencies/` | one dependency depot or staging directory |
| `mathlib-downloads/` | the entire download namespace |
| `lake/system/` | the entire Lake system cache |
| `tmp/` and `trash/` | one direct child |

In particular, thousands of `.ltar` files never become thousands of GC
candidates. The regression suite creates 10,000 download files and asserts
that GC plans one candidate and performs exactly one recursive measurement.

Index rows have `ready`, `dirty`, or `deleting` state. A job marks every tree it
may mutate as `dirty` before yielding control to Lake and refreshes it before
releasing its reservation. If the job is killed, its OS leases disappear; the
next reconciliation measures the dirty tree once. A dirty active tree without
a live package reservation makes strict admission fail closed.

## Bounded GC algorithm

GC holds the admission lease and uses one deadline for reconciliation and
deletion. The default is 900 seconds.

1. Reconcile direct eviction units. Stable signatures use their indexed byte
   count; a changed or dirty inactive unit is recursively measured once.
2. Plan coarse candidates. Within each recovery/cost tier, older entries are
   selected first: interrupted trash, temporary data, project builds,
   dependency depots, the bulk download cache, then the Lake system cache.
3. Acquire the candidate's exclusive lease. An active candidate is skipped.
4. Atomically rename the whole candidate to `trash/gc-...`. Bulk roots are
   recreated empty immediately.
5. Delete the quarantine in one depth-first traversal, checking the deadline
   on every node and emitting progress at least every 30 seconds.
6. Update remaining totals in memory. There is no recursive filesystem scan
   inside the eviction loop.

If the deadline expires, GC raises an explicit error. The partially removed
tree remains quarantined and is the first candidate on the next pass. It can
never return to a live project path.

Run GC manually with a custom deadline:

```bash
proof-assistant cache gc --gc-timeout 900
```

The command reports its number of recursive measurements. On an unchanged
indexed cache this should be zero.

## Concurrency and crash recovery

Project builds, dependency depots, and global download caches use POSIX advisory
file locks. A running job holds shared leases for its complete lifetime.
Construction and deletion require an exclusive lease. A `repoprover-prove`
operation also holds a shared lease on its dependency depot for the complete
proof run, so a concurrent GC cannot remove dependencies in use.

The operating system releases these locks when a process exits or is killed.
Reservation recovery tests the corresponding lease instead of trusting a PID
or timestamp. Garbage collection skips any entry whose exclusive lease cannot
be obtained immediately.

## Disk policy

Defaults:

- managed cache maximum: 16 GiB;
- minimum filesystem free space: 25 GiB;
- cold-depot setup reservation: 10 GiB;
- warm-project setup reservation: 1 GiB; and
- GC deadline: 900 seconds.

A run starts only if its reservation fits under both the cache ceiling and
free-space reserve. If active entries make that impossible, setup fails before
downloading or compiling.

Configure persistent limits:

```bash
proof-assistant cache init --max-gb 16 --min-free-gb 25
```

Temporary environment overrides are also supported:

```bash
export PROOF_ASSISTANT_CACHE_MAX_GB=16
export PROOF_ASSISTANT_MIN_FREE_GB=25
```

## Operations and recovery

```bash
proof-assistant cache status
proof-assistant cache doctor
proof-assistant cache gc --gc-timeout 900
proof-assistant cache prepare --project /absolute/path/to/project
```

Cache configuration schema 3 records the validated compiler separately from
the optional `LEAN_CC` override. Older configurations that stored Lean's own
`bin/clang` as an override migrate to the safe bundled-toolchain default; real
external overrides are preserved.

`cache status` reports allocated managed bytes from the reconciled coarse index,
the index path, and active reservations. It does not enumerate every Mathlib
archive as a separate entry.

A completed output may retain a `.lake` symlink whose isolated root build was
later evicted. `cache attach` repairs a missing managed target. Dependency and
build caches are reproducible; proof source, reports, logs, and Git commits live
in the output folder and are not garbage-collected.
