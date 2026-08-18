---
name: find-session
description: Find and resume an earlier Claude Code session by describing what you were doing.
argument-hint: "[--all] <what you were doing>"
---

# Find session

Load a local session catalog, then pick the match yourself (do not spawn nested `claude -p`):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/claude_recall.py --fast --json $ARGUMENTS
```

If `$ARGUMENTS` is empty, ask what they were doing first.

Prefer the session that actually did the work, not a later "find me the session" prompt. Present date, cwd, branch, first prompt, a one-line reason, session id, and `claude --resume <id>`. Resume happens in a new invocation.
