# Claude Recall

Find an old Claude Code session by describing what you were doing, then resume it.

```bash
ccrecall "the session that built the mcp inspector PoC"
ccrecall "mcp inspector" --resume
```

Claude Code already has `claude --resume` and `claude --continue`. Those resume **by session id, session name, or an interactive picker**. The picker's search term filters titles/names, not the work you actually did. There is no `claude --resume --prompt "the session where I was…"`.

You also cannot add new top-level flags to the Claude CLI. The supported extension points are **skills, slash commands, and plugins**. This repo ships both:

1. **`ccrecall`** — a standalone CLI you run from any terminal (including when Claude is not open)
2. **`/find-session`** — a Claude Code skill/command that shells out to the same CLI

## Why not grep?

Keyword tools exist (`claude-grep`/`ccfind`, `claude-chat-search`, `cc-sessions`). They are great when you remember an exact string.

This tool is for the other case: *"the session where I was building X"*. It ranks **first prompt, later user messages, git branch, and title**, downranks sessions whose first prompt is itself a find/resume request, and slightly prefers longer implementation sessions when the query looks like build work.

## Install

```bash
git clone https://github.com/jasonmadigan/claude-recall.git
cd claude-recall
./install.sh
```

Requires Python 3.9+ and the `claude` CLI on `PATH` (only needed for `--resume` / `--llm`).

`install.sh` will:

- symlink `ccrecall` to `~/.local/bin/ccrecall` (make sure that directory is on `PATH`)
- symlink the skill to `~/.claude/skills/find-session`

Restart Claude Code (or `/reload`) so `/find-session` appears.

### As a plugin

From this repo:

```bash
claude plugin install --source .
```

Or add it as a local marketplace later. The plugin exposes `/find-session`.

## Usage

```text
ccrecall "the session where I was doing X"
ccrecall mcp inspector --resume          # jump into the top match
ccrecall mcp inspector --pick            # numbered picker, then resume
ccrecall mcp inspector --json            # for scripts / the Claude skill
ccrecall mcp inspector --all             # every project, not just cwd
ccrecall mcp inspector --llm             # rerank top hits with `claude -p`
ccrecall acdb2e31                        # resume by id prefix
ccrecall mcp inspector --resume --prompt "where did we leave off?"
```

Default search scope is **this directory's Claude project**. Pass `--all` to search every project under `~/.claude/projects/`. If the current directory has no sessions, the CLI falls back to all projects and says so.

`--resume` `cd`s into the session's original working directory (when it still exists) and execs `claude --resume <id>`. Add `--fork` to pass `--fork-session`. `--print-cmd` prints the command instead of execing it.

## Inside Claude Code

```text
/find-session the session that built the mcp inspector PoC
```

The skill runs `ccrecall --json` and prints ranked matches with a ready `claude --resume <uuid>` line. Ask it to resume one and it will tell you the command (it cannot attach your current TUI to another session; resume happens in a new Claude invocation).

## How it works

Claude Code stores transcripts as JSONL under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.

`ccrecall` indexes those files (skipping `subagents/`) into `~/.cache/claude-recall/index-v1.json`, keyed by path + mtime + size. It extracts:

- first real human prompt (slash-command `<command-args>` counted as the prompt)
- later user messages
- `aiTitle`, git branch, cwd, timestamps

It does **not** treat tool output as the user's intent. That is why a session that *mentioned* MCP inspector in bash output loses to the session whose first prompt was "let's build this PoC".

Reindex after unusual transcript edits:

```bash
ccrecall --reindex
```

## Claude CLI notes

| You want | Built-in | This tool |
| --- | --- | --- |
| Last session in this directory | `claude --continue` | |
| Picker / name / id | `claude --resume [term]` | `ccrecall <id-prefix>` |
| Resume and send a follow-up | `claude -r <id> "follow up"` | `ccrecall … --resume --prompt "follow up"` |
| "The session where I was doing X" | not supported | `ccrecall "…"` |

Custom commands live in `~/.claude/commands/` or a plugin's `commands/`. Skills live in `~/.claude/skills/` or a plugin's `skills/`. None of those can add a `claude --resume --prompt` flag; they wrap this CLI instead.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
