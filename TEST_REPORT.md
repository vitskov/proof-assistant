# Test report — version 0.3.0

Test date: 2026-08-22 (America/New_York)

## Release result

The version 0.3.0 implementation at commit
`a754bc904db90d66cdbfb18384758b879fc1f58d` passed the complete Python test
suite, a fresh-wheel test, the supported local installer, Codex app-server
health checks, and a real two-project Lean cache-sharing acceptance test.

The cache design prevents a new manuscript workspace from retaining another
complete Mathlib/REPL build when its dependency fingerprint is compatible.
Every project still has an isolated root build. A bounded LRU policy prevents
inactive managed cache data from exceeding the configured ceiling or consuming
the configured filesystem free-space reserve.

No files in the sibling RepoProver checkout were changed. Nothing was pushed
to `facebookresearch/repoprover`, and no upstream pull request or issue was
created.

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
- Native compiler selected for Lean: `/usr/bin/clang`

The active development installation is:

- environment: `/Users/vui1/.venvs/repoprover-codex`
- interpreter: Python 3.13.15
- package version: 0.3.0
- package source: `/Users/vui1/src/repoprover-codex`
- cache: `/Users/vui1/.cache/repoprover-codex`

All of these locations are outside Dropbox.

## Automated suite

`python -m pytest -q` completed with **78 passed**. The installer ran the same
suite after reinstalling the editable package with uv.

Coverage includes:

- app-server JSONL protocol, model/effort validation, and dynamic tools;
- Codex failure, timeout, and external MCP/skill isolation cases;
- compiler discovery and real compile/execute checks;
- cache path, Dropbox, local-filesystem, and Git-hygiene enforcement;
- config migration, cache limits, LRU eviction, active leases, and capacity
  refusal;
- dependency fingerprinting, immutable depot publication, concurrent warm
  claims, project-build isolation, and broken-link repair; and
- manuscript snapshots, task-file preservation, generated Lean projects,
  output validation, result evidence, independent builds, and CLI parsing.

Focused Ruff `E`, `F`, `I`, and `UP` checks and Ruff formatting checks passed
for every implementation and test file changed in 0.3.0. `git diff --check`
also passed.

## Real Lean cache-sharing acceptance

Two independent small Lean projects were prepared under:

`/Users/vui1/.cache/repoprover-codex/fixtures/cache-sharing-acceptance`

Each project pins Lean 4.28, Mathlib 4.28, and REPL 4.28.0-rc1, and proves the
toy theorem `2 + 2 = 4` with `norm_num`.

Project A exercised the cold path. Project B exercised the warm path. Both
completed `lake build` successfully and selected:

- dependency key: `9409030579b28f587dcc649b`
- shared depot:
  `/Users/vui1/.cache/repoprover-codex/lake/dependencies/deps-9409030579b28f587dcc649b`
- resolved Mathlib commit:
  `8f9d9cff6bd728b17a24e163c9402775d9e6a365`
- resolved REPL commit:
  `08ef67a0ce3606ff54b143bce6e7bb63af491e75`
- locked-manifest SHA-256:
  `fad34588ea56a5f5414d1b70d14c77b31c5fa836325c7a0f6007aafde688a142`

The projects' top-level `.lake` links resolve to different build directories.
Their `.lake/packages` links resolve to the same shared depot. The depot is
about 7.05 GiB; each isolated root build is about 5.9 MiB. Measured managed
cache growth from preparing Project B was 6,225,920 bytes rather than another
multi-gigabyte dependency tree. No writable regular files remained in the
sealed dependency packages tree.

Two `cache prepare` commands were then run simultaneously against Projects A
and B. Both acquired shared warm leases, reported depot reuse, and completed
`lake build`. This specifically verifies that compatible warm jobs are not
serialized behind an unnecessary exclusive depot lock.

After acceptance:

- managed cache: 7.45 GiB
- dependency depots: 7.05 GiB
- isolated project builds: 0.01 GiB
- Mathlib downloads: 0.39 GiB
- configured cache maximum: 16.00 GiB
- configured minimum filesystem free space: 25.00 GiB
- measured filesystem free space: 114.28 GiB
- manual `cache gc`: zero removals because both safety limits were satisfied

## Installer and wheel

`scripts/install-dev.sh` passed using uv and Python 3.13.15. As a mandatory
installation step it compiled and executed a C program, selected
`/usr/bin/clang`, initialized the non-Dropbox cache with the 16/25 GiB policy,
and passed all 78 tests.

`uv build --python /Users/vui1/.venvs/repoprover-codex/bin/python` produced the
0.3.0 sdist and wheel. The wheel was installed with uv into a fresh disposable
Python 3.13.15 environment outside Dropbox. In that environment:

- the imported package reported version 0.3.0 from `site-packages`;
- `compiler-check` compiled and executed successfully;
- `cache doctor` confirmed local APFS storage outside Dropbox and repeated the
  compile/execute check; and
- all 78 tests passed against the installed wheel.

The disposable environment and local package build artifacts were removed
afterward.

## Repository publication audit

The current tracked tree and the complete local Git patch history were scanned
without printing candidate values for private-key headers and common GitHub,
OpenAI, AWS, and Slack token formats; no matches were found. A separate
`detect-secrets` 1.5.0 scan of all repository files reported zero findings.

No credential file, private key, Python environment, Lean cache, Python cache,
package build directory, or temporary test file is tracked or left in the
working tree. `.gitignore` explicitly excludes those categories. The only Git
remote is the user-owned `git@github.com:vitskov/repoprover-codex.git` origin.

## Codex and RepoProver status

With `OPENAI_API_KEY` removed, `doctor` initialized Codex app-server and listed
seven authenticated models. `models` returned the expected exact model/effort
catalog. The child isolation checks found no exposed local MCP tools/resources
or enabled local skills.

The existing end-to-end acceptance evidence from the preceding development
pass remains valid for the unchanged provider/proof path:

- a Codex dynamic-tool smoke turn completed using the existing Codex login;
- a real RepoProver `ContributorAgent` replaced a committed `sorry`, invoked
  RepoProver's `lean_check`, and passed independent final Lean verification;
- a LaTeX-only manuscript plus a Markdown task file produced a verified Lean
  result and passed an independent final `lake build`; and
- exactly two Codex-backed dynamic-tool turns completed concurrently.

The 0.3.0 pass reran app-server initialization and inventory checks, but did not
spend another model turn on those unchanged scenarios. Its new real acceptance
work targeted the dependency sharing, disk-bound, lease, packaging, and local
installation paths.

## Platform boundary

Linux remains a supported target in the portable implementation, but no Linux
machine was available for this acceptance run. Cache leases require POSIX
`flock`, consistent with the documented macOS/Linux support boundary.
