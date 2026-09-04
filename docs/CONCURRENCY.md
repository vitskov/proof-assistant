# Concurrency and resource management

Proof Assistant controls three different kinds of work independently:

```text
AI provider turns    Lean checks and REPLs     Lake builds
        |                     |                    |
        v                     v                    v
 AI admission          Lean admission        build admission
```

This separation is a correctness and reliability boundary. A machine may have
room for several remote AI turns but only one memory-heavy Lean process, or
many Lean checks but only one I/O-heavy full build. Proof Assistant never turns
one generic worker count into all three limits.

The default policy is machine-wide, conservative, and adaptive. A new user can
start with automatic settings; an advanced user can inspect the exact effective
limits, override them, and return to automatic tuning without editing project
files.

## The everyday interface

Open **Settings** from the welcome screen or a project dashboard, then choose
**Concurrency / Resources**. This page shows:

- the configured policy and the effective values currently enforced;
- the source of each value: default, machine setting, environment, or CLI;
- usable CPU and memory, current pressure, swap and I/O observations;
- active and queued AI, Lean, and build work;
- Codex throttle/backoff state and Lean memory calibration state;
- the reasons automatic tuning chose its limits; and
- editable machine-wide controls and safe benchmark actions.

`Auto` and an effective number are deliberately displayed separately. For
example, `Lean REPL pool: Auto; Effective now: 4` means that four checks may be
admitted now, not that the limit is unknown.

Settings changes are previewed by the backend. Values that look unsafe for the
detected machine produce a confirmation dialog with a recommendation. Lowering
a limit stops new admissions above that limit but does not terminate work that
already holds a lease. The result tells you which changes applied live and
which take effect on the next run. Concurrent settings clients use revision
checks, so one client cannot silently overwrite a newer edit.

Choose **Legacy settings** for the remaining compatibility knobs. They are kept
on a separate page because they are not independent resource budgets:

| Legacy setting | Default | Current meaning |
|---|---:|---|
| proof batch workers (`jobs`) | 2 | logical worker-process fan-out; the machine AI controller still caps active provider turns |
| claims per batch | 8 | scheduling granularity for the next run |
| Lean REPLs per batch worker | 1 | compatibility value; global Lean admission is authoritative |
| old process-local AI guard | — | superseded on managed paths by machine AI admission |
| upstream raw build limit | — | superseded on managed paths by machine build admission |

Both pages use selectable, copyable text for paths, effective values, warnings,
telemetry, and benchmark results. The TUI is only a client: hardware detection,
validation, persistence, and controller updates belong to the workflow/backend
contracts.

## The three controllers

### AI admission

Every managed AI turn uses the same machine-wide AI resource namespace,
regardless of whether it uses Codex CLI or Claude CLI.
Proof, sketching, maintenance, clarification, diagnosis, review, reporting, and
duplicate proof attempts cannot each create their own independent allowance.
A reviewer therefore consumes one of the same slots as a prover.

Logical work and active remote work are different quantities. It is valid to
have 30 ready claims, 8 batch worker processes, and only 4 admitted AI turns.
The AI controller regulates the last number.

Work is admitted through a priority queue. Clarification diagnosis has highest
priority, followed by prerequisite proof work, maintenance and required review,
then speculative duplicate attempts and background reporting. Within a class,
the queue is stable and transactional.

Automatic AI starting values are Proof Assistant policy heuristics:

| Configured Codex plan | Initial | Automatic ceiling |
|---|---:|---:|
| Plus | 2 | 6 |
| Pro 5x | 4 | 12 |
| Pro 20x | 8 | 24 |
| Custom / unknown | 4 | 8 |

These are not claims about official OpenAI simultaneous-session limits. Proof
Assistant does not scrape the ChatGPT interface or inspect authentication data
to infer a subscription. Select the policy profile yourself and let the
controller adapt to observed behavior.

