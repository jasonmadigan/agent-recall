# Claude Recall

Find an old Claude Code session by describing what you were doing, then resume it.

```bash
ccrecall "the session that built the mcp inspector PoC"
ccrecall "mcp inspector" --resume
```

`claude --resume` matches a session **id**, a **name**, or the interactive picker's title filter. It does not search by what you were doing. `ccrecall` ranks local transcripts and can exec `claude --resume` for you.

## Install

From this repository:

```bash
./install.sh
```

That symlinks:

- `~/.local/bin/ccrecall` → `claude_recall.py` (`~/.local/bin` must be on `PATH`)
- `~/.claude/skills/find-session` → this repo's skill

Requires Python 3.9+. The `claude` CLI is only needed for `--resume` and `--llm`.

Start a new Claude Code session so `/find-session` is available.

To clone from GitHub:

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
ccrecall "the session where I was doing X"
ccrecall                                      # recent sessions in this project
ccrecall mcp inspector --resume               # jump into the top match
ccrecall mcp inspector --pick                 # numbered picker, then resume
ccrecall mcp inspector --all                  # every project, not just cwd
ccrecall mcp inspector --here ~/Work/other
ccrecall mcp inspector --json                 # scripts / the Claude skill
ccrecall mcp inspector --id                   # print the top session id only
ccrecall mcp inspector --llm                  # rerank top hits with `claude -p`
ccrecall acdb2e31 --resume                    # look up by id prefix, then resume
ccrecall mcp inspector --resume --prompt "where did we leave off?"
ccrecall mcp inspector --resume --fork        # pass --fork-session
ccrecall mcp inspector --resume --print-cmd   # print the resume command, don't exec
ccrecall --reindex
```

Default scope is **this directory's Claude project**. `--all` searches every project under `~/.claude/projects/`. If cwd has no sessions, the CLI falls back to all projects and prints a note.

`--resume` `cd`s into the session's original working directory (when it still exists) and execs `claude --resume <id>`.

```text
 1.  227.9  acdb2e31  2026-08-18  ~/Work/kuadrant-console-plugin  [poc/mcp-inspector-direct]
    /goal … Let's build this out…
    first prompt; branch
    claude --resume acdb2e31-afa2-424e-82cf-7721376c153a
```

## Inside Claude Code

```text
/find-session the session that built the mcp inspector PoC
```

The skill runs `ccrecall --json` and prints ranked matches with a `claude --resume <uuid>` line. Resuming starts a **new** Claude invocation; it cannot attach the current TUI to another transcript.

## How it works

Claude Code stores transcripts as JSONL at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.

`ccrecall` indexes those files (skipping `subagents/`) into `~/.cache/claude-recall/index-v1.json`, keyed by path + mtime + size. It scores:

- first real human prompt (slash-command `<command-args>` counts as the prompt)
- later user messages
- `aiTitle`, git branch, cwd

Tool output is ignored, so a session that *mentioned* a topic in bash output loses to the one whose first prompt was "let's build this". Sessions whose first prompt is itself a find/resume request are downranked.

```bash
ccrecall --reindex    # rebuild the cache after unusual transcript edits
```

## Related

| You want | Use |
| --- | --- |
| Last session in this directory | `claude --continue` |
| Picker / name / id | `claude --resume [term]` |
| Resume and send a follow-up | `claude -r <id> "follow up"` |
| "The session where I was doing X" | `ccrecall "…"` |

You cannot add a `claude --resume --prompt "the session where…"` flag. Claude Code extension points are skills, slash commands, and plugins; this repo ships a CLI plus `/find-session`.

Full-text / fzf tools (`claude-grep`/`ccfind`, `claude-chat-search`, `cc-sessions`) are better when you remember an exact string. This one is for intent ranking.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
