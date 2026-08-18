# Pick session

Rank candidate Claude Code sessions for resume.

## Query

{{QUERY}}

## Candidates

{{SESSIONS_JSON}}

## Rules

Prefer the session that **actually did the work**:

- A first prompt that is a build, fix, implement, or design request
- A matching git branch
- More than a couple of user turns

Downrank:

- Sessions whose first prompt is itself "find me the session…" / "resume the session…"
- Drive-by mentions (one question about the topic, no follow-through)
- Sessions that only talked about the work in passing

If several match, best first. Omit sessions that are clearly unrelated.

## Output

Return ONLY JSON:

```json
{"results": [{"id": "<session-id>", "reason": "<one line>"}]}
```

At most {{LIMIT}} results.
