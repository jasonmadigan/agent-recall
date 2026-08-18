---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing. Use when the user asks for an old/closed session, wants to jump back into prior work, or says "the session where I was…".
argument-hint: "[--all] [--resume] <what you were doing>"
---

# Find session

Load a catalog of local Claude Code sessions, then **you** pick the one that matches the user's description. Do not spawn another `claude -p` — this session is the ranker.

## Run

Use `--fast --json` so the CLI only reads transcripts and does not call Claude again:

```bash
if command -v ccrecall >/dev/null 2>&1; then
  ccrecall --fast --json $ARGUMENTS
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  python3 "${CLAUDE_PLUGIN_ROOT}/claude_recall.py" --fast --json $ARGUMENTS
else
  python3 ~/.local/bin/ccrecall --fast --json $ARGUMENTS
fi
```

If `$ARGUMENTS` is empty, ask what they were doing, then rerun.

Pass `--all` when they did not specify a repo, or when they say the work might be in another project.

## Pick

From the JSON results, choose the session that **actually did the work**:

- Prefer a first prompt that is a build/fix/implement request, a matching git branch, and more than a couple of user turns.
- Downrank sessions whose first prompt is itself "find me the session…".
- Downrank drive-by mentions (one question about the topic, no follow-through).

## Present results

Print a short ranked list (your ranking, not the keyword scores). For each hit:

- date, cwd, git branch
- title or first prompt (one line)
- one-line reason
- session id
- the exact resume command: `claude --resume <session_id>`

If they want to resume, do **not** claim you can attach this TUI to that session. Tell them to run the resume command in a terminal (or run it for them with `ccrecall --resume` / `claude --resume <id>` only if they explicitly want you to spawn it). Prefer printing the command unless they ask you to launch it.

If nothing matches, say so and suggest `--all` or a more specific phrase from the first prompt or branch name.
