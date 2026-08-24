# Python 3.13 Optimization Recommendation for Proof Assistant

Repository: `/home/runner/work/proof-assistant/proof-assistant`  
Minimum Python: `>=3.13`

## Executive summary

The codebase already uses many modern Python features and has strong lint/test discipline, but it is **not yet fully optimized** for Python 3.13-era typing safety and runtime efficiency.

The most important next steps are:
1. enforce strict type checking in CI,
2. reduce broad `Any` usage at protocol/JSON boundaries with structured typed contracts,
3. selectively apply `slots=True` to high-volume dataclasses after measurement.

This priority order is based on **efficiency impact** and **code safety/maintainability**.

---

## Current-state assessment

### Strengths already present
- Python 3.13 is consistently targeted in project metadata and CI.
- Modern typing/features are already used broadly (`StrEnum`, `|` unions, `Self`, frozen dataclasses, postponed annotations).
- CI quality gates already include linting and tests.

### Gaps limiting full Python 3.13 leverage
1. **Large dynamic typing surface (`Any`)**, especially around protocol/integration payloads.
2. **No strict static type-check gate in CI** (ruff + pytest only).
3. **No slot-enabled dataclasses observed**, missing potential memory/attribute-access gains in object-heavy paths.
4. **No `py.typed` marker**, so downstream type consumers cannot rely on packaged typing support.
5. **Schema boundaries are not consistently modeled with typed contracts** (`TypedDict` / narrow DTOs).

---

## Prioritized implementation roadmap

## P0 — Highest priority

### P0.1 Add strict static type checking in CI

**Why this is first**  
This delivers the largest immediate safety and maintainability gain per unit effort, and it prevents future regressions while other improvements are rolled out.

**Actions**
- Add strict type checker configuration (staged if necessary, but with a clear endpoint).
- Add CI job that runs type checks on `src/` (and selected tests where valuable).
- Block merges on new type errors.
- Document local command parity with CI in development docs.

**Expected impact**
- Efficiency: **Medium (indirect via less rework and fewer runtime failures)**
- Safety/Maintainability: **Very High**
- Delivery risk: **Medium** (initial type debt)

**Acceptance criteria**
- Type-checking runs in CI and is required.
- Baseline debt is either fixed or tracked with bounded/justified suppressions.
- New changes cannot silently increase untyped surface area.

---

### P0.2 Replace broad `Any` at protocol/JSON seams with typed contracts

**Why this is second (still P0)**  
The dominant safety gap today is unstructured dynamic payload handling in critical boundaries.

**Actions**
- Inventory high-traffic boundaries (e.g., backend/protocol/incremental/workflow seams).
- Introduce `TypedDict` or equivalent typed DTOs for stable payload schemas.
- Centralize parse/validate/coerce logic near each boundary.
- Replace repeated `dict[str, Any]` plumbing with named, typed contracts.

**Expected impact**
- Efficiency: **Medium**
- Safety/Maintainability: **Very High**
- Delivery risk: **Medium**

**Acceptance criteria**
- Material reduction of broad `Any` in boundary modules.
- Contract violations fail early with explicit diagnostics.
- Type checker can traverse key request/response paths without broad ignores.

---

## P1 — High-value targeted optimization

### P1.1 Apply `slots=True` selectively to high-volume dataclasses

**Why here**  
This is the clearest direct runtime/memory optimization opportunity, but it should follow typing hardening and be evidence-driven.

**Actions**
- Identify object-heavy dataclasses in incremental/concurrency/workflow paths.
- Add `slots=True` only where attribute shape is stable and dynamic attribute writes are unnecessary.
- Validate serialization/reflection compatibility.
- Benchmark before/after for representative workloads.

**Expected impact**
- Efficiency: **High** (when targeted to hot/high-volume objects)
- Safety/Maintainability: **Medium**
- Delivery risk: **Medium**

**Acceptance criteria**
- Measurable memory reduction and/or throughput improvement in representative runs.
- No behavioral regressions.

---

### P1.2 Ship `py.typed`

**Why**  
This improves package quality for typed downstream consumers and clarifies support expectations.

**Actions**
- Add `py.typed` to the package and ensure wheel/sdist include it.
- Document typed-consumer expectations.

**Expected impact**
- Efficiency: **Low**
- Safety/Maintainability: **Medium**
- Delivery risk: **Low**

**Acceptance criteria**
- Artifacts contain `py.typed`.
- Downstream typing smoke checks succeed.

---

## P2 — Cleanup and long-term consistency

### P2.1 Introduce named aliases for repeated nested types
- Improves readability and reduces duplicated ad-hoc nested annotations.

### P2.2 Expand typing policy/lint conventions
- Encourage precise modern typing style and reduce future `Any` creep.

**Expected impact (P2 overall)**
- Efficiency: **Low**
- Safety/Maintainability: **Medium**
- Delivery risk: **Low**

---

## Suggested sequencing

1. **PR 1:** strict type-check infrastructure + CI gate + docs
2. **PR 2:** boundary typing in backend/protocol seams
3. **PR 3:** boundary typing in incremental/workflow seams
4. **PR 4:** selective dataclass slotting with benchmark evidence
5. **PR 5:** `py.typed` + alias/style follow-up cleanup

This order gives immediate safety gains first, then targeted runtime optimization, then maintenance polish.

---

## Overall definition of done

- Strict type checking is mandatory in CI.
- Broad `Any` usage is materially reduced in boundary-critical modules.
- Selective `slots=True` changes show measured efficiency gains.
- `py.typed` is included in distributable artifacts.
- Developer docs reflect local commands that match CI gates.

---

## Final recommendation

Proceed with the roadmap above, starting with **P0.1 + P0.2** as mandatory safety foundations. Then implement **P1.1** using benchmark-backed, selective slotting for high-volume dataclasses. Finish with packaging and consistency improvements (**P1.2 + P2**) to lock in long-term maintainability.
