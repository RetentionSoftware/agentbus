# The director playbook

[Русская версия](README.ru.md)

How a director session runs a team of Claude agents on agentbus, written down from a project where it worked well:
a C++ game engine built by up to two parallel Opus/Sonnet agents for weeks, with one director chat planning,
reviewing and merging. The bus is only the transport. What keeps the team out of chaos is the routine around it,
and that routine lives in files, not in anyone's memory.

The [README](../../README.md#working-as-a-team) explains the tools. This document explains the routine.

## Contents

- [Principles](#principles)
- [The files that hold the process](#the-files-that-hold-the-process)
- [The life of a task](#the-life-of-a-task)
- [Communication: the bus plus a wake-up](#communication-the-bus-plus-a-wake-up)
- [Review](#review)
- [Merge and clean-up](#merge-and-clean-up)
- [Pause and resume](#pause-and-resume)
- [Working while the user is away](#working-while-the-user-is-away)
- [Rules of discipline](#rules-of-discipline)
- [Why teams turn into a mess](#why-teams-turn-into-a-mess)
- [Starting a new project: a checklist](#starting-a-new-project-a-checklist)

## Principles

1. **The process lives in the repository.** The director reads the protocol, its journal and the task file every
   time instead of trusting what it remembers: context gets compacted, sessions restart, the user pauses for days.
2. **The bus carries the substance; a direct message only wakes the agent.** An idle session reads nothing.
3. **Nothing is "done" because an agent said so.** The director builds, runs and reads everything itself, in a clean
   checkout.
4. **Few workers, strict review.** Two parallel agents and a careful director beat five agents and a director who
   only skims. Review is the bottleneck, so the number of workers is set by it.
5. **Decisions that belong to the user stay with the user**: starting tasks, the number of workers and the model,
   anything that changes the user's machine or spends money.

## The files that hold the process

| File | Who writes it | What it is for |
|---|---|---|
| `docs/agents/PROTOCOL.md` | director | rules for every agent: start checklist, quality bar, git, progress log, claims, messages, hand-in ([template](../../templates/PROTOCOL.md)) |
| `docs/agents/KICKOFF.md` | director | the one kickoff message, identical for every agent except the task ([template](../../templates/KICKOFF.md)) |
| `docs/tasks/T-xxx-*.md` | director | the task specification ([template](../../templates/TASK.md)) |
| `docs/progress/T-xxx.md` | agent | the agent's progress log: state, plan, decisions, findings, the next step. Survives context compaction |
| `docs/director/JOURNAL.md` | director | the director's memory: a **"Now"** section that is the resume point, then dated entries ([template](../../templates/DIRECTOR-JOURNAL.md)) |
| `docs/director/ROADMAP.md` | director | milestones and the status of every task |
| `docs/director/REVIEW.md` | director | the review checklist ([template](../../templates/REVIEW-CHECKLIST.md)) |
| `docs/adr/` | director | decisions with their reasons; agents propose the text, the director writes the ADR |
| `CLAUDE.md` | director | one paragraph sending every session with a task to the protocol ([template](../../templates/CLAUDE.md-snippet.md)) |

The director's own long-lived rules (the user's standing instructions: worker limit, model choice, "archive after a
merge") go into the director's memory files, and a short copy into the journal, so a fresh director session finds
them.

## The life of a task

### 1. Specification first

- The director writes `docs/tasks/T-xxx-*.md` from the template: goal, owned paths, contracts used and provided, scope,
  out of scope, tests, measurements, acceptance criteria, "must read" list, director's notes.
- **Owned paths are explicit.** Shared files (dependency manifests, build presets, lint config, root README, ADRs,
  tooling) are edited by the director, or by an agent with explicit permission in a bus decision.
- **A big or long-lived design gets an independent read** before launch: a fresh subagent reads the spec against the
  real code and lists contradictions, decisions the agent would have to guess, scope problems and untestable
  requirements. It routinely finds things the author cannot see (in our case: a determinism hole, an API that did not
  exist yet, a task that was two tasks).
- The spec is **committed to the main branch before the launch**: the agent's worktree branches off it.
- `task_create(agent="director", task_id=…, title=…, spec_path=…, depends_on=[…])`. Leave `owner` empty when the
  agent has not registered yet; the agent moves it to `in_progress` in its start checklist (with `owner=<its name>`
  it becomes the owner).

### 2. Launch

- **Choose the model by difficulty and write the choice into the journal before launching**: hard design or
  concurrency work — the strongest model; well-specified moderate work — a mid model with high effort; trivial work —
  medium effort. Review the work of weaker models more strictly.
- **Respect the worker limit** (we used two). A third stream is worth it only when the critical path allows real
  parallelism and the director can still review everything properly.
- Start the session in **its own git worktree**, in the permission mode the team uses, with the kickoff message from
  `KICKOFF.md`. In Claude Desktop the director starts it from its own chat; the user can open and watch it.
- Starting a task is the user's call: the director proposes, the user says "go".

### 3. Plan, then "go"

- The agent registers, reads the protocol, the task and everything on its "must read" list, and sends a plan as
  `review_request`. **No code before "go"** (a throw-away prototype for a measurement is fine if the spec allows it).
- The director answers with a `decision`: answers to the agent's questions, mandatory changes to the plan (numbered,
  so the agent can report "finding → change → test" later), and "go" for the next stage.
- **Intermediate reviews at architectural points**: after the API and the core algorithm, before the parts that build
  on them. Cheap to fix there, expensive later.

### 4. Work

- The agent keeps its status and its progress log current, claims paths outside its own area, asks over the bus
  instead of guessing, and sends `contract_change` for anything that touches a shared contract or dependency.
- The director answers `contract_change` explicitly: accepted / accepted with conditions / refused, who edits which
  file, and in which commit (we used separate commits tagged `[T-xxx][module]` for edits outside the agent's paths).
- When the agent reports a step done, the director acknowledges it (`note`) and wakes the agent if it stopped.

### 5. Hand-in and review

See [Review](#review). The verdict is a `decision`: merged, or a numbered list of fixes with "blocker / mandatory /
minor" and what to do next. Fixes come back as a new `review_request` with a "finding → fix → test (mutation result)"
list.

## Communication: the bus plus a wake-up

- **Substance goes into the bus**: plans, decisions, reviews, contract changes. It is persistent and searchable, and
  the agent's next tool call delivers it.
- **An idle session does not read the bus.** After every message that the agent must act on, send its session a
  short direct message ("decision #227 in the bus: read inbox(include_read=True) and act on it"). Without the wake-up
  the message just sits there and the director thinks the agent is working.
- **Long messages**: write the JSON to a file and send it with `agentbus send --json "$(cat reply.json)"`, which avoids
  shell quoting problems.
- **Waiting for mail**: run `agentbus waitmail agent=director timeout=1800` as a background command; it exits when mail
  arrives. Do not poll.
- **Kinds matter**: `review_request` for plans and hand-ins, `decision` for answers that change what the agent does,
  `note` for acknowledgements and status, `contract_change` for shared contracts, `question` / `answer`, `alert` for
  "stop everything".
- **Session notes are reports, not proof.** When a session says "done", check the branch before telling the user.

## Review

The rule: **the director verifies everything itself, in a clean checkout, at the exact commit**.

1. **Clean worktree.** `git worktree add --detach .claude/worktrees/review-T-xxx main`, then merge the agent's branch
   into it (`git merge --no-ff --no-edit <branch>`). This builds exactly what will land on main. Never build in the
   agent's own working copy: it may hold the uncommitted next step.
2. **The full matrix**: every compiler × configuration (or every platform × runtime) the project supports, with tests,
   logs kept. Zero warnings. Formatting and lint clean.
3. **Stress-run** the tests that involve time, threads, IO or shared resources, under CPU load, many repetitions (the
   agent must have done it too; repeat the risky ones).
4. **Read the code.** The director reads the public API, the docs and the risky parts itself.
5. **An independent read for hard code** (synchronization, memory, parsers, concurrency): a subagent gets the code
   and a list of risks to look at, **not the director's conclusions**, and returns concrete findings with file:line,
   a scenario and a severity. The director **verifies every finding against the code** before forwarding it.
6. **Mutation over trust.** For every fix, the agent writes the test first and shows it fails without the fix. For
   key claims ("the validation catches X", "the limit is enforced"), the director breaks the code on purpose and
   watches the test go red. A test that passes against broken code proves nothing.
7. **Check what the tool actually sees.** Validators, sanitizers and linters have blind spots (in our case the GPU
   validation layer could not see bindless accesses and silenced repeated messages). A test that relies on "the
   validator is quiet" must exercise something the validator can see.
8. **Verdict as a numbered list**, most severe first: blocker (contract broken, data race, corruption), mandatory
   (wrong behaviour in a real scenario), minor (docs, edge cases). Each item: where, scenario, what happens, what to
   do, what test proves it.

## Merge and clean-up

- Only the director merges, only when the tree of the merge equals the tree it verified
  (`git rev-parse HEAD^{tree}` in both).
- Then, without asking (if the user has said so once): `task_update(state="done")`, archive the agent's session, remove
  its worktree and branch, remove the review worktree, update the root README, the roadmap and the journal.
- The ADR text comes from the agent's log; the director writes the ADR.

## Pause and resume

**Pause** (the user is leaving):
1. Send every agent a `decision`: "finish what is in your hands into a coherent commit (or a `[WIP]` commit), update
   your progress log (state and next step), stop long background runs by PID and note the commit to rerun them on,
   leave the tree clean, send a note, set status paused". Wake each one.
2. Check each worktree yourself: clean, last commit as reported.
3. Write the resume point into the journal's "Now": for each task — branch, commit, what is done, what comes first;
   what the director owes (reviews, merges); what waits for the user.

**Resume**: start the bus, read "Now", send each agent "pause over, continue from your log" and wake it, handle the
review requests that arrived during the pause first.

## Working while the user is away

When the user leaves the director in charge until a given time:
- Write the window into the journal; set a background timer for the parking time (start parking 15 minutes before
  the end, so agents have time to commit).
- Do director work in the gaps: reviews, specs for the next tasks (with an independent read), small fixes on main
  with tests, data for flaky-test investigations.
- **Do not start new tasks or change the worker count** without the user's word. Proposals go into the journal.
- At the end: park the agents, write "Now", summarize for the user in a few lines.

## Rules of discipline

- **Stop processes only by PID**, never by name: other agents' builds and tools run on the same machine.
- **Flaky tests are bugs.** A test that failed once under load is not "retried until green": either it is fixed
  (usually it measured something shared with other threads or processes, or waited for time instead of a
  condition), or it goes into the journal with the data for an investigation.
- **One writer per file.** Shared files have one owner. Two sessions never edit the same files.
- **Never edit files through a shell round trip that can change their encoding** (on Windows PowerShell 5.1,
  `Get-Content` + `WriteAllText` corrupts BOM-less UTF-8). Use the editor tools; mark temporary mutations with a
  comment and grep for it before committing; check `git diff` before every commit.
- **Commits are small and described**; the director never commits on behalf of an agent in its branch.
- **The director's mistakes go into the journal too**, with the rule that prevents them, and into its memory.

## Why teams turn into a mess

The failure modes we saw or avoided, and what prevents each:

| Symptom | Cause | Prevention |
|---|---|---|
| The agent "ignores" a decision | it was idle and never read the bus | wake it with a direct message after every bus message |
| Work done twice or overwritten | two sessions in one checkout or one file | a worktree per agent, owned paths in the spec, claims |
| "It worked for the agent" | review trusted the report or built in the agent's copy | clean worktree at the exact commit, full matrix |
| The design is redone after a week | no plan review, no intermediate review | plan before code, review at architectural points |
| The director forgets what was decided | the decision lived only in the chat | journal with "Now", decisions in the bus and ADRs |
| An agent guesses and guesses wrong | vague spec | spec template, "must read" list, independent read of the spec |
| Tests pass but the bug is there | tests never failed against broken code | test first, mutation, check what the validator sees |
| The machine is overloaded and tests flake | too many workers, timing-based tests | worker limit, condition-based tests, stress runs |

## Starting a new project: a checklist

1. Install agentbus, register the MCP server and the hook ([README](../../README.md#setting-up-a-project)).
2. Copy the templates: `PROTOCOL.md` and `KICKOFF.md` to `docs/agents/`, `TASK.md` to `docs/tasks/_TEMPLATE.md`,
   `DIRECTOR-JOURNAL.md` to `docs/director/JOURNAL.md`, `REVIEW-CHECKLIST.md` to `docs/director/REVIEW.md`; fill in the
   placeholders and the project's quality bar (build commands, configurations, test rules).
3. Add the `CLAUDE.md` paragraph.
4. Write the roadmap: milestones, then the first tasks with dependencies.
5. Agree with the user on the standing rules (worker limit, model choice, what the director may do alone) and write
   them into the journal and the director's memory.
6. Write the first spec, commit it, launch the first agent.
