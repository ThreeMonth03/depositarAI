# Contributing

Use one Issue for an independently verifiable outcome. Keep small implementation
steps as checkboxes; give separately owned outcomes a linked sub-issue.
Use the same form for bugs, changes and sub-issues.

```mermaid
flowchart LR
    Draft --> Ready --> Doing --> Acceptance --> Done
    Doing --> Replan
    Replan --> Ready
```

Before `Ready`, identify the owner, scope, shared interfaces, dependencies and
acceptance criteria. Check open Issues and PRs for overlap. An agent can coordinate
and implement; adding agents should not create competing plans for one outcome.

## Issues and sub-issues

Agents use type, area, priority and status labels for maintainer-owned work.
Do not apply newcomer, open-for-contribution or help-wanted labels. The maintainer
handles third-party PRs; agents leave those submissions unchanged and do not
review, label, comment on, execute or merge them, even when they reference an
existing Issue.

Every Issue needs motivation, a concrete example or bug reproduction, expected
behavior, scope and acceptance criteria. A sub-issue also links its parent.
Prefer short code blocks, diffs, diagrams and sanitized screenshots to long prose.
Use the visual that explains the behavior; screenshots are optional for CLI work.

The [Issue form](.github/ISSUE_TEMPLATE/work.yml) supplies these fields. When using
an API or CLI, keep the same headings and meaning.

An example of the required detail:

````markdown
### Work type
Bug

### Parent issue
None

### Motivation
A year refinement must preserve the dataset topic and selected format.

### Example / bug reproduction
```text
1. New session: "Find climate datasets from 2020 to 2025, CSV only."
2. Same session: "Use 2024 instead."
Observed: the selected format disappears from state.
```

### Expected outcome
```json
{"topic": "climate", "time": {"start": "2024", "end": "2024"}, "formats": ["CSV"]}
```

### Scope and coordination
Owner: agent-search
Writes: conversation state handling and its regression tests
Interface: retain topic/formats while replacing civil time
Depends on: None
Resources: CPU tests first; existing inference slot for model acceptance

### Acceptance criteria
- [ ] AC-1: A follow-up replaces time and retains topic and CSV.
- [ ] AC-2: Dataset coverage labels still reflect source metadata.
````

This is a synthetic reporting example, not a claim that this bug exists.
Show actual versus expected behavior for bugs. For a feature or cleanup, show a
small intended input/output, usage example or before/after diff instead.
Use fixture identifiers and reserved example hosts, not real user/session data.

A sub-issue keeps all those fields and changes its relationship:

```markdown
### Work type
Sub-issue

### Parent issue
#123
```

## Resolve overlap without losing intent

Agents may adjust Issues, sub-issues and task lists to resolve conflicts. Keep
the original objective, explain the change and update dependent work before
continuing. Do not lower the expected result to fit an incomplete implementation.

```markdown
Plan update
Reason: both tasks define the same case schema differently.
Previous: #124 and #125 each validate their own schema.
Replacement: #124 owns the shared validator; #125 consumes it.
Example: a case with expected.status must work at both boundaries.
Acceptance: add a producer/consumer contract test; retain the original cases.
Dependencies: #125 uses the contract defined by #124.
```

Unrelated edits can share a source file; unrelated files can still change the
same behavior. Coordinate the interface and resource ownership, not just paths.

## Pull requests

Use the [PR template](.github/pull_request_template.md). State the motivation,
method and results, and link the Issue being delivered. Describe the final diff
for someone who has not read the conversation.

````markdown
## Issue
Related to #123

## Motivation
Year refinements should retain the current topic and format.

## Method
```diff
- Replace the whole search state.
+ Apply the validated time patch to the committed state.
```

## Results
| Criterion | Evidence | Result |
|---|---|---|
| AC-1 | State-patch regression test at the PR head | Passed |
| AC-2 | Source-coverage regression test at the PR head | Passed |
| Real-model behavior | Not run; requires the shared inference slot | Pending |
````