In adaptive mode, stable successful operation with queued work increases the
limit slowly by one, up to the configured ceiling. Explicit rate limiting or
throttling reduces it by approximately half. Repeated transient service errors
reduce it by one. `Retry-After` is honored when available; otherwise the
controller uses jittered exponential backoff based on:

```text
30 s -> 60 s -> 120 s -> 240 s -> 300 s maximum
```

The `economy`, `balanced`, and `throughput` budget policies control both
independent duplicate attempts and normal AIMD recovery. By default, additive
growth requires 12, 8, or 4 successful queued turns respectively. A numeric
`increase_after_successes` setting overrides that threshold without changing
the duplicate policy. `balanced` is the default. `agents_per_target` is a
ceiling, not an instruction to launch every duplicate immediately: one agent
starts first, repair is tried, and another independent attempt is admitted only
after the escalation policy finds it useful and the global AI budget permits
it.

### Lean admission

Lean concurrency is bounded by both usable CPU and usable memory:

```text
effective Lean pool = min(CPU cap, RAM cap, initial automatic cap)
```

Proof Assistant detects the allocation visible to its process rather than
blindly using the whole host. On Linux it accounts for CPU affinity, cgroup v1
or v2 quota/cpuset and memory limits, and SLURM CPU and memory variables where
available. On macOS and ordinary Linux it uses `psutil` for topology, available
memory, process memory, CPU, swap, and I/O observations. Missing optional
metrics degrade to `not available`; they do not prevent a run.

The initial CPU cap is approximately 60% of usable physical CPUs for an
interactive profile and 90% for a server profile. The memory reserve is:

```text
interactive: max(6 GiB, 30% of allocated RAM)
server:      max(4 GiB, 15% of allocated RAM)
```

Of the remaining currently available budget, 65% is available to the initial
REPL pool. Before calibration, each REPL is budgeted at 3 GiB by default. After
calibration, its budget is:

```text
max(3 GiB fallback, 2 GiB, 1.5 * measured p95 working RSS)
```

Lean admission is machine-global, while calibrations are project-specific.
Proof Assistant therefore computes the effective automatic budget from the
largest fresh measured p95 across all known profiles with the same visible
OS/architecture/CPU/RAM allocation. The fallback remains a hard floor. This
prevents an uncalibrated or lighter project from raising the shared Lean limit
after a heavier project established a safer value. Exact project profiles are
still used for provenance and the project-aware Settings display. An explicit
manual Lean pool remains authoritative after the normal unsafe-value warning.
Resolved-state fingerprints are maintained separately for AI, Lean, and build,
so discovering a new Lean profile cannot reset adapted AI or build limits.

Automatic startup is capped at 32 REPLs even on very large machines. It can
ramp only after telemetry supports doing so. The controller waits for repeated
observations and normally resizes no more often than every 45 seconds. A
persistent queue with CPU below 75% and green memory can grow the pool by one.
Red memory pressure or CPU above 92% without a throughput improvement can
shrink it. Sustained active paging contributes to the platform-aware pressure
state instead of bypassing it. Red or emergency memory pressure pauses new Lean
admissions; an emergency also reduces the effective limit to its minimum in
adaptive mode.

Both host-side extraction/certification operations and RepoProver's
`lean_check` dynamic tool pass through this global boundary.

### Build admission

Full `lake build` work has a separate, more conservative controller because it
can stress CPU, memory, filesystem I/O, and the shared Lake cache at once.
Automatic starting limits are:

| Usable physical CPUs / allocated RAM | Initial concurrent builds |
|---|---:|
| at most 16 CPUs, or at most 32 GiB | 1 |
| at most 32 CPUs, or at most 64 GiB | 2 |
| at most 64 CPUs, or at most 128 GiB | 3 |
| larger allocations | 4 |

The default hard ceiling is 8. Explicit settings may choose a different hard
ceiling, but the TUI warns about values that are unreasonable for the detected
machine.

