# Test report — version 0.4.0

Test date: 2026-08-22 (America/New_York)

## Result

The version 0.4.0 release changes, based on version 0.3 commit
`dc5c9c3f302a0a890c00d2a7e03997329dcaeeae`, passed the complete Python suite,
the supported installer, a fresh-wheel test, a cold and warm real Lean
cache-sharing acceptance, and steady-state cache checks.

This pass replaces the cache-GC implementation that made the aborted
`laplacians` run appear stuck. No upstream RepoProver files were changed,
nothing was pushed to `facebookresearch/repoprover`, and no upstream pull
request or issue was created.

## Root cause and regression target

The old collector represented each Mathlib `.ltar` archive as a separate
candidate. After every deletion it recursively measured the complete managed
cache again. The observed cache contained 7,634 download entries and 7.43 GiB
of managed data; one measurement took 4.68 seconds. With 7,603 candidates still
to process, the repeated scans projected to about 9.88 hours.

Version 0.4.0 uses one candidate for the complete download namespace, one row
per project build or dependency depot, and in-memory accounting during the
eviction loop. Both recursive measurement and deletion check a common deadline.
The 10,000-file regression test asserts one candidate and exactly one recursive
measurement, so the former multiplicative behavior cannot pass the suite.

## Tested environment

- macOS 12.7.6 (21H1320), x86_64
- Python 3.13.15
- uv 0.9.26
- Codex CLI 0.149.0
- Git 2.37.1
- Lean 4.28.0, commit
  `7e01a1bf5c70fc6167d49c345d3bf80596e9a79b`
- Lake 5.0.0-src+7e01a1b
- RepoProver commit `386adba3df572cb71df534add2c764e071898a2e`
- RepoProver worktree status: clean
- native compiler selected for Lean: `/usr/bin/clang`

The active development installation is:

- source: `/Users/vui1/src/repoprover-codex`
- environment: `/Users/vui1/.venvs/repoprover-codex`
- interpreter: Python 3.13.15
- installed package version: 0.4.0, editable
- cache: `/Users/vui1/.cache/repoprover-codex`

All locations are outside Dropbox.

## Automated suite

`python -m pytest -q` completed with **88 passed**. The supported installer ran
the same suite after reinstalling version 0.4.0 with uv and after compiling and
executing a native test program.

The cache-focused coverage now includes:

- coarse planning for 10,000 Mathlib download files;
- proof that the eviction loop never recursively remeasures after a removal;
- normalized dependency keys that ignore project-only Lake declarations but
  change when a dependency revision changes;
- transactional concurrent capacity reservations;
- stale reservation recovery through process-held OS leases;
- active-entry exclusion and strict fail-closed admission;
- dirty-entry crash recovery and version-1 index migration;
- interrupted atomic-quarantine recovery; and
- hard deadlines for accounting and deletion.

The existing suite continues to cover cache-location/Dropbox enforcement,
compiler checks, dependency publication, concurrent warm claims, isolated root
builds, manuscript snapshots and outcomes, RepoProver tools, app-server
protocol, model validation, and child isolation from local MCP servers and
skills.

Focused Ruff `E`, `F`, `I`, and `UP` checks and Ruff formatting checks passed
for every Python file changed in version 0.4.0. `git diff --check` passed.

## Real Lean acceptance

The previous managed build, dependency, download, Lake-system, temporary, and
trash data were first removed directly, without invoking package GC. Config,
locks, fixtures, worktrees, sources, and manuscript outputs were preserved.
The cleanup reclaimed about 7.4 GiB and left roughly 122 GiB free.

Two independent projects under
`/Users/vui1/.cache/repoprover-codex/fixtures/cache-sharing-acceptance` pin Lean
4.28, Mathlib 4.28, and REPL 4.28.0-rc1 and prove `2 + 2 = 4` with `norm_num`.

Project A exercised the fully cold path after cleanup. It downloaded and
published the shared dependency depot, selected `/usr/bin/clang`, and completed
`lake build`. Project B exercised the warm path, printed `dependency depot
reused: yes`, and completed `lake build` against the same depot.

Both projects and the generated `laplacians` manuscript workspace now compute:

- dependency key: `e50bd489673215a5b89a0dc2`
- shared depot:
  `/Users/vui1/.cache/repoprover-codex/lake/dependencies/deps-e50bd489673215a5b89a0dc2`
- resolved Mathlib commit:
  `8f9d9cff6bd728b17a24e163c9402775d9e6a365`
- resolved REPL commit:
  `08ef67a0ce3606ff54b143bce6e7bb63af491e75`
- locked-manifest SHA-256:
  `fad34588ea56a5f5414d1b70d14c77b31c5fa836325c7a0f6007aafde688a142`

The two top-level `.lake` links resolve to different project-build directories;
their `.lake/packages` links resolve to the same read-only depot. Current
managed storage is:

- total: 7.47 GiB
- dependency depot: 7.06 GiB
- two isolated builds: 0.01 GiB total
- Mathlib downloads: 0.39 GiB
- active reservations: 0 GiB
- filesystem free: 114.89 GiB

An unchanged-cache `cache gc --gc-timeout 900` completed in 0.21 seconds,
reported zero recursive measurements, removed zero entries, and left `tmp/`
and `trash/` empty.

## Installer and wheel

`scripts/install-dev.sh` used uv and Python 3.13.15, upgraded the external
editable installation from 0.3.0 to 0.4.0, compiled and executed a C program,
selected `/usr/bin/clang`, initialized the 16/25 GiB policy, and passed all 88
tests. The subsequent cache status check migrated the accounting index to
schema 2.

`uv build --python /Users/vui1/.venvs/repoprover-codex/bin/python` produced the
0.4.0 sdist and wheel. The wheel was installed with uv into a fresh disposable
Python 3.13.15 environment outside Dropbox. In that environment:

- the imported package reported 0.4.0 from `site-packages`;
- `compiler-check` compiled and executed successfully;
- `cache doctor` repeated the compiler check and validated local APFS storage;
  and
- all 88 tests passed against the installed wheel.

The disposable environment, sdist, wheel, package build tree, and generated
egg-info were removed afterward.

## Repository hygiene

No credential-named file, private key, Python environment, Lean cache, package
build directory, or temporary test file is tracked. `.gitignore` excludes these
categories. A `detect-secrets` 1.5.0 all-files scan after removing ignored test
and Ruff caches reported zero findings. The audit did not print candidate
secret values.

## Provider and upstream status

The provider/proof path is unchanged from the preceding release acceptance,
which completed real Codex dynamic-tool, RepoProver proof, file-based
manuscript, independent Lean-build, and two-agent concurrency checks. This pass
did not spend another model turn; it targeted the reproduced disk/GC failure,
dependency reuse, packaging, and local installation.

The sole configured Git remote is the user-owned
`git@github.com:vitskov/repoprover-codex.git`. Publication is confined to that
repository's `main` branch. Upstream RepoProver integration remains untouched.

## Platform boundary

Linux remains a supported target in the portable implementation, but no Linux
machine was available for this acceptance run. Cache leases require POSIX
`flock`, consistent with the documented macOS/Linux support boundary.