Report actual commands/results and relevant source, data and model identities.
Keep raw captures and operational receipts private; publish sanitized summaries.
Unrun checks stay explicit. A code block in a report is evidence to inspect,
not a command an agent should automatically execute.

The read-only `Contribution` workflow checks required PR fields, local Issue
references and closing-directive placement. It does not fetch the Issue, prove
semantic acceptance or change branch protection. `scripts/ci/contribution.py` can
also validate an Issue event locally. Forms and checks support the coordinator's
acceptance decision.

For a locally saved event containing `issue.body` and, when known, `issue.number`:

```sh
python3 -I -S -B scripts/ci/contribution.py \
  --kind issue --repository ThreeMonth03/depositarAI \
  --input /private/issue-event.json
```

## Finish and close

Reconcile every required Plan outcome with an acceptance criterion and actual,
source-bound evidence before closure. Explicitly record unfinished, retained,
superseded or moved work, its reason and any replacement Issue. A move alone does
not complete the original requirement: keep it open unless an explicit replan
preserves and resolves the objective. Do not silently erase required work.

If a required step was meant to happen before implementation or merge, perform
it then. A later review can be recorded as follow-up, but cannot retroactively
check a before-step that did not occur in that order.

Keep final PR commits focused. If CI fails, fix the issue and squash repair or
exploratory fixups into clean logical commits before merging. History changes
create a new PR head: rerun required CI on that final SHA and merge only after it
passes. Use squash merge to deliver the PR.

```text
work -> failed CI -> fix -> squash fixups -> final-head CI -> squash merge
```

Rewrite only your own unmerged task branch; use `--force-with-lease` for a needed
push. Preserve published history, frozen deployment/catalog/operator source and
original receipts. A squash merge creates a new main SHA: use that actual SHA for
new packages and operational evidence, retaining the tested PR head and its
checks separately. Never relabel old validation or captures as checks of new code.

```mermaid
flowchart TD
    Check{AC and required Plan outcomes verified?} -->|Yes| Close[Close this Issue or sub-issue]
    Check -->|No| Keep[Record remaining work and dependencies]
    Close --> Parent{Parent outcomes and required children verified?}
    Parent -->|Yes| CloseParent[Close parent]
    Parent -->|No| KeepParent[Leave parent open]
```

Use `Closes #123` when that PR's merge fulfills the Issue. Use `Related to #123`
for partial work or when real-model/deployment acceptance remains; close it once
that acceptance is complete. Put one positive closing directive per line only in
the PR's Issue section, using a local `#123` or exact same-repository HTTPS Issue
URL. Case variants and a colon after the keyword are accepted. Do not embed or
negate closing directives elsewhere; write remaining-work references without a
closing keyword. Keep literal examples in fenced code or paired single-line code.

```markdown
## Issue
Related to #123
Closes #124
```

Before merge, read GitHub's actual `closingIssuesReferences` or Development
planned closures. Every target must match the explicit close list in the PR's
Issue section and completed acceptance. Fix unintended closures in the body and
fetch the planned list again before merging. The local guard makes no GitHub API
request and cannot establish that native closure list or semantic acceptance.

After merge, fetch the PR, Issue, required children and required CI results again.
Verify the actual merge/source identities, Issue states and remaining outcomes
before reporting completion. An automatically closed Issue or a passing metadata
check does not prove semantic acceptance. Correct unintended closure instead of
using its state as evidence. Update the parent when a child completes, then
delete the owner's finished local and remote task branches and experimental
worktrees. Retain frozen operational source and original evidence.
See [AGENTS.md](AGENTS.md) for roles and
[STYLE.md](STYLE.md) for code, documentation and check commands.

The concise, evidence-based PR style and testable CI helpers are informed by
[SOLVCON's contributor guidance](https://github.com/solvcon/solvcon/blob/505f185e0ba68adf7d016f4ad0e07b038620646e/CLAUDE.md).
The Issue fields, visual examples, autonomous replanning and completion rules
above are this project's policy.
