# Proof Assistant TUI Design

## Source of truth

- Status: Implemented responsive baseline and living design contract
- Last refreshed: 2026-09-03
- Primary product surfaces: terminal project catalog, project creation, project
  dashboard, live verification, author clarification, findings and failure
  analysis, report viewing, and machine/project settings.
- Evidence reviewed:
  - `README.md`
  - `docs/USAGE.md`
  - `docs/AI_PROVIDERS.md`
  - `src/proof_assistant/tui/app.py`
  - `src/proof_assistant/tui/screens.py`
  - `src/proof_assistant/tui/settings/screens.py`
  - `src/proof_assistant/tui/theme.py`
  - `tests/test_tui.py`
  - `tests/test_tui_providers.py`
- External design references:
  - [Apple Human Interface Guidelines: Settings](https://developer.apple.com/design/human-interface-guidelines/settings)
  - [Microsoft: Guidelines for app settings](https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings)
  - [Microsoft: Navigation design basics](https://learn.microsoft.com/en-us/windows/apps/design/basics/navigation-basics)
  - [GNOME HIG: Navigation](https://developer.gnome.org/hig/guidelines/navigation.html)
  - [GNOME HIG: View switchers](https://developer.gnome.org/hig/patterns/nav/view-switchers.html)
  - [GNOME HIG: Boxed lists](https://developer.gnome.org/hig/patterns/containers/boxed-lists.html)
  - [Nielsen Norman Group: Progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/)
  - [Textual: ContentSwitcher](https://textual.textualize.io/widgets/content_switcher/)
  - [Textual: OptionList](https://textual.textualize.io/widgets/option_list/)
  - [Textual: DataTable](https://textual.textualize.io/widgets/data_table/)
  - [Textual: TabbedContent](https://textual.textualize.io/widgets/tabbed_content/)

This file is the design contract for user-facing Textual work. New screens and
substantial changes to existing screens should satisfy its geometry,
interaction, accessibility, and test requirements. If implementation exposes a
conflict, update this file deliberately rather than adding another one-off
layout rule.

## Brand

- Personality: a calm, rigorous formal-verification workbench. It should feel
  precise and capable without feeling like a raw diagnostic console.
- Trust signals: clear scope, explicit state, visible provenance, restrained
  language, deterministic actions, and an unambiguous distinction between AI
  assistance and Lean certification.
- Visual identity: preserve the existing warm Flexoki-derived Proof Ink and
  Proof Paper palettes and their semantic focus, warning, error, success, and
  informational tokens.
- Avoid:
  - giant diagnostic dumps before the user's primary task;
  - hidden controls that exist only below a long scroll;
  - color-only status;
  - unexplained model/provider jargon;
  - screen-specific arbitrary fixed heights;
  - dense walls of equally prominent buttons;
  - implying that a language model certifies a proof.

## Product goals

- Goals:
  - make the current project, scope, state, and next action obvious;
  - let keyboard-first users complete every flow efficiently over SSH;
  - keep expert detail available without making it the default reading path;
  - make long paths, source excerpts, findings, logs, and role matrices usable
    at realistic terminal sizes;
  - preserve exact, copyable technical evidence when it is needed.
- Non-goals:
  - imitating a desktop GUI pixel for pixel;
  - hiding verification evidence or reducing expert capability;
  - adding a second frontend framework or a new visual dependency;
  - optimizing only for a large local terminal.
- Success signals:
  - users can locate every major setting from recognition rather than recall;
  - the primary action is visible at 80x24;
  - common screens need no page-length scrolling at 120x40;
  - all eight RepoProver role assignments are visible together at 120x40;
  - changing terminal size never loses the selected item, edit draft, or focus
    context;
  - tests verify visible geometry, not merely that widgets exist in the DOM.

## Personas and jobs

- Primary personas:
  - a mathematics author who wants clear verification results and actionable
    clarification requests;
  - an expert operator who tunes AI roles, providers, and machine resources;
  - a maintainer diagnosing provider, Lean, dependency, or installation state.
- User jobs:
  - create or resume a manuscript-verification project;
  - understand what is running and whether author action is required;
  - answer a clarification against exact source context;
  - inspect findings, dependencies, and persisted evidence;
  - select a provider and review or customize role-aware model policy;
  - tune machine concurrency without confusing configured and effective state.
- Key contexts of use:
  - local terminals and remote SSH sessions;
  - keyboard-only operation, with mouse interaction optional;
  - terminal sizes ranging from 80x24 to large desktop windows;
  - long filesystem paths, long theorem names, and variable-size reports;
  - async provider checks and long-running verification jobs.

## Information architecture

### Primary navigation

Keep global navigation stable:

- the visible, focusable **Menu** control in the header;
- `Ctrl+P` as the only global keyboard binding;
- Help, Projects, Settings, Theme, and Quit as named Menu destinations; and
- live actions from the current screen above those application destinations.

The Menu is searchable, opens with the current focus as its return point, and
restores that exact screen/focus when dismissed with `Esc`. Screen-specific
tasks belong in the visible workspace and Menu. Do not introduce function-key,
unmodified-letter, bracket, `Ctrl+Enter`, or Vim/Emacs command aliases. During
first-run AI onboarding, Projects and Settings cannot bypass the readiness
gate; the current screen explains what remains incomplete. Quit remains an
explicit Menu action.

### Navigation depth

- Use at most two levels: a stable hub and one focused destination/editor.
- Use a flat settings navigator for peer categories and a list/detail pattern
  for collections such as projects, providers, roles, findings, and changed
  claims.
- Do not use a tree merely because data has labels and children. Use a tree
  only when understanding the hierarchy itself is the task, as in proof
  dependencies.
- Preserve and restore the last selected settings category and the selected row
  when returning from a detail editor.
- `Esc` always returns one conceptual level. Dirty editors intercept it with a
  save/discard/continue choice.

### Settings architecture

The Settings overlay is a persistent shell with these primary destinations:

1. **Verification AI**
2. **Runtime & resources**
3. **Advanced / compatibility**

`Verification AI` contains three peer views:

1. **Role assignments** — selected scope/provider, all eight roles, recommended
   policies, and save/discard state.
2. **Provider connections** — installation, authentication, readiness,
   credentials, model catalog, and provider diagnostics.
3. **Provider diagnostics** — provider fallback and exact catalog/configuration
   provenance that ordinary users do not need.

Machine and project policy use the same editor, selected by a scope switch in
the page title area:

- **Machine defaults**
- **This project: _name_**

When a project inherits machine settings, show the assignment matrix read-only
with an **Inherited** state and one **Customize this project** action. Do not
append a duplicate project form beneath the machine form.

The scope switch belongs only to **Role assignments**. Provider installation,
authentication, credentials, and account verification are always machine-owned,
even when Settings was opened from a project.

### First-run AI onboarding

First run is a focused onboarding flow, not the ordinary settings shell. A
machine with settings revision zero and no ready primary provider follows three
explicit steps:

1. **Choose provider** — show every supported provider with Ready, Needs login,
   Needs key, Missing, or Verification required state. Selecting a provider does
   not install, authenticate, or send a model request.
2. **Connect provider** — show only the selected provider's contextual install,
   login, API credential, recheck, or explicitly confirmed account-verification
   action. Exact commands and diagnostics are available in a detail pane.
3. **Review verification team** — show the eight provider-recommended role,
   model, and effort assignments before saving. The final action is **Save and
   continue to projects**.

First-run navigation contract:

- `Esc` returns to the previous onboarding step but cannot leave an unready
  setup for the main menu.
- Choosing **Projects** or **Settings** from Menu explains that one ready
  primary provider and a reviewed eight-role team must be saved first.
- **Quit** exits without treating onboarding as complete.
- Refresh/recheck preserves the selected provider and current step.
- An install, authentication, catalog, or network failure stays on the Connect
  step, keeps the safe next action visible, and exposes copyable details without
  expanding the page above the action.
- At 80x24, each step uses the entire workspace and keeps Back/Continue or
  Recheck/Exit in the fixed action bar.

### Core routes/screens

- Main menu / project catalog
- New project flow
- Project dashboard
- Live progress
- Clarification and change review
- Findings, failure analysis, and report viewer
- Recovery
- Settings shell and focused settings pages
- Modal confirmations and help
- First-run AI onboarding

### Content hierarchy

Every screen follows the same order:

1. Where am I? — title, breadcrumb, project/scope context.
2. What is the state? — a compact status summary, at most four rows before the
   primary workspace.
3. What can I inspect or change? — one dominant flexible workspace.
4. What happens next? — a persistent action/status bar.
5. Where is the expert detail? — a tab, inspector, collapsible region, or
   copyable source view reached explicitly.

## Design principles

### One screen, one subject

Each view has one primary question or task. Installation, authentication,
provider selection, role assignment, and project override are related, but they
are not one form. Split them into focused views under a stable shell.

### Recognition before recall

Show meaningful collections as lists or tables. The user should see all eight
roles and their current assignments rather than remember which values are
hidden behind a role dropdown.

### Progressive disclosure without concealment

Show the ordinary decision and its current value first. Put exact diagnostics,
catalog provenance, executable paths, and uncommon fallbacks behind clearly
labeled detail actions. The control that reveals detail must state what it
contains.

### Overview plus focused detail

For repeated objects, prefer list/detail:

- wide terminal: list and inspector side by side;
- compact terminal: list first, then push a focused detail view;
- returning from detail restores the selected row and scroll position.

### Scroll content, not navigation

Scrolling is a content behavior, not the information architecture.

- A screen has one primary vertical scroll owner.
- Header, context, and action/status bar remain visible.
- Do not nest scrollable read-only summaries inside a page-length scroll.
- Multiple large datasets use tabs or list/detail, not consecutive fixed-height
  panels.

### Defaults first, customization visible

Provider-aware recommended policies should make the common path easy, while the
complete resulting role matrix remains visible. Applying recommendations edits
a draft, reports how many assignments changed, and offers Undo. It does not
persist until Save.

Changing the provider for a scope changes only the provider in the current
draft and leaves the eight assignments untouched. **Apply provider defaults**
then replaces the complete matrix in one generation-checked operation; **Undo
defaults** restores the immediately preceding complete draft. Provider changes
never silently rewrite role assignments, and stale results from a previously
selected provider are discarded. Recommendation Undo exists until the draft is
saved, discarded, or replaced by another recommendation operation.

### Scope is always explicit

Machine settings and project overrides must never share an undifferentiated
form. Put the active scope in the title and action bar, label inheritance, and
make `Ctrl+S` save only the displayed scope.

### Evidence remains available

Summarize first, but never discard exact evidence. Long technical details
belong in a selectable, copyable, independently scrollable source/detail pane.
Code, logs, commands, and source excerpts do not soft-wrap; prose, paths, and
descriptions do.

### Tradeoffs

- Prefer one extra, clearly labeled navigation step over a 100-row settings
  page.
- Prefer a visible summary row plus a focused editor over eight simultaneous
  forms.
- Prefer stable geometry and keyboard context over maximum information density.
- Prefer explicit Save/Discard for coordinated multi-field edits; use immediate
  actions only for self-contained, reversible operations such as refresh.

## Visual language

- Color: use existing semantic theme tokens. Add semantic component tokens
  rather than literal colors in individual screens.
- Typography: terminal font is user-controlled. Establish hierarchy with
  concise titles, weight, spacing, borders, and semantic color.
- Spacing/layout rhythm:
  - one-row gap between major regions;
  - compact rows inside lists;
  - consistent label/control alignment;
  - no decorative empty space that displaces primary controls.
- Shape/radius/elevation: use the existing restrained round borders. A focused
  region gets one focus border; avoid boxing every line.
- Motion: no decorative animation. Loading indicators and progress movement
  communicate actual state only.
- Imagery/iconography: use small text glyphs only when paired with words, for
  example `● Ready`, `△ Needs login`, `★ Recommended`. Never rely on the glyph
  or color alone.

## Components

### Existing components to reuse

- `OptionList` for stable navigation and compact semantic lists.
- `DataTable(cursor_type="row")` for project, provider, role, and change
  rosters when columns materially aid comparison.
- `ContentSwitcher` for a settings shell or wide master/detail workspace only
  when every child is lightweight and safe to remain mounted while hidden.
- `TabbedContent` for three to five peer views or alternate representations of
  the same artifact.
- `Tree` only for proof/dependency hierarchy.
- `TextArea` or source widgets for selectable code, logs, and exact evidence.
- Existing semantic theme tokens and `CommandFooter`.

### New/changed components

- `SettingsShell`: stable category navigator, breadcrumb/scope header,
  `ContentSwitcher`, and fixed action/status bar.
- `RoleAssignmentRoster`: all eight roles with stage, role, model, effort,
  recommendation/inheritance state, and active/reserved lane state.
- `RoleInspector`: purpose, model, effort, capability validation, and per-role
  restore action.
- `ProviderRoster` and `ProviderInspector`: readiness list plus contextual
  remediation and progressive diagnostics.
- `ScopeSwitcher`: machine/project scope with explicit inheritance state.
- `ActionBar`: current scope, dirty/saved/error state, and prioritized actions.
- `ResponsiveToolbar`: visible primary action plus compact or stacked secondary
  actions on narrow terminals.
- `ScrollableDialogBody`: modal body scrolls while Cancel/Confirm stay fixed.

`ContentSwitcher` toggles display; it does not unmount inactive children. Hidden
panes must therefore own no workers, timers, credential value, or secret-bearing
draft, and no hidden element may remain in the active focus chain. A screen-owned
controller may refresh lightweight presentation widgets in hidden panes so the
next visit is current; asynchronous results are still generation-checked before
they can change a draft. Sensitive input is dynamically mounted only on the
visible connection page and is cleared and removed at every page or navigation
boundary. Tests inspect hidden focus and secret absence, while production tests
exercise stale-result rejection and transient-input destruction.

### Role-assignment reference layout

At 140 columns and wider:

```text
 Settings / Verification AI                       Scope: Machine defaults
 ┌ Settings ───────┬ Role assignments ─ Connections ─ Provider diagnostics ┐
 │ Verification AI│ Provider  Claude Code CLI                   Ready       │
 │ Runtime        │ [Use Claude recommendations]                            │
 │ Advanced       ├ Verification team ───────────────┬ Selected role ──────┤
 │                │ Role             Model   Effort  State  │ Independent  │
 │                │ Clarification    Fable   X-high  Recomm.│ Rechecks the │
 │                │ Diagnostics      Opus    High    Recomm.│ main proof.  │
 │                │ Sketch           Sonnet  Medium  Recomm.│              │
 │                │ Primary proof    Best    High    Recomm.│ Model Fable  │
 │                │ ▸ Independent    Fable   X-high  Recomm.│ Effort X-high│
 │                │ Maintenance      Sonnet  Medium  Recomm.│              │
 │                │ Review           Opus    High    Recomm.│ [Restore]    │
 │                │ Reporting        Haiku   Low     Recomm.│              │
 ├────────────────┴───────────────────────────────┴────────────────────────┤
 │ 8 roles configured · no unsaved changes        [Discard] [Save changes] │
 └─────────────────────────────────────────────────────────────────────────┘
```

At 120–139 columns, primary settings categories use a horizontal switcher, the
role roster uses the full width, and `Enter` opens a focused role detail view.
All eight rows and at least `Role | Model | Effort | State` are visible together
at 120x40. At 80–119 columns, Settings first shows a category page; a selected
destination replaces it, and the roster/detail replace one another. `Esc`
returns to the same selected row.

Roster cells use human display names. A long model name may be ellipsized in a
bounded column, but its exact provider model ID and full name are always visible
in the inspector and copyable detail. Ellipsis must not hide effort or state.

The canonical deterministic Claude Code fixture for design, Pilot, and SVG
snapshot review is:

| Stable `TaskKind` | Display role | Model | Effort |
| --- | --- | --- | --- |
| `clarification` | Author clarification | `fable` | Extra high (`xhigh`) |
| `diagnostic` | Scan / triage diagnostics | `opus` | High |
| `proof` | Primary prove agent | `best` | High |
| `sketch` | Sketch agent | `sonnet` | Medium |
| `maintenance` | Maintain / fix agent | `sonnet` | Medium |
| `review` | Math and engineering reviewers | `opus` | High |
| `duplicate_proof` | Independent prove agent | `fable` | Extra high (`xhigh`) |
| `reporting` | Progress / reporting agent | `haiku` | Low |

`duplicate_proof` is the stable backend/storage identifier. The user-facing role
name is **Independent prove agent** because the lane independently rechecks the
primary proof; do not expose “Duplicate proof” as a competing display name.

### Role editor state machine

- Entering an inspector starts from the scope-level draft.
- Model/effort edits update that draft and mark the corresponding row Custom.
- `Esc` from an inspector returns to the roster and preserves the draft without
  prompting.
- Leaving the role-assignment destination, changing scope, closing Settings, or
  returning to the main menu with a dirty draft offers **Save**, **Discard**, or
  **Continue editing**.
- `Ctrl+S` saves the displayed scope from either roster or inspector.
- Save is revision-checked. A stale revision keeps the draft and offers Reload
  or Compare; it never overwrites newer state.
- Save success updates the roster to the authoritative revision, clears dirty
  and Undo state, and restores focus to the edited row.
- Project inheritance is read-only until **Customize this project** creates a
  project draft. **Use machine defaults** previews removal of the override.

### Provider-connections reference layout

- Left/list region: every provider, readiness text, and whether it is used by
  the current scope.
- Detail region: selected provider version, authentication state, available
  actions, and collapsed model/executable diagnostics.
- Installation, login, credential, recheck, and account verification actions
  are contextual. Disabled actions include a textual reason.
- Never render the full all-provider diagnostic dump above the controls.

### Live-progress reference layout

- Persistent context: project, run state, elapsed time, progress counts, and
  current blocking/warning state in at most four rows.
- Wide: bounded stage rail on the left and the event stream on the right.
- Compact: peer **Events**, **Stages**, and **Sources** views, with Events as the
  ordinary default.
- The event stream owns flexible height and scrolling. New events do not move
  the action bar.
- **Detach** and **Request cancellation** remain visible. Detach only stops this
  observer; it never sends a cancellation request.

```text
 laplacians / Verification running                 23 of 41 claims
 ┌ Stages ─────────────┬ Events ───────────────────────────────────────────┐
 │ Done  Validate      │ 14:32 Proof batch 6 started                      │
 │ Done  Import        │ 14:33 Claim harmonic_decomposition certified     │
 │ Run   Prove         │ ...                                              │
 │ Wait  Review        │                                                  │
 └─────────────────────┴──────────────────────────────────────────────────┘
 Running normally                         [Detach] [Request cancellation]
```

### Clarification reference layout

- Persistent context: project, question number/count, affected file and lines.
- A compact **Best current guess** banner is always visible above the main
  workspace. It either shows the confidence-rated AI-assisted hypothesis or an
  honest unavailable state; it always says that the hypothesis is not a Lean
  result or confirmed author intent.
- The exact source segment is a single inline, read-only, syntax-highlighted
  LaTeX view. Clarification never suspends the TUI or launches an editor.
- Wide: exact source context and **Diagnosis & options** side by side.
- Compact: **Source context** and **Diagnosis & options** peer views; changing
  questions preserves the active view when possible.
- Diagnosis begins with the immutable observed problem and question origin,
  then shows the hypothesis, evidence IDs, alternatives, uncertainties,
  recommended author check, and provider/model/effort provenance. Generated
  prose never replaces the observed problem.
- Previous/Next and **Check manuscript changes** remain fixed. Long source and
  explanation content scroll inside the workspace, not the page.

```text
 laplacians / Clarification 2 of 3              theorem.tex:118-126
 Best current guess · Confidence: High · interpretation only
 ┌ Source context ─────────────────┬ Diagnosis & options ─────────────────┐
 │ 118 theorem ...                 │ Why verification stopped             │
 │ 119 ...                         │ Best current guess [E-...]           │
 │                                 │ Model and effort provenance          │
 └─────────────────────────────────┴──────────────────────────────────────┘
 [Previous] [Next]                              [Check manuscript changes]
```

### Project-catalog reference layout

- Wide: project roster and selected-project inspector.
- Compact: full-width roster; Enter opens detail and returns to the selected
  project.
- **New project** is always visible. Resume/repair/report actions live in the
  inspector. Delete is visually and spatially separated and remains
  cancel-first.

### New-project reference layout

- Four focused steps: **Source**, **Destination**, **Verification**, **Review**.
- The step indicator and Back/Continue remain visible.
- Folder browsing and main-file selection reuse bounded list/detail controls.
- The ordinary Verification step offers the default task first. Custom task
  editing opens a full-height editor instead of adding a large text area below
  the rest of the form.
- Going back preserves entered values; project creation occurs only from the
  final Review step after the source inventory is still current.

### Variants and states

Every reusable editor supports:

- loading;
- ready/clean;
- dirty;
- invalid row/field;
- inherited/read-only;
- unavailable provider;
- save in progress;
- saved;
- stale revision/conflict.

### Token/component ownership

- Palette and semantic color tokens remain in `tui/theme.py`.
- Shared layout and component geometry belongs in a dedicated TUI component or
  stylesheet surface, not scattered screen-specific selectors.
- Backend policy, validation, provider catalogs, persistence, and authority
  remain outside Textual screens.

## Accessibility

- Target standard: keyboard-complete, high-contrast, non-color-dependent,
  screen-reader-conscious terminal interaction within Textual's capabilities.
- Keyboard/focus behavior:
  - initial focus lands on the primary workspace or the action needed to
    remediate an unavailable state;
  - `Up/Down` moves through navigation and rows;
  - `Enter` selects or edits;
  - `Left/Right` changes peer tabs or scope only when that region is focused;
  - `Tab` moves between major interactive regions, not explanatory prose;
  - `Esc` closes detail, then returns one level;
  - `Ctrl+S` saves the visible draft and visible scope;
  - focus and row selection survive refresh and resize.
- Contrast/readability:
  - retain semantic light/dark theme pairs;
  - use text labels in addition to color and glyphs;
  - wrap prose and paths; preserve exact source/code whitespace in dedicated
    panes.
- Screen-reader semantics:
  - controls have human-facing labels and purpose text;
  - decorative separators are not focus stops;
  - status changes are repeated in the persistent textual status area.
- Reduced motion and sensory considerations:
  - do not use flashing, decorative spinners, or rapid color changes;
  - progress updates should replace stable regions rather than reflow the whole
    screen.

## Responsive behavior

### Supported viewport classes

- Minimum supported: **80x24**
- Standard: **120x40**
- Wide: **140x48 and above**

Below 80x24, show a concise resize-needed view with the current safe command
and state; do not squeeze controls into unusable geometry.

### Layout adaptations

- Wide (`>=140` columns and `>=40` rows): category rail plus workspace; the
  workspace may use master/inspector.
- Standard (`120–139` columns and `>=32` rows): horizontal peer categories and
  one full-width workspace; roster/detail replace one another.
- Compact (`80–119` columns or short height): category page, focused
  destination, roster, and detail replace one another sequentially.
- Very short terminals: keep header/action bar, reduce secondary summary, and
  give remaining height to the primary workspace.
- Forms align label and control columns when wide and stack each label above its
  control when compact.
- Horizontal toolbars collapse to the primary action plus keyboard-advertised
  secondary actions, or stack vertically when all actions must remain visible.

Textual breakpoint mechanics are explicit:

- `HORIZONTAL_BREAKPOINTS = [(0, "-h-under-min"), (80, "-h-compact"),
  (120, "-h-standard"), (140, "-h-wide")]`;
- `VERTICAL_BREAKPOINTS = [(0, "-v-under-min"), (24, "-v-compact"),
  (32, "-v-standard"), (40, "-v-wide")]`.

A pure `composition_for(width, height)` function derives exactly one additional
root class, applied on mount and resize after removing the previous composition
class:

1. `resize-needed` if `width < 80` or `height < 24`;
2. `wide` if `width >= 140` and `height >= 40`;
3. `standard` if `120 <= width < 140` and `height >= 32`;
4. `compact` if `80 <= width < 120` and `height >= 24`;
5. `compact-short` otherwise.

TCSS selects layout only through the mutually exclusive derived composition
class, avoiding ambiguous precedence between independent horizontal/vertical
classes. Tests also assert Textual applied the expected axis classes. Boundary
coverage is mandatory at widths 79/80, 119/120, 139/140 and heights 23/24,
31/32, 39/40, including 120x32 and 140x40.

### Viewport composition truth table

Width and height requirements are conjunctive. A viewport below either minimum
shows resize-needed; wide requires both wide width and its height floor; a
standard-width or wide-width terminal below the standard/wide height floor uses
the compact sequential composition rather than squeezing the richer layout.

| Viewport | Composition | Settings navigation | Role workspace |
| --- | --- | --- | --- |
| 79x24 | Resize-needed | Safe status and exit/global commands only | No editable roster |
| 80x23 | Resize-needed | Safe status and exit/global commands only | No editable roster |
| 80x24 | Compact | Category -> destination drill-down | Roster/detail replace each other |
| 100x32 | Compact | Category -> destination drill-down | Roster/detail replace each other |
| 120x31 | Compact-short | Sequential composition; fixed action bar | Roster/detail replace each other |
| 120x40 | Standard | Horizontal peer categories | Full-width eight-row roster |
| 140x39 | Compact-short | Sequential composition; fixed action bar | Roster/detail replace each other |
| 140x48 | Wide | Category rail + workspace | Roster + inspector side by side |

### Geometry budgets

- Header/breadcrumb/context: 2–4 rows.
- Compact status summary before the task: at most 4 rows.
- Persistent action/status bar: 2–4 rows.
- Primary workspace: all remaining rows, `1fr`, with a meaningful minimum.
- Modal: at most 92% of the viewport; only the body scrolls; actions stay fixed.
- Read-only summary panels: `height:auto` with a small `max-height`, never a
  large unconditional fixed height.

### Scrolling and wrapping

- Exactly one primary vertical scroll owner per view.
- A source/detail pane may have its own scroll only when the outer view does not
  scroll.
- `soft_wrap=True`: prose, paths, descriptions, status, and diagnostics.
- `soft_wrap=False`: source code, logs where columns matter, commands, and
  machine-readable evidence.
- Long lists live in a bounded `1fr` list/table with fixed headers, not in an
  auto-height container.

### Mouse/touch differences

- Mouse selection is supported but never required.
- Make entire semantic list rows selectable when the row represents navigation.
- Destructive actions stay separated from ordinary row activation.

## Screen geometry and redesign map

| Surface | Primary workspace | Growth risk | Target structure | Priority |
|---|---|---|---|---|
| Help | searchable command/shortcut list | commands and descriptions | bounded list/detail; fixed Close | medium |
| Main menu / project catalog | project roster | project count, long paths, many row buttons | project list/detail; actions in inspector | high |
| Project deletion | consequence detail | long project path/diagnostic | scrollable body; fixed Cancel/Delete | medium |
| Deletion outcome | result and recovery location | long paths | concise result with copyable details | low |
| Folder picker | folder table | directory size, long names/paths | retain flexible table; responsive controls | low |
| New project | current setup step | paths and custom task | Source → Destination → Verification → Review wizard | high |
| Main-file selection | candidate roster | candidate count and paths | bounded list/detail; no growing `RadioSet` | medium |
| Project review | final choices | long source closure/task | categorized summary; fixed Create/Back | medium |
| Existing-project repair | main-file candidates | candidate count and ambiguity text | same reusable candidate selector | medium |
| Destination conflict | conflict explanation | diagnostics and paths | focused resolution choices; fixed actions | low |
| Dashboard | project state and next actions | input-file count, long paths | compact status plus action list; detail tab | medium |
| Live progress | stage rail and event stream | continuous events, warnings, source list | fixed progress header/stages; flexible log; fixed Detach/Cancel | critical |
| Clarification | source context and requested decision | long source/question/resolutions | source/request master-detail; fixed Previous/Next/Check | critical |
| Change review | affected changes | file, claim, certificate, question counts | summary counts plus categorized tabs/list-detail | high |
| Findings | finding roster and detail | finding count and long explanations | categorized roster/detail; fixed report actions | high |
| Failure dependency | proof graph and detail | graph size and long reasons | retain tabbed graph/detail; consistency polish | low |
| Report viewer | rendered report or exact source | report length | retain full-height tabs; consistency polish | low |
| Recovery | blocking reason and safe next action | cancellation/failure report length | concise diagnosis; detail tab; fixed recovery actions | high |
| Settings home | settings categories | category count | stable navigator plus compact current-state summary | high |
| Verification AI roles | eight-role matrix and inspector | model labels and role explanations | master/detail roster; fixed draft actions | critical |
| Provider connections | provider roster and inspector | catalogs and diagnostics | contextual list/detail; progressive diagnostics | critical |
| Runtime limits | editable limit policy | controls and explanations | controls first; focused policy page | critical |
| Resource diagnostics | telemetry and benchmark output | live metrics and long output | separate overview/calibration views | high |
| Advanced / legacy | uncommon compatibility values | diagnostic explanations | progressive disclosure outside primary path | medium |
| Install/account/warning modals | decision and consequence | command plans and warnings | scrollable body; fixed actions | high |
| First-run AI onboarding | provider choice, connection, team review | diagnostics, catalogs, role names | three focused gated steps; fixed Back/Continue/Exit | critical |

## Interaction states

- Loading: preserve layout and label what is being loaded; do not insert a new
  block that shifts the primary controls.
- Empty: explain why the collection is empty and show the available next action
  in the workspace.
- Error: identify the affected object/row, keep valid state visible, and move
  focus to the remediable item.
- Retryable verification failure: keep exact failure evidence visible and show
  **Retry verification** as the primary action only when the backend marks at
  least one incident retryable. Non-retryable proof failures keep it disabled
  and identify `Retryable: no` in the exact-reason view.
- Success: update the authoritative current-value display and show a concise
  status message without navigating unexpectedly.
- Disabled: retain the control only when it teaches availability; pair it with
  a visible textual reason. Otherwise omit it.
- Dirty: display scope and change count persistently. `Esc` and navigation
  away from the draft's destination require Save, Discard, or Continue editing;
  returning from an inspector to its roster preserves the same draft silently.
- Conflict/stale revision: preserve the draft, explain the newer authoritative
  state, and offer reload/compare rather than silently overwriting.
- Offline/slow provider: show last known sanitized status and an explicit
  Recheck action; do not block unrelated settings navigation.

## Content voice

- Tone: precise, calm, direct, and non-accusatory.
- Terminology:
  - use `provider` in the UI, not `driver`, except in exact technical detail;
  - use `reasoning effort` or the short human labels Low, Medium, High, and
    Extra high;
  - show exact values such as `xhigh` and exact model IDs in the inspector or
    Advanced view;
  - call the eight assignments the **verification team** or **role
    assignments**;
  - label reserved roles as **Configured — not currently dispatched**, rather
    than implying active use.
- Microcopy rules:
  - buttons use verbs and state their object: **Save role assignments**,
    **Recheck provider**, **Customize this project**;
  - headings use nouns: **Role assignments**, **Provider connections**;
  - warnings state consequence before mechanism;
  - avoid paragraphs when a status/value row is clearer;
  - never use `verified`, `proved`, or `certified` for model-only output.

## Implementation constraints

- Framework/styling system: Python 3.13; first land a behavior-preserving
  compatibility upgrade to Textual 8.2.8 (`textual>=8.2.8,<9`) and Rich 14.2
  (`rich>=14.2,<15`), then begin redesign using Textual 8 native horizontal and
  vertical breakpoints. Preserve the current backend/TUI boundary.
- Development/QC tooling: `textual-dev>=1.8,<2` and
  `pytest-textual-snapshot==1.1.0` are development dependencies, not runtime UI
  authority.
- Reproducible dependency mechanism: pin build tools in
  `requirements/py313-build.txt` (`setuptools==80.9.0`, `wheel==0.45.1`),
  generate a hash-locked `requirements/py313-build.lock`, then generate/commit
  `requirements/py313-dev.lock` with both `pyproject.toml` and the build pins as
  inputs:
  `"$uv012" pip compile pyproject.toml requirements/py313-build.txt --extra dev
  --python-version 3.13 --universal --generate-hashes -o
  requirements/py313-dev.lock`. A clean unseeded venv first installs exact
  setuptools/wheel wheels from the hash-locked build lock with
  `--only-binary=:all:`, then hash-syncs the full lock with
  `--no-build-isolation`; this lets the
  pinned setuptools/wheel build the hash-verified `pylatexenc` sdist without an
  untracked isolated build environment. The exact uv 0.12.0 command surface has been
  checked to support `--universal`, `--generate-hashes`, `--require-hashes`, and
  `--no-build-isolation`.
- Pinned uv bootstrap: do not run the remote installer. Select the official uv
  0.12.0 release artifact by `uname`: `uv-x86_64-unknown-linux-gnu.tar.gz` for
  Linux x86_64, `uv-aarch64-apple-darwin.tar.gz` for macOS arm64, or
  `uv-x86_64-apple-darwin.tar.gz` for macOS x86_64. Verify against committed
  `requirements/uv-0.12.0-sha256.txt`, containing the official digests
  `eaf842262aa1c418d8ecc5605f02ee1ebfd369124fa48548e85f9481a47831a9`,
  `2b9e582af54f84fa50c115427451a6c13e80f43b52f8282b8af5791077317bbf`,
  and `d41593beaefc54bab7d062af0ef6ca093bfb81d001d58ebbef39e44423f9c496`
  for those three artifacts respectively. Calculate SHA-256 with
  `sha256sum` on Linux or `shasum -a 256` on macOS, compare exact digests, and
  extract only into a task-specific temporary directory. Set
  `UV_NO_MODIFY_PATH=1`; a raw artifact installs no updater metadata and no
  updater is invoked. Hash every existing `.bash_profile`, `.bashrc`, `.profile`,
  `.zprofile`, and `.zshrc` before/after bootstrap and require byte-identical
  files. Reject ambient uv, including this host's 0.12.7.
- Required jobs create a clean Python 3.13 venv, install
  `requirements/py313-build.lock` with `pip install --require-hashes
  --only-binary=:all:`, then run `"$uv012" pip sync --require-hashes
  --strict --no-build-isolation requirements/py313-dev.lock`,
  and install the checkout with `"$uv012" pip install --no-deps
  --no-build-isolation -e .`. Test commands use direct `.venv/bin/*` tools or
  `uv run --no-sync`; ordinary `uv run` is forbidden after the immutable gate.
  Capture sorted `uv pip freeze` before and after all checks and require exact
  equality; upload the reviewed resolution and Textual/Rich versions.
- Dependency freshness: a separate scheduled/manual latest-within-major canary
  installs `.[dev]` without the lock but within the declared `<9`, `<15`, and
  `<2` bounds, runs the same TUI/snapshot gate, and uploads its resolved versions
  and SVG diffs. Canary drift does not silently rewrite the reviewed lock.
- Active local deployment: after clean CI passes, separately review the dry-run
  resolution for `/data1/homes/vui1/.venvs/proof-assistant`. That environment
  contains editable `/data1/homes/vui1/src/repoprover`; never run `pip sync`
  against it. Compile a temporary hash-locked union of Proof Assistant dev
  dependencies, exact build pins, and the dependencies declared by the current
  local RepoProver checkout. Diff it against the complete active freeze and
  create a hash-locked file containing only direct/changed third-party packages;
  first bootstrap `requirements/py313-build.lock` into the active interpreter
  with `uv pip install --require-hashes --only-binary=:all:`. Only after those
  build requirements are present, install the reviewed changes file with
  `uv pip install --require-hashes
  --no-build-isolation` so extras are preserved, then reinstall
  both local projects editable with `--no-deps --no-build-isolation`. Require
  `"$uv012" pip check`; verify both projects' import paths, RepoProver Git commit/
  dirty state, and dependency resolution before and after; then launch the
  installed TUI to inspect Settings. This deployment gate
  is not evidence for clean CI and clean CI is not evidence for the installed
  local environment.
- Design-token constraints: extend existing semantic theme tokens and shared
  layout classes; do not add literal per-screen colors or a parallel theme.
- Performance constraints:
  - do not rebuild large trees/tables on every status tick when rows can be
    updated in place;
  - async refresh must preserve selection and draft state;
  - keep provider checks off the UI thread.
- Compatibility constraints:
  - Linux and macOS terminals;
  - SSH and keyboard-only use;
  - light and dark themes;
  - no dependency on a graphical browser or desktop keyring UI for ordinary
    navigation.
- Behavior and security preservation gates:
  - API credentials remain one-shot secret submissions, are cleared from input
    immediately, and never remain in the Textual DOM, screen repr, settings
    DTOs, project files, or logs;
  - provider installation requires review of the exact plan and separate
    cancel-first confirmation before the backend executes it;
  - the Copilot account probe sends no request without its own explicit,
    cancel-first confirmation;
  - destructive and unsafe-setting dialogs focus Cancel first;
  - all machine and project saves retain revision-conflict protection;
  - closing Settings restores the exact originating screen and observer state;
  - global navigation from live verification detaches the TUI observer and does
    not cancel backend work;
  - the TUI imports typed view models and sends commands but never takes over
    backend provider, project, verification, persistence, or credential
    authority;
  - first-run readiness enforcement cannot be bypassed by global navigation.
- Test/screenshot expectations:
  - exercise the full truth table above, including 79x24, 80x23, 120x31, and
    140x39 boundary compositions;
  - use maximum-length paths, provider/model names, eight roles, many projects,
    many findings, long source excerpts, and long failure text;
  - assert primary widgets have positive geometry and lie within the viewport;
  - assert the primary action/status bar remains visible;
  - assert every interactive control is keyboard reachable in deterministic
    order;
  - assert resize preserves selected item, active scope/tab, focus target, and
    unsaved draft;
  - at 120x40, assert all eight stable role row keys and their Role, Model,
    Effort, and State cells have positive, in-viewport regions;
  - assert a long model display name may truncate only its model cell while the
    inspector exposes its exact copyable ID;
  - assert no nested-scroll trap is required to reach an action;
  - retain behavior/integration tests for backend requests and revision checks.

## Implementation and quality-control record

The redesign is implemented as one shared system rather than a collection of
screen-local exceptions:

1. Textual 8.2.8, Rich 14, textual-dev 1.8, and the snapshot tooling are pinned
   by reviewed, hash-locked Python 3.13 resolutions. Linux and macOS CI install
   those exact files; a scheduled latest-within-major canary catches drift.
2. `ResponsivePage`, `PageWorkspace`, `ActionBar`, roster/detail components,
   and modal-body patterns provide the common geometry. Boundary, production,
   keyboard, and SVG snapshot tests exercise the shared contract.
3. **Verification AI** now separates role assignments, provider connections,
   and diagnostics. Machine and project scopes retain distinct drafts,
   revision-checked saves, provider defaults, and one-level Undo.
4. First run is the gated **Choose provider → Connect provider → Review team**
   flow. Global navigation, transient secrets, explicit account checks, and
   reviewed installation commands retain their safety boundaries.
5. **Runtime & resources** is split into policy, overview, and calibration.
6. **Live progress** and **Clarification** use one flexible workspace, compact
   peer views, wide split views, and fixed actions.
7. Catalog, project creation, review, findings, recovery, reports, and the
   remaining workflow screens use the same bounded-workspace/fixed-action
   hierarchy, with list/detail or focused steps where content can grow.
8. Background settings loads are generation-checked. Out-of-order provider,
   role-policy, and project-policy results cannot overwrite newer reads,
   mutations, or unsaved edits. Credential-store mutations are serialized.
9. Closing Settings restores the exact originating screen; background workflow
   results wait behind the overlay and appear only after it closes.
10. Release quality gates are Ruff, the typing-policy check, strict mypy, the
    complete pytest and snapshot suites, textual-dev diagnostics/smoke, locked
    dependency immutability, and Linux/macOS CI. Snapshot capture removes the
    ambient `NO_COLOR` variable so local shell preferences cannot contaminate
    the portable SVG baseline.
11. Local deployment uses the same reviewed lock without syncing away the
    editable RepoProver install, followed by `uv pip check`, compiler, import,
    executable, and Settings smoke checks. Shell startup files must remain
    byte-for-byte unchanged during that deployment.

## Review checklist

A TUI change is not ready if any answer is no:

- Is the current project/scope/state visible without scrolling?
- Is the screen about one subject and one primary task?
- Is the primary workspace given the remaining flexible height?
- Is the primary action visible at 80x24?
- Is there exactly one primary vertical scroll owner?
- Are long/dynamic collections bounded in a list, table, tree, or source pane?
- Can a user recognize all relevant peer items without cycling a selector?
- Does `Esc` move back one conceptual level and protect dirty work?
- Does `Ctrl+S` save only the currently displayed scope/editor?
- Are focus, status, warning, inheritance, and readiness conveyed with text as
  well as color?
- Do compact and wide layouts preserve the same capabilities?
- Do tests assert visible geometry and keyboard reachability with worst-case
  content?
- Does first-run onboarding still require one ready, explicitly saved primary
  provider before the main menu becomes available?
- Are credentials absent from the DOM/repr/logs after one-shot submission?
- Are installation, account probes, destructive actions, and unsafe settings
  still separately reviewed and cancel-first?
- Does leaving live progress detach observation without sending cancellation?

## Open questions

- [ ] Confirm whether the last selected settings category should be persisted
  across application restarts or only during the current session. Project scope
  remains determined by the screen from which Settings was opened.
- [ ] Decide whether the product should reject terminals smaller than 80x24 or
  provide a limited command-only fallback view.
- [ ] Validate the role-stage labels and one-line purpose text with RepoProver's
  long-term dispatch roadmap before treating them as stable UI terminology.
- [ ] Determine which terminal/screen-reader combinations are part of the
  supported accessibility test matrix.
