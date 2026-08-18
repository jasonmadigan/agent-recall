# Claude Recall

Find an old Claude Code session by describing what you were doing, then resume it.

```bash
ccrecall "the session that built the mcp inspector PoC"
ccrecall "mcp inspector" --resume
```

`claude --resume` matches a session **id**, a **name**, or the interactive picker's title filter. It does not search by what you were doing.

`ccrecall` gathers a catalog of local transcripts (first prompt, later user messages, title, branch, cwd) and **runs `claude -p` to pick the session that actually did the work**. Then it can exec `claude --resume` for you.

## Install

From this repository:

```bash
./install.sh
```

That symlinks:

- `~/.local/bin/ccrecall` → `claude_recall.py` (`~/.local/bin` must be on `PATH`)
- `~/.claude/skills/find-session` → this repo's skill

Requires Python 3.9+ and the `claude` CLI (used to find the session, and again for `--resume`). Keyword-only ranking is available with `--fast` if Claude is unavailable.

Start a new Claude Code session so `/find-session` is available.

```bash
git clone https://github.com/jasonmadigan/claude-recall.git
cd claude-recall
./install.sh
```

### Optional: Claude Code plugin

`./install.sh` is enough for daily use. To load this repo as a plugin instead (or as well):

```bash
claude plugin marketplace add /path/to/claude-recall
claude plugin install find-session@claude-recall
```

Or for a single invocation: `claude --plugin-dir /path/to/claude-recall`.

## Usage

```bash
ccrecall "the session where I was doing X"    # Claude picks from local sessions
ccrecall                                      # recent sessions in this project
ccrecall mcp inspector --resume               # jump into Claude's top pick
ccrecall mcp inspector --pick                 # numbered picker, then resume
ccrecall mcp inspector --all                  # every project, not just cwd
ccrecall mcp inspector --here ~/Work/other
ccrecall mcp inspector --json                 # scripts / the Claude skill
ccrecall mcp inspector --id                   # print the top session id only
ccrecall mcp inspector --fast                 # skip Claude; keyword rank only
ccrecall acdb2e31 --resume                    # look up by id prefix, then resume
ccrecall mcp inspector --resume --prompt "where did we leave off?"
ccrecall mcp inspector --resume --fork        # pass --fork-session
ccrecall mcp inspector --resume --print-cmd   # print the resume command, don't exec
ccrecall --reindex
```

Default scope is **this directory's Claude project**. `--all` searches every project under `~/.claude/projects/`. If cwd has no sessions, the CLI falls back to all projects and prints a note.

`--resume` `cd`s into the session's original working directory (when it still exists) and execs `claude --resume <id>`.

```text
Asking Claude to pick among 18 sessions…
 1.  acdb2e31  2026-08-18  ~/Work/kuadrant-console-plugin  [poc/mcp-inspector-direct]
    /goal … Let's build this out…
    This is the session that built the inspector PoC, not the later hunt for it.
    claude --resume acdb2e31-afa2-424e-82cf-7721376c153a
```

Set `CLAUDE_RECALL_MODEL` to override the model used for the picker.

## Inside Claude Code

```text
/find-session the session that built the mcp inspector PoC
```

The skill dumps a local catalog (`ccrecall --fast --json`) and this Claude session ranks it, so it does not spawn a nested `claude -p`. Resuming starts a **new** Claude invocation; it cannot attach the current TUI to another transcript.

## How it works

Claude Code stores transcripts as JSONL at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.

`ccrecall` indexes those files (skipping `subagents/`) into `~/.cache/claude-recall/index-v1.json`, keyed by path + mtime + size. It extracts first human prompt (slash-command `<command-args>` counts as the prompt), later user messages, `aiTitle`, git branch, and cwd.

That catalog — keyword matches plus recent sessions, capped at 40 — is sent to `claude -p`. Claude is told to prefer the session that **did** the work over one that later asked to find it, or that only mentioned the topic. If `claude` is missing or the pick cannot be parsed, it falls back to keyword ranking.

```bash
ccrecall --fast           # keyword rank only
ccrecall --reindex        # rebuild the cache after unusual transcript edits
```

## Related

| You want | Use |
| --- | --- |
| Last session in this directory | `claude --continue` |
| Picker / name / id | `claude --resume [term]` |
| Resume and send a follow-up | `claude -r <id> "follow up"` |
| "The session where I was doing X" | `ccrecall "…"` |

Full-text / fzf tools (`claude-grep`/`ccfind`, `claude-chat-search`, `cc-sessions`) are better when you remember an exact string.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
