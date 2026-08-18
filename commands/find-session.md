---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing.
argument-hint: "[--all] <what you were doing>"
---

# Find session

Run the bundled CLI and show ranked matches with `claude --resume <id>` commands.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/claude_recall.py --json $ARGUMENTS
```

If `$ARGUMENTS` is empty, ask what they were doing first.

Present rank, date, cwd, branch, first prompt, session id, and the resume command. You cannot attach this session to another transcript; resume happens in a new `claude --resume` invocation.