Targeted builds are preferred over full builds while proof work is being
integrated. A targeted `lake build ManuscriptVerification` has higher admission
priority than an independent full-project certification build. Yellow pressure
admits at most one full build machine-wide: it prevents additional build
pressure without making forward progress depend on memory returning to green.
The check is serialized across processes. Red or emergency pressure blocks all
new builds.
In adaptive mode, red memory pressure or I/O pressure can reduce the limit;
green pressure, queued work, and an observed throughput gain can increase it.
The controller uses repeated observations and a roughly 60-second resize
interval.

Agent-initiated `lake build` commands, batch bootstrap/final builds, merged
frontier builds, and the final full certification build all use this admission
boundary in the managed manuscript workflow.

## Automatic, adaptive, and fixed modes

The TUI calls the normal mode **Auto / Adaptive**. In YAML and environment
configuration its canonical name is `adaptive`; the CLI accepts both `auto`
and `adaptive`.

- **Adaptive** derives conservative initial values and changes effective limits
  from telemetry, queue, success, throttle, and pressure observations.
- **Fixed** requires numeric AI, Lean, and build values and disables ordinary
  limit adaptation. It is intended for reproducible debugging and benchmarks.

Fixed mode does not disable correctness boundaries or emergency protection.
Lease accounting, serialized integration, final Lean certification, red-memory
pauses, and full-build pressure guards remain in force.

`resource_profile: auto` selects `interactive` for a local desktop session and
`server` for a headless or SSH session. Set the profile explicitly if that
classification does not match how the machine should be used.

## Machine settings and precedence

Concurrency policy is machine-wide because CPU, RAM, the Codex account budget,
and the shared cache belong to the machine rather than to one manuscript. The
normal settings file is:

```text
$XDG_CONFIG_HOME/proof-assistant/settings.yaml
```

or, when `XDG_CONFIG_HOME` is unset:

```text
$HOME/.config/proof-assistant/settings.yaml
```

The file declares `scope: MACHINE`, uses an atomic revisioned update, and is
written with mode `0600`. Proof Assistant refuses a custom machine-settings
path under Dropbox. Use the TUI to update it; if it must be edited by hand,
close settings clients and increment the revision so later compare-and-swap
updates cannot mistake it for the previous version.

Configuration resolves leaf by leaf in this order, highest precedence first:

```text
explicit CLI override
> environment variable
> optional future PROJECT overlay
> persisted MACHINE setting
> automatic/default policy
```

The resolver and workflow contracts already carry an explicit `PROJECT` scope,
but project overlays are intentionally not persisted or enabled yet. This keeps
today's behavior unambiguous while allowing a later per-project patch to be
inserted without changing machine storage or precedence.

The TUI shows the winning source for every impactful value. CLI and environment
overrides do not rewrite the machine YAML.

### YAML shape

The settings file stores only overrides. This complete example shows the
available structure and defaults:

```yaml
schema_version: 1
scope: MACHINE
revision: 1
concurrency:
  mode: adaptive
  resource_profile: auto
  telemetry_enabled: true

  ai:
    plan: unknown
    initial: auto
    hard_max: auto
    minimum: 1
    budget_policy: balanced
    increase_after_successes: auto
    increase_cooldown_seconds: 60.0
    throttle_multiplier: 0.5

  lean:
    pool_size: auto
    min_pool: 1
    max_pool: auto
    memory_calibration: true
    fallback_memory_per_repl_gib: 3.0
    p95_safety_multiplier: 1.5
    initial_auto_cap: 32

  build:
    max_concurrent: auto
    min_concurrent: 1
    hard_max: 8

  scheduler:
    agents_per_target_initial: 1
    agents_per_target_max: 4
    dependency_priority: true
    duplicate_agent_escalation: true

  legacy:
    jobs: 2
    batch_size: 8
    lean_pool_size: 1
```

Unknown keys and invalid combinations fail validation instead of being silently
ignored. Fixed mode is rejected unless `ai.initial`, `lean.pool_size`, and
`build.max_concurrent` are numeric.

