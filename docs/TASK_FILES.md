# Task files

The task file is authoritative input. Keep it with the manuscript or in a
separate local task folder, pass its path during `manuscript init`, and edit it
between passes when the verification scope changes.

## Free-form text or Markdown

Any nonempty UTF-8 file other than `.yaml` or `.yml` is passed as a free-form
request. It can contain multiple paragraphs, assumptions, exclusions, desired
formal statements, and acceptance criteria.

```markdown
Check every named lemma, proposition, theorem, and corollary in the manuscript.
Preserve the manuscript's stated assumptions. Audit the written argument, not
only theorem correctness. Separate ambiguous, unproved, and false claims. Do
not use sorry, admit, or new axioms.
```

Free-form tasks target every indexed theorem-like object by default. Be explicit
about scope: “all claims” can require many proof batches.

## Structured `VERIFY.yaml`

YAML supports deterministic target selection and policy:

```yaml
schema: 1
mode: argument-audit

targets:
  - thm:main
  - cor:dimension

policy:
  pause_on_ambiguity: true
  preserve_certified: true
  counterexample_search: true
  require_statement_correspondence_review: false

instructions: >
  Use the literal stated assumptions. Record any implicit dependency used by
  the Lean proof but absent from the manuscript references.
```

Modes:

- `theorem`: check whether each statement follows from its stated assumptions;
  proof-prose-only edits do not invalidate a certificate.
- `argument-audit`: audit the manuscript’s written proof path; proof text edits
  mark the claim and its dependents dirty.

Targets are stable claim IDs. Explicit `\label{...}` values are preferred IDs,
such as `thm:main`; unlabeled objects receive persistent generated IDs visible
through `manuscript graph` and `.repoprover/exports/claims.json`.

`require_statement_correspondence_review: true` leaves new mappings in
`STATEMENT_DRAFTED` until a human reviews the Lean file and runs:

```bash
repoprover-codex manuscript correspondence \
  --project "$PROJECT" \
  --approve thm:main
```

The next verification independently builds and certifies the approved mapping.
Reject an unfaithful proposal with `--reject CLAIM --reason TEXT`.

The other policy switches are enforced by the host, not merely added to the
agent prompt:

- `pause_on_ambiguity: false` prevents an agent from opening a clarification;
  it must record an unresolved result instead.
- `preserve_certified: false` discards reusable certificates and approved
  mappings in the selected dependency slice, forcing fresh correspondence and
  proof validation.
- `counterexample_search: false` prevents the agent from submitting a
  counterexample result. A merely suspected counterexample is recorded as
  `SUSPECT_FALSE`, never as a proved falsity result.
