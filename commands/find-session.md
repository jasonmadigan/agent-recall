---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing.
argument-hint: "[--all] <what you were doing>"
---

# Find session

Shortlist with local heuristics, then rank with the picker prompt in this session (do not spawn nested `claude -p`):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/claude_recall.py --fast --json $ARGUMENTS
```

If `$ARGUMENTS` is empty, ask what they were doing first.

Read `${CLAUDE_PLUGIN_ROOT}/skills/find-session/references/pick-session.md` and apply it to that catalog. Present date, cwd, branch, first prompt, a one-line reason, session id, and `claude --resume <id>`. Resume happens in a new invocation.