### Environment overrides

The supported environment variables are:

```bash
export PROOF_ASSISTANT_CONCURRENCY_MODE=fixed
export PROOF_ASSISTANT_AI_CONCURRENCY=4
export PROOF_ASSISTANT_LEAN_POOL_SIZE=2
export PROOF_ASSISTANT_MAX_BUILDS=1
export PROOF_ASSISTANT_AGENTS_PER_TARGET=2
```

Each numeric override is an exact limit for its resource: for example the AI
value sets both its starting value and ceiling for that process. Positive
integers are required.

### CLI overrides

The concurrency-aware `smoke`, `repoprover-prove`, and `manuscript verify`
commands accept:

```text
--concurrency auto|adaptive|fixed
--ai-concurrency N
--lean-pool N
--max-builds N
--agents-per-target N
--codex-plan plus|pro-5x|pro-20x|unknown
--resource-profile auto|interactive|server
```

Numeric CLI values are exact one-run overrides and therefore set the initial
value and ceiling together. In fixed mode, supply all three resource values:

```bash
proof-assistant manuscript verify \
  --project "$PROJECT" \
  --model gpt-5.6-sol \
  --concurrency fixed \
  --ai-concurrency 2 \
  --lean-pool 1 \
  --max-builds 1
```

The older `--jobs`, `--batch-size`, and `--lean-pool-size` options remain for
compatibility. They are the same settings shown on the Legacy page; they do not
replace the three machine admission controllers.

## Telemetry and pressure

The backend samples low-cost telemetry approximately every five seconds during
a verification run. The same data model supplies the controllers, TUI, and run
provenance; the UI does not implement a second hardware-measurement path.
Collected observations include CPU utilization and load, available memory,
swap occupancy and active swap-out rate, process RSS/PSS where the platform
exposes it, disk I/O wait where available, queue depths, and Linux
pressure-stall information from `/proc/pressure` when present.

Memory pressure is classified by an OS-specific policy. macOS first consults
the optional XNU `kern.memorystatus_vm_pressure_level` state, then combines it
with available memory and active swap-out. If the native query is unavailable,
telemetry continues with available memory and swap-out, or available memory
alone when `psutil` does not expose a useful `sout` counter.

