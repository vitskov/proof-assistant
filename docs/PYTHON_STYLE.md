# Python 3.13 development style

Proof Assistant supports Python 3.13 and publishes inline type information.
Treat typing and boundary validation as runtime design tools, not annotation
cleanup performed at the end of a change.

## Required local gates

Run the same checks as CI from an external `uv` environment:

```bash
uv run --no-sync ruff check src tests scripts
uv run --no-sync python scripts/check_typing_policy.py
uv run --no-sync mypy
uv run --no-sync pytest -q
```

Mypy is strict over all of `src/proof_assistant`. Do not add a global ignore or
weaken strict mode to land a feature. A narrow suppression is acceptable only
at an unavoidable untyped dependency or reflective operation, with the reason
on the same line or in the adjacent configuration. The typing-policy gate also
caps remaining explicit `Any` debt and keeps critical boundary modules
`Any`-free.

## Data and boundary conventions

- Accept `object` at an untrusted boundary, validate once, and return a narrow
  type. Use `json_types.py` for decoded JSON and JSON-bound persistence.
- Use `JSONObject` / `JSONValue` for genuinely variable JSON. Use a named
  `TypedDict`, immutable dataclass, or protocol when fields are stable.
- Reject malformed external payloads at the boundary with a domain-specific,
  path-aware diagnostic. Do not spread unchecked casts through business logic.
- Give optional dependencies a small structural `Protocol`; keep their dynamic
  imports localized in the adapter that owns them.
- Centralize unavoidable dynamic library boundaries, such as SQLite row
  narrowing and dataclass reflection, instead of repeating casts at call sites.
- Annotate CLI handlers with `argparse.Namespace` and callbacks with their exact
  argument and return types.

`py.typed` is a compatibility promise: public annotations must describe the
installed runtime behavior. Build artifacts and a downstream checker smoke test
must be part of release validation.

## Dataclasses and runtime optimization

Use `slots=True` only for stable-shape records that are instantiated often.
Check serialization, reflection, inheritance, weak references, and any dynamic
attribute writes before changing an existing class. Measure a representative
workload before and after; do not slot every dataclass mechanically.

The initial Python 3.13 optimization selected three immutable incremental graph
records. On CPython 3.13.15 / macOS x86_64, allocating 100,000 records with
shared representative values produced these `tracemalloc` peaks:

| Record | Before | Slotted | Reduction |
| --- | ---: | ---: | ---: |
| `SourceObject` | 24.42 MiB | 19.07 MiB | 21.9% |
| `ManuscriptEdge` | 11.45 MiB | 7.63 MiB | 33.4% |
| `LeanDeclaration` | 12.21 MiB | 8.39 MiB | 31.3% |

Construction time did not improve materially, so the supported claim is lower
memory use—not higher throughput.

## Recommendations for future changes

1. Spend the remaining `Any` budget down when touching concurrency adapters,
   SQLite persistence, presentation normalization, or optional RepoProver code;
   never raise the budget to accommodate a feature.
2. Prefer schema-specific `TypedDict` contracts once a variable JSON payload
   becomes stable across more than one producer or consumer.
3. Keep type assertions near I/O and dependency seams. If application logic
   needs repeated `cast()` calls, improve the owning boundary instead.
4. Benchmark additional slot candidates only in object-heavy incremental or
   scheduling paths and record the workload and result here.
5. Test public typing against the built wheel, not only the editable checkout,
   before changing the package's supported type surface.
