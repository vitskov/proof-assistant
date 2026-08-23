# Project task and verification scope

Users do not supply or maintain a separate task file. During project creation,
Proof Assistant creates and versions:

```text
$PROJECT/VERIFY.yaml
```

Choose **Use default task** or edit the seeded request with **Customize task**
in the TUI. The workflow service validates and serializes the result; ordinary
users do not need to write YAML.

## Default task

The default requests verification of every indexed theorem-like claim under
its stated assumptions. Its semantic content is equivalent to:

```yaml
schema: 1
mode: theorem
targets: []
policy:
  pause_on_ambiguity: true
  preserve_certified: true
  counterexample_search: true
  require_statement_correspondence_review: false
instructions: >
  Verify every claimed lemma, proposition, theorem, corollary, and other
  theorem-like statement under its stated assumptions. Preserve distinctions
  between verified, ambiguous, unresolved, and false statements. Do not use
  sorry, admit, or new axioms.
```

An empty `targets` list means all theorem-like claims indexed from the project's
persisted main file and its recursive input closure. It does not include an
alternate root or orphaned LaTeX draft in the same source folder, and it is
different from “verify nothing.”

## Custom instructions

The built-in editor begins with the default instructions. Describe scope and
interpretation in plain language, for example:

```text
Verify every theorem-like claim. Treat the convention introduced in Section 2
as global, and report any result whose proof requires compactness but whose
statement does not assume it.
```

Proof Assistant preserves the structured safety policy around the text. A task
cannot authorize `sorry`, `admit`, unreviewed new axioms, mutation outside
assigned claim modules, or treating an unsuccessful proof search as evidence of
falsity.

## Modes and policies

`theorem` mode checks mathematical statements. A prose-only proof edit does not
invalidate a certificate when the statement remains identical.

`argument-audit` mode also treats the written argument as verification input.
Changing proof prose invalidates the affected audit slice even if the theorem
statement is unchanged.

Policy meanings:

| Policy | Meaning |
|---|---|
| `pause_on_ambiguity` | Permit a structured clarification request; otherwise record unresolved. |
| `preserve_certified` | Reuse current certificates when their source/environment provenance remains valid. |
| `counterexample_search` | Permit search for a Lean-checkable counterexample. |
| `require_statement_correspondence_review` | Require human approval of the prose-to-Lean mapping before certification. |

## Versioning and task changes

The project commits task changes to its own Git history. The backend analyzes
task impact separately from manuscript impact:

- selecting more targets schedules their dependency closure;
- selecting fewer targets changes current scope without deleting certificates;
- switching to argument-audit mode schedules required written-proof checks;
- changing instructions produces a reviewable task-impact plan; and
- policy changes are applied explicitly on the next confirmed iteration.

Use the TUI task editor rather than replacing `VERIFY.yaml` while verification
is active. Other interfaces may call the same workflow contract, but must pass a
validated task object rather than an arbitrary external path.
