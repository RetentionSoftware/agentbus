# Starting a new agent

<!--
Template from agentbus. Put it next to the protocol (e.g. docs/agents/KICKOFF.md) and replace the {{...}}
placeholders. Delete this comment.
-->

## The usual way: the director starts the agent

1. The director writes the task `{{TASKS_DIR}}/T-xxx-*.md` and **commits it to `{{MAIN_BRANCH}}`**: the agent's
   worktree branches off `{{MAIN_BRANCH}}`, so an uncommitted task is invisible to the agent.
2. The bus is running: `agentbus health` answers (start it with `scripts/start.ps1` / `scripts/start.sh` from the
   agentbus checkout, or `agentbus serve` in a terminal of its own).
3. The director creates the agent's session from its own chat (in Claude Desktop: a new session in a separate
   worktree) and sends the kickoff message below. The user can open that session, watch it and write to it. If the
   app asks to **trust the repository** in the new session, confirm it.
4. The agent registers (visible in `agentbus watch`) and sends its plan. The director reviews the plan and answers
   "go". An agent that waits is woken up by a direct message from the director's session; the bus carries the
   substance, the direct message only wakes the agent up.

## The fallback: the user creates the session

If the new session cannot be created in a worktree, start it in the project folder and prepend to the kickoff
message: "First switch to your own worktree named <agent-name>" (Claude Code: the EnterWorktree tool).

## Kickoff message

```text
You are the engineer agent `<agent-name>` of the {{PROJECT}} project. Your task: `{{TASKS_DIR}}/<T-xxx-file>.md`.

1. Read `{{PROTOCOL_PATH}}` and run the start checklist of its section 1 (worktree and branch, registration on the
   agentbus, status).
2. Read your task and everything it tells you to read.
3. Send your plan to the director over the bus: send(to="director", kind="review_request", subject="plan <T-xxx>",
   body=<a plan of 3-7 steps, the paths you will touch, open questions>).
4. Report here briefly: name, branch, worktree, plan, questions.

Do not write code until you get "go". The director is the session that started you: its instructions arrive as bus
messages ([agent-bus]) or as direct messages in this chat.
```
