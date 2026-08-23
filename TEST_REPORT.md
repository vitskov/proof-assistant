# Test report — version 0.4.1

Test date: 2026-08-23 (America/New_York)

## Result

Version 0.4.1 implements the persistent manuscript feedback loop: immutable Git
source snapshots, a structural LaTeX object index, manuscript and elaborated
Lean dependency graphs, statement-level certificates, graph-sliced
invalidation, formal-type reconciliation, clarification pause/resume,
dependency audits, and bounded parallel proof worktrees.

The complete automated suite and a real Codex/RepoProver/Lean end-to-end project
passed. An unchanged second pass reused both certificates without starting a
Codex app-server. The legacy one-shot command and the previously accepted
bounded cache implementation remain covered.

No upstream RepoProver file was changed. Nothing was pushed to
`facebookresearch/repoprover`, and no upstream pull request or issue was
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
- Mathlib commit `8f9d9cff6bd728b17a24e163c9402775d9e6a365`
- RepoProver commit `386adba3df572cb71df534add2c764e071898a2e`
- RepoProver worktree: clean
- native compiler selected for Lean: `/usr/bin/clang`

Active development locations:

- source: `/Users/vui1/src/repoprover-codex`
- environment: `/Users/vui1/.venvs/repoprover-codex`
- cache: `/Users/vui1/.cache/repoprover-codex`
- installed command: `/Users/vui1/.local/bin/repoprover-codex`

All locations are outside Dropbox.

## Automated suite

`python -m pytest -q` completed with **130 passed**. The supported installer
then ran the same **130-test** suite again after its editable install. Coverage
includes:

- content-addressed, filtered bare-Git snapshots and exact source diffs;
- pylatexenc structural environments, labels, proof association, references,
  character/byte spans, normalized hashes, duplicate-label rejection, and
  persistent IDs for unlabeled claims;
- free-form and schema-1 YAML tasks, target validation, theorem/argument modes,
  host-enforced pause/reproof/counterexample policies, and correspondence
  review;
- deterministic graph serialization, cycles, dependency closure, reverse
  invalidation, minimal ready frontiers, and blocked descendants;
- SQLite transactions, interrupted-run recovery, unique open questions,
  question supersession, and status reads during a writer lock;
- required clarification diagnostics, separation of `SUSPECT_FALSE` from a
  certified counterexample, and rejection of semantic dependency cycles or
  out-of-batch state mutations;
- certification dependency order, structural type/value hashes, source/type
  reconciliation, environment revalidation, and manuscript/Lean discrepancy
  detection;
- rejection of `sorryAx`, direct axiom mappings, newly introduced project
  axioms, missing declaration values, failed builds, and host-controlled batch
  paths;
- deterministic batching, concurrency limit validation, and ordered Git merge;
- all earlier cache location, compiler execution, coarse GC, capacity,
  dependency sharing, app-server protocol, provider failure, model validation,
  RepoProver tool, one-shot manuscript, and MCP/skill isolation cases.

## Real Lean dependency-extractor gate

A generated Lean project with declarations `D`, `A`, `B`, and `T` was built
against Lean 4.28/Mathlib. The helper inspected Lean’s `Environment`, not source
text. It reported the expected project proof chain (`B` references `A`; `T`
references `B`), distinct structural proof/value hashes, common theorem type
hashes where appropriate, and empty axiom sets.

The helper imports the project aggregate module, traverses elaborated type and
value expressions, removes non-semantic metadata, collects `Expr.const`
references, calls Lean’s axiom collector, emits canonical JSON, and is packaged
in the wheel.

## Real incremental Codex/RepoProver acceptance

Project:
`/Users/vui1/repoprover-test-runs/feedback-loop-small-20260823`

The golden manuscript contains `lem:zero-add` and dependent `thm:add-zero`.
Initialization indexed both and recovered the explicit graph edge
`thm:add-zero -> lem:zero-add`.

The first attempted verification exposed a dependency-lease lifecycle bug
before any Codex turn. The run was transactionally recorded as setup failure.
The depot claim type is now itself a context manager with deterministic cleanup,
both main and parallel worker paths use that interface, and a lifecycle
regression test verifies that its OS lease is closed.

The successful first pass (run 3):

- started `2026-08-23T04:42:02.920983+00:00`;
- completed `2026-08-23T04:53:55.186559+00:00`;
- scheduled the lemma before the theorem;
- used one isolated Codex process per frontier batch;
- visibly started each child with apps/plugins/MCP servers and local/bundled
  skills disabled;
