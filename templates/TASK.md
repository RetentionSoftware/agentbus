# T-xxx: <title>

<!--
Template from agentbus (see docs/director/README.md, "The life of a task"). Put it at {{TASKS_DIR}}/_TEMPLATE.md,
copy it for every task as {{TASKS_DIR}}/T-xxx-<slug>.md and replace everything in <...>. Commit the task to
{{MAIN_BRANCH}} before starting the agent. Delete this comment.
-->

| | |
|---|---|
| **Agent** | `<agent-name>` |
| **Wave** | <milestone; after which tasks> |
| **Model** | <model and effort, with the reason: why this task needs it> |
| **Depends on** | <T-yyy (merged, commit), ... or "—"> |
| **Must read** | `{{PROTOCOL_PATH}}`; <ADRs, module READMEs, headers, progress logs of earlier tasks that this task builds on> |
| **Research / background** | <links to design documents, sections> |

## Goal

<What must exist after this task and why, in 3–6 sentences. Name the downstream tasks that will build on it.>

## Your paths (owned)

- `<path/**>` (new module; the name may be proposed differently)
- `{{PROGRESS_DIR}}/T-xxx.md`

<Who else works in parallel and on which paths. Public APIs of merged tasks are contracts: only compatible additions,
through `contract_change`. Shared files (<list: dependency manifest, build presets, lint config, root README, ADRs,
tooling>) are edited by the director. New dependencies only through `contract_change` with: why, licence,
alternatives.>

## Scope

### 1. <the main part — usually the API or the data model: "the main decision of the plan">

- <what to design, with the constraints that matter>
- <what to compare and measure before choosing>

### 2. <next part>

- <...>

## Out of scope

- <what is deliberately not done here, and which task does it>

## Tests

- <behaviour, edge cases, error paths — each with what proves it>
- <mutation: which test must fail when the key mechanism is broken>
- <no-allocation / determinism / concurrency tests, if relevant>
- <tests involving time, threads, IO: wait for conditions, not time; measure only your own objects; stress-run
  before hand-in>

## Measurements

Method and numbers go into the progress log:
- <what to measure, on which sizes, against which alternative>

## Acceptance criteria

- [ ] The plan is approved by the director before implementation. The plan covers:
  - <the main decision, with a prototype or measurement where the choice is not obvious>;
  - <the other parts>;
  - <how downstream tasks will use the result>;
  - <dependencies and `contract_change`s needed>.
- [ ] Intermediate review after <architectural point>.
- [ ] All tests green: {{TEST_COMMAND}} on <every compiler × configuration / platform>; zero warnings; formatting and
  lint clean.
- [ ] Stress-run of the affected tests.
- [ ] Measurements in the progress log; module README; decisions worth an ADR written as ADR text for the director.

## Director's notes

- <the traps you expect, the places where errors are not visible at once, what to rely on instead of "looks right">
- <decisions already taken with the user that the agent must not reopen>
