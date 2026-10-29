# Director's review checklist

<!--
Template from agentbus (see docs/director/README.md, "Review"). Put it at docs/director/REVIEW.md and replace the
{{...}} placeholders and the <...> lists with your project's commands and rules. Delete this comment.
-->

No item is skipped "because the agent said it checked". The verdict goes to the agent as a `decision` with numbered
findings, most severe first.

## 1. Build and tests — myself, not from the report

- [ ] A clean worktree with the branch merged into the current {{MAIN_BRANCH}}:
      `git worktree add --detach .claude/worktrees/review-T-xxx {{MAIN_BRANCH}}`, then
      `git merge --no-ff --no-edit <branch>` in it. Never the agent's own working copy.
- [ ] The full matrix: <every compiler × configuration / platform × runtime>, with tests: {{TEST_COMMAND}}. Logs kept.
- [ ] Zero warnings; formatting and lint clean.
- [ ] Tests with time, threads, IO or shared resources: a stress run under load, the numbers in the agent's log; the
      risky ones repeated by me.

## 2. Matches the task

- [ ] Every acceptance criterion is met in substance, not formally.
- [ ] No work outside the scope, no edits of other paths without a claim or a decision.
- [ ] Public contracts match the spec; deviations are agreed and recorded (a `decision`, an ADR).

## 3. Code quality — I read the diff

- [ ] The public API and its documentation: what, contract, thread safety, errors.
- [ ] Hard code (synchronization, memory, parsers, concurrency) had an independent read: a subagent got the code and
      the risks, not my conclusions; I verified every finding against the code before forwarding it.
- [ ] No workarounds: disabled tests or warnings, silent fallbacks, unexplained constants, TODOs without a task.
- [ ] Tests check behaviour and edge cases, and **fail against broken code**: every fix has a test that was red before
      it; key mechanisms were broken on purpose (mutation) and the tests went red.
- [ ] Tests that rely on a validator, sanitizer or linter exercise something that tool can actually see.
- [ ] Tests wait for conditions, not time; measure only their own objects; do not depend on scheduling.
- [ ] Decisions look ahead: nothing the next milestone will have to throw away.

## 4. Records

- [ ] The agent's progress log is current: final state, known limitations, measurements.
- [ ] Decisions that matter to others are in the bus (`decision`) or in ADR text for me.

## 5. Merge and clean-up

- [ ] `git merge --no-ff <branch>` into {{MAIN_BRANCH}} with `Merge T-xxx: <title>`; the merged tree equals the tree I
      verified (`git rev-parse HEAD^{tree}`).
- [ ] `task_update(agent="director", task_id="T-xxx", state="done")`; journal entry; roadmap status; root README if
      the task added a module.
- [ ] The agent's session archived; its worktree and branch removed; the review worktree removed.
