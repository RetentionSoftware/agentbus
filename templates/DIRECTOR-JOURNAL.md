# Director journal

<!--
Template from agentbus (see docs/director/README.md). Put it at docs/director/JOURNAL.md. The director reads "Now"
first after every context compaction, restart or pause, then the roadmap, then `agentbus board`. Keep "Now" short
and current: it is a resume point, not a history. History goes into dated entries. Delete this comment.
-->

> The director's memory between context compactions. In a new session read "Now" first, then
> `docs/director/ROADMAP.md`, then `agentbus board`.

## Now

- **State:** <working / paused since YYYY-MM-DD HH:MM, by whose decision>. The bus: `<how to start it>` (port
  {{PORT}}).
- **Standing rules from the user:**
  - workers: at most <N> at once;
  - model choice: <hard → ..., moderate → ..., trivial → ...>; never above <model>; review weaker models strictly;
  - after a merge: archive the agent's session, remove its worktree and branch without asking;
  - <what the director may do alone, what needs the user's word: starting tasks, measurements, system settings>.
- **Tasks in flight** (one item each):
  - **T-xxx** (`<agent>`, <model>, branch `<branch>`, commit `<sha>`): <done so far>; <what comes first after the
    pause>; <what the director owes: review #NNN, merge>.
- **Waiting for the user:** <decisions, launches, checks only the user can do>.
- **Loose ends:** <flaky tests with data, follow-ups, small fixes>.

## How I work (short reminder)

- Spec first, committed to {{MAIN_BRANCH}}; plan review before code; intermediate review at architectural points.
- Every bus message the agent must act on → a direct wake-up message to its session.
- Review in a clean worktree at the exact commit, full matrix, independent read for hard code, verify every finding,
  mutation for every fix.
- Merge only when the merged tree equals the verified tree; then clean up.

## Entries

### YYYY-MM-DD

- <what happened, what was decided and why, model chosen for a launch (before the launch), review verdicts with
  their numbered findings, merges with commits, mistakes of the director with the rule that prevents them>