The native level mapping follows Apple's
[XNU memorystatus documentation](https://github.com/apple-oss-distributions/xnu/blob/main/doc/vm/memorystatus_notify.md):
0 normal, 1 warning, 2 urgent (equivalent to warning), and 3 critical. The
adapter uses `/usr/sbin/sysctl` with a short timeout and treats any missing,
denied, malformed, or unknown response as an unavailable optional signal.

macOS available-memory floors are:

| State | Trigger | Admission response |
|---|---|---|
| Green | more than 20% available, native normal/unknown, and no sustained active swap-out | normal admission; adaptive growth may occur |
| Yellow | 12–20% available, native warning/urgent, or sustained substantial swap-out | no Lean growth; at most one full build machine-wide |
| Red | 6–12% available or native critical | pause new Lean and build work; adapt downward |
| Emergency | at most 6% available | shed admission pressure and reduce adaptive pools toward their minimum |

Native critical produces RED immediately rather than EMERGENCY by itself.
Swap occupancy and changes to occupancy are observational only. The active
swap-out threshold scales with memory size between 16 MiB/s and 128 MiB/s;
sustained activity can raise macOS to YELLOW. Ordinary worsening takes two
samples, while recovery takes four healthy samples. Native critical and an
emergency available-memory ratio escalate immediately.

Linux retains the existing 30%/15%/8% available-memory thresholds, cgroup-aware
memory allocation, and `/proc/pressure` collection. Stable swap occupancy has
no pressure meaning. Active swap-out above the scaled threshold is YELLOW on
the first observation candidate and RED when sustained; the shared hysteresis
prevents a transient sample from changing admission.

Telemetry can be disabled as a machine policy, but doing so removes the
observations needed for runtime adaptation and live pressure explanation.

## Dependency-aware scheduling

When several claims are ready, the scheduler prefers work that unblocks the
largest number of still-blocked requested descendants. Conceptually:

```text
unlock score = number of blocked requested descendants
```

Traversal is cycle-safe. Dependency priority can be disabled in Settings.
This scheduling preference changes work order, never the conditions required
for a certificate. Duplicate-agent escalation likewise changes search effort,
not the authoritative Lean result.

## Durable leases and multiple processes

The three controllers share a small SQLite admission database at:

```text
$HOME/.cache/repoprover-codex/concurrency/admission.sqlite3
```

The older `repoprover-codex` cache root is retained deliberately after the
product rename so an installed machine does not create a second multi-gigabyte
Mathlib depot. The cache and Python environment must remain local and outside
Dropbox.

Admission, priority ordering, limits, and lease creation are transactional.
Leases have a TTL and heartbeat; expired leases and abandoned waiters are
reclaimed during admission. Lowering a limit never revokes an active lease.
This lets detached batch processes and multiple TUI clients observe one
machine-wide budget without making a project or a TUI process the resource
owner.

For distributed execution, AI capacity remains global to the Codex account,
while Lean and build capacity belong to each node. The concurrency package
provides coordinator-owned AI lease requests with worker/task identity,
heartbeat, release, ownership validation, TTL reclamation, and separately
keyed per-node Lean/build controllers. A deployment must connect those protocol
objects through its coordinator transport; workers must not each construct a
full independent AI allowance. Proof Assistant's normal manuscript workflow is
currently local-process/multi-process, not a bundled multi-node transport or
SLURM launcher.

## Calibration and benchmark commands

The calibration schema keys profiles by the relevant environment: OS and
architecture, visible CPU and RAM allocation, Lean version, Mathlib revision,
project/import profile, Codex plan, model, and backend. Records live under:

```text
$HOME/.cache/repoprover-codex/concurrency/calibration/
```

The TUI offers three benchmark actions. Open Settings from a project dashboard
to give the screen an exact project calibration context; the global welcome
screen deliberately disables the real Lean benchmark and project-profile reset.
The CLI equivalents are:

```bash
proof-assistant benchmark codex-concurrency
proof-assistant benchmark lean-concurrency [--project "$PROJECT"]
proof-assistant benchmark build-concurrency [--project "$PROJECT"]
```

These operations are deliberately conservative:

- the default Codex benchmark records the policy recommendation and sends no
  Codex request;
- `--allow-codex-traffic` explicitly authorizes a tiny harmless Codex probe on
  an idle AI queue, currently at widths no greater than two;
- the Lean benchmark with `--project` starts two sequential disposable REPLs,
  imports the representative project root, runs bounded harmless checks, and
  records warm idle, median working, p95 working, and maximum process-tree RSS;
- the Lean probe obtains an exclusive machine Lean calibration lease and
  refuses to start when Lean work is active or queued, so unmanaged REPLs are
  never launched beside a verification;
- a Lean benchmark without `--project` retains the conservative uncalibrated
  policy fallback and is explicitly reported as such; and
- the build benchmark records the conservative recommendation without starting
  concurrent builds.

Every result states the tested values, recommendation, whether Codex traffic
was used, and the calibration record path. A benchmark never silently applies
its recommendation. The current actions establish the safe calibration and
persistence boundary; they are not stress tests and do not claim measured
throughput where no real work was run. A project-backed Lean record is reused
only when its OS/architecture, visible CPU/RAM allocation, effective Lean
version, Mathlib revision, import profile, and configured plan match exactly,
and only while the record is at most 30 days old. A missing, stale, or mismatched
record uses the 3 GiB-per-REPL fallback. Runtime provenance identifies the
selected profile and its measured p95/budget.

The same TUI section provides **Reset project Lean calibration** and **Reset
adaptive history**. Profile reset deletes only the exact environment-keyed
record and requires an idle Lean queue. Adaptive-history reset clears AI
latency/success/throttle/backoff evidence and Lean/build pressure hysteresis;
it never revokes an admitted lease. Adaptive mode returns to the current
policy-derived starting limits, while fixed/manual values remain unchanged.

## Run provenance

Every persistent verification run records both configured and effective
concurrency. The initial environment record is:

```text
$PROJECT/.repoprover/runs/NNNNNN/environment.json
```

The completed or failed run record is:

```text
$PROJECT/.repoprover/runs/NNNNNN/run.json
```

The concurrency section includes the machine-settings revision, configured
values, effective initial/final limits, active and queued observations, AI
latency/success/throttle/backoff state, pressure state, auto-tuning reasons,
sample count, peak and mean active work, peak queues, pressure events, and the
latest telemetry snapshot. The project database also stores the configured,
initial-effective, final-effective, and telemetry summaries for reporting.

Machine concurrency is intentionally excluded from a detached verification
job's semantic request fingerprint. A safe live limit change must not make a
second TUI treat the same proof request as a different job. The complete
resource behavior is retained in run provenance instead.

## Correctness and safety invariants

Concurrency changes do not weaken verification:

- the final independent Lean build and kernel evidence remain authoritative;
- agent proposals are not certificates;
- host merges, source snapshots, state transitions, and final integration stay
  serialized or transactional where required;
- separate worktrees prevent proof workers from writing the same checkout;
- pressure adaptation changes admission only, not proof semantics;
- reducing a limit lets admitted work reach a safe boundary;
- fixed mode makes resource limits reproducible without disabling final
  certification; and
- controllers are test-injectable and can be exercised without real Codex
  subscription traffic.

## Migration audit: upstream limits and active paths

Proof Assistant integrates selected RepoProver agent/tool behavior; it does not
run RepoProver's research coordinator wholesale. The following audit prevents
hidden upstream defaults from being mistaken for Proof Assistant policy:

| Audited upstream mechanism | Upstream default | Proof Assistant status |
|---|---:|---|
| `BookCoordinator.max_concurrent_sketchers` | 20 | coordinator is not instantiated by the manuscript/TUI workflow |
| `BookCoordinator.max_concurrent_contributors` | 256 | coordinator is not instantiated; logical PA batches use machine AI admission |
| coordinator event-loop executor | 512 threads | coordinator is not instantiated |
| `BookCoordinator.lean_pool_size` | 24 | not authoritative; managed Lean work uses machine Lean admission |
| coordinator `agents_per_target` and `32 // targets` cap | 1 default | not used as eager fan-out; PA uses its escalation ceiling and global AI budget |
| `DistributedWorker.max_concurrent` | 512 | upstream distributed worker is not used by the normal workflow |
| `DistributedWorker.lean_pool_size` | 24 | upstream distributed worker is not used; PA exposes per-node controller primitives |
| upstream `MAX_CONCURRENT_BUILDS` | 8 | not the managed workflow's source of truth; PA build admission gates bootstrap, agent, merge, and certification builds |
| upstream reviewer thread pool | 2 | upstream reviewer path is not used; any managed review AI call shares PA's AI controller |
| RepoProver contributor agent and dynamic tools | varies | used through Proof Assistant's narrow adapter; provider turns, `lean_check`, and `lake build` calls are wrapped by PA admission |
| RepoProver process-local Lean pool | compatibility setting | initialized per batch, but global cross-process Lean admission is authoritative |
| Proof Assistant `jobs` / batch size | 2 / 8 | retained as visible Legacy scheduling controls; they cannot raise AI, Lean, or build admission limits |

The managed CLI and workflow pass a concurrency runtime specification into
AI-driver and worker processes. A small process-local Codex guard remains only as a
defensive fallback for an unmanaged caller that constructs a backend without a
runtime specification; it is not the source of truth for normal Proof
Assistant runs.

Upstream RepoProver source is not modified by this migration.