- merged only the assigned claim modules;
- independently rebuilt after each merge;
- extracted and certified both elaborated declarations; and
- exited 0 with outcome `verified` and a clean project worktree.

Certificates:

| Claim | Lean declaration | Type hash | Value hash |
|---|---|---|---|
| `lem:zero-add` | `ManuscriptVerification.lem_zero_add` | `2d63366dac90651290b5ffd18f571a00d1f0403a2d7ea101181531551443de08` | `379def315b9c563f13e20e6655b3fb619595863abb5558e0d4b4c04be1196dc9` |
| `thm:add-zero` | `ManuscriptVerification.thm_add_zero` | `ec69184a0e9b97a5a83884c03c3f67d91ca885000a4f6fecc841df224e307483` | `e7b9599485332c8309dc4db50e64190febdebebbf628ab1906ba97f6a3450715` |

The Lean graph records the theorem’s direct dependency on
`ManuscriptVerification.lem_zero_add`.

The byte-identical second pass (run 4):

- started `2026-08-23T04:55:00.269415+00:00`;
- completed `2026-08-23T04:56:12.774636+00:00`;
- kept the same source snapshot commit;
- independently rebuilt and re-extracted the environment;
- started no Codex app-server;
- reused 2 certificates, reconciled 0 statements, and created 0 certificates;
  and
- exited 0 with outcome `verified`.

After the final 0.4.1 reinstall and hardening changes, a third byte-identical
pass (run 5) again exited 0, rebuilt and re-extracted Lean, reused both
certificates, and created no Codex batch directory. Its canonical manifest
contains non-null 64-character manuscript, Lean, and combined graph hashes.
The persistent project ended clean at
`90d745a1daecd1362b05b6586c43b4a44b0f62d0`.

## Cache/storage state

The acceptance reused dependency key `e50bd489673215a5b89a0dc2` and shared
depot
`/Users/vui1/.cache/repoprover-codex/lake/dependencies/deps-e50bd489673215a5b89a0dc2`.
No Mathlib dependency tree was duplicated per proof batch.

At the end of the live passes:

- managed cache: 7.57 GiB;
- dependency depots: 7.06 GiB;
- isolated project builds: 0.11 GiB;
- Mathlib downloads: 0.39 GiB;
- active reservations: 0 GiB; and
- filesystem free after the final pass: 114.82 GiB.

The cache-GC design accepted in 0.4.0 remains unchanged: coarse SQLite units,
transactional capacity reservations, OS leases, atomic quarantine, one shared
deadline, and no recursive rescan inside the eviction loop. The 10,000-file
operation-count regression remains in the suite.

## Installer, packaging, and repository hygiene

`scripts/install-dev.sh` passed under Python 3.13 using uv. It installed 0.4.1
into `/Users/vui1/.venvs/repoprover-codex`, compiled and executed its required C
probe with `/usr/bin/clang`, initialized the central cache, and passed all 130
tests. The installed command resolves through `/Users/vui1/.local/bin` to that
environment.

`uv build` produced the 0.4.1 sdist and wheel in a temporary directory under the
managed cache. After package metadata was tightened to require Python 3.13, a
final `uv build --wheel` repeated the wheel gate. A fresh Python 3.13
environment installed only that final wheel and its declared dependencies,
confirmed `Requires-Python: >=3.13`, imported version 0.4.1, found the packaged
`DependencyExtractor.lean`, rendered CLI help, and passed the compiler
compile/run probe. The temporary build environments were then moved to Trash.

The Markdown link check covered 15 front-facing/report/help files with zero
broken local links. Python compileall and Ruff correctness/import checks passed
for the incremental subsystem. `detect-secrets` reported zero findings;
credential/private-key filename checks reported none; `.gitignore` excludes
environments, caches, build output, secrets, editor metadata, and temporary
files. `git diff --check` passed, and ignored test/build residue was removed
from the source worktree.

The user-owned origin is `git@github.com:vitskov/repoprover-codex.git`. This
report does not authorize publication; no changes from this pass have been
pushed at the time of writing. Upstream RepoProver integration remains
untouched.

## Platform boundary

Linux remains a supported target in the portable implementation, but no Linux
machine was available for this acceptance. Project/cache locking requires
POSIX `flock`, matching the documented macOS/Linux support boundary.
