---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing. Use when the user asks for an old/closed session, wants to jump back into prior work, or says "the session where I was…".
argument-hint: "[--all] [--resume] <what you were doing>"
---

# Find session

Shortlist local Claude Code sessions with heuristics, then rank them with the picker prompt. Do not spawn another `claude -p` — this session is the ranker.

## 1. Heuristic catalog

Use `--fast --json` so the CLI only reads transcripts:

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

## 2. Rank with the picker prompt

Read `references/pick-session.md` (same directory tree as this skill). Treat `$ARGUMENTS` as the query and the CLI JSON as the candidate list. Follow that file's rules and output format, then present the ranked sessions to the user as a readable list (not raw JSON).

## Present results

For each hit:

- date, cwd, git branch
- title or first prompt (one line)
- one-line reason from the picker
- session id
- the exact resume command: `claude --resume <session_id>`

If they want to resume, do **not** claim you can attach this TUI to that session. Tell them to run the resume command in a terminal (or run it for them with `ccrecall --resume` / `claude --resume <id>` only if they explicitly want you to spawn it). Prefer printing the command unless they ask you to launch it.

If nothing matches, say so and suggest `--all` or a more specific phrase from the first prompt or branch name.
