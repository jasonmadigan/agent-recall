---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing. Use when the user asks for an old/closed session, wants to jump back into prior work, or says "the session where I was…".
argument-hint: "[--all] [--resume] <what you were doing>"
---

# Find session

Resolve the user's description to a Claude Code session using the bundled CLI, then show ranked matches.

## Run

Prefer `ccrecall` on PATH (installed by `./install.sh`). Fall back to the plugin copy of the script:

```bash
if command -v ccrecall >/dev/null 2>&1; then
  ccrecall --json $ARGUMENTS
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  python3 "${CLAUDE_PLUGIN_ROOT}/claude_recall.py" --json $ARGUMENTS
else
  python3 ~/.local/bin/ccrecall --json $ARGUMENTS
fi
```

If `$ARGUMENTS` is empty, ask what they were doing, then rerun.

Pass `--all` when they did not specify a repo, or when they say the work might be in another project.

## Present results

Print a short ranked list. For each hit:

- rank, score, date, cwd, git branch
- title or first prompt (one line)
- session id
- the exact resume command: `claude --resume <session_id>`

If they want to resume, do **not** claim you can attach this TUI to that session. Tell them to run the resume command in a terminal (or run it for them with `ccrecall --resume` / `claude --resume <id>` only if they explicitly want you to spawn it). Prefer printing the command unless they ask you to launch it.

If nothing matches, say so and suggest `--all` or a more specific phrase from the first prompt or branch name.
