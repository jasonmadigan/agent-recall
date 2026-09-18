# Agent Recall

Find an old coding-agent session by describing what you were doing, then resume it in the agent that owns it.

```bash
agent-recall "the session that built the auth middleware"
agent-recall "auth middleware" --pick
```

Searches **Claude Code, Codex, OpenCode, and Pi** history on your machine. Python 3.9+ is the only runtime requirement. Search runs locally; an agent CLI is only required to resume its sessions or to request optional Claude ranking.

## Install

```bash
git clone https://github.com/jasonmadigan/agent-recall.git
cd agent-recall
./install.sh
```

The installer links two commands into `~/.local/bin` and the `find-session` skill into `~/.agents/skills` (Codex, OpenCode, and Pi) and `~/.claude/skills` (Claude Code). Set `CLAUDE_CONFIG_DIR` if your Claude configuration lives elsewhere. Add `~/.local/bin` to `PATH` if necessary, and restart your agent to discover the skill.

Keep the checkout in place: these are symlinks. Rerun the installer after moving it. Existing symlinks can be updated; existing regular files or directories are left alone and reported as conflicts.

For just the commands:

```bash
./install.sh --cli-only
```

Alternatively, install the CLI from the checkout into a Python environment:

```bash
python3 -m pip install .
```

A pip install includes the skill files under `<environment>/share/agent-recall/skills`, but does not link them into agent configuration directories. The repository installer is the simplest way to install both CLI and skill.

## Usage

```bash
agent-recall                                  # recent sessions in this project
agent-recall "auth middleware"               # local keyword and transcript search
agent-recall "auth middleware" --all         # every project
agent-recall "auth middleware" --here ~/Work/app
agent-recall "auth middleware" --source pi   # all | claude | codex | opencode | pi
agent-recall "auth middleware" --json        # structured results, including resume commands
agent-recall "auth middleware" --id          # top session ID only
agent-recall "auth middleware" --limit 20
agent-recall "auth middleware" --ranker claude # optional model ranking via claude -p
agent-recall --reindex                        # rebuild the local cache
```

Default scope is the current directory and its subdirectories, matched against each session's recorded working directory. If there are no sessions in scope, search expands to every project and reports that fallback. `--all` explicitly searches everything.

`--source` filters results to one agent; discovery still refreshes the shared cache for all configured sources. Set the discovery overrides below to disable a source entirely.

### Resume

```bash
agent-recall "auth middleware" --resume      # resume the highest-ranked match
agent-recall "auth middleware" --pick        # select from numbered matches
agent-recall 01a0ae6f --source codex --resume # UUID or eight-character prefix
agent-recall ses_abc123 --resume              # OpenCode ID or unambiguous prefix
agent-recall "auth middleware" --resume --fork
agent-recall "auth middleware" --resume --print-cmd
agent-recall "auth middleware" --resume --prompt "where did we leave off?"
```

`--resume` changes to the recorded working directory when it exists, then replaces the Recall process with the owning agent. `--prompt` must come last; everything after it becomes one follow-up prompt. `--print-cmd`, used with `--resume` or `--pick`, displays the command instead of launching it.

| Agent | Resume | With `--fork` |
| --- | --- | --- |
| Claude Code | `claude --resume <id>` | Adds `--fork-session` |
| Codex | `codex resume <id>` | Uses `codex fork <id>` |
| OpenCode | `opencode --session <id>` | Adds `--fork` |
| Pi | `pi --session <file>` | Uses `pi --fork <file>` |

Generated commands preserve the original directory, Codex home, and OpenCode data root. Pi uses the full session filename to avoid ambiguity. Older agent versions may need upgrading to support their fork command.

An ambiguous ID is rejected. Use a full ID and `--source` to narrow it; when two homes contain copies of the same session, choose a result with `--pick`.

## Inside an agent

The same `find-session` skill searches all four stores regardless of which agent is running it. Ask your agent to “use find-session to find the session where I built the auth middleware.” Claude Code also exposes `/find-session`; in Pi it is `/skill:find-session`.

The skill runs `agent-recall --fast --json`, then ranks the candidates using the current conversation's model and the [shared picker prompt](skills/find-session/references/pick-session.md). It gives you the exact command to resume the chosen session in another terminal. The current conversation cannot be switched into an old session by this skill.

### Optional Claude Code plugin

The installer is sufficient for normal use. To load the checkout as a Claude plugin:

```bash
claude plugin marketplace add /path/to/agent-recall
claude plugin install find-session@agent-recall
```

Or use `claude --plugin-dir /path/to/agent-recall`. The plugin's name remains `find-session`; the marketplace is now `agent-recall`.

## Session discovery

| Agent | Default local storage | What is indexed |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/<encoded-cwd>/*.jsonl` | Human prompts, title, cwd, branch; skips subagent transcripts and metadata turns |
| Codex | `<CODEX_HOME>/sessions/**/*.jsonl` and `archived_sessions/**/*.jsonl` | Human prompts, cwd, branch; skips subagents and replayed instruction/environment turns |
| OpenCode | `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` | Top-level sessions, title, directory, user text parts; skips synthetic/ignored parts |
| Pi | `${PI_CODING_AGENT_DIR:-~/.pi/agent}/sessions/**/*.jsonl` | Session header, session name, user messages across the stored tree |

Codex discovery includes `$CODEX_HOME` and every `~/.codex*` sibling containing a session directory, so separate work/personal homes are searchable. Human-created Codex forks remain searchable. Pi indexes the whole stored tree; a match may refer to an earlier branch rather than the active leaf.

OpenCode support reads the SQLite `session`, `message`, and `part` tables in read-only mode. It accounts for uncheckpointed WAL updates. Legacy OpenCode JSON-directory stores, remote servers, and other database layouts are not supported. Database read errors are reported without preventing searches of other sources.

### Configuration

| Variable | Purpose |
| --- | --- |
| `CLAUDE_PROJECTS_DIR` | Override Claude transcript directory |
| `AGENT_RECALL_CODEX_HOMES` | Colon-separated Codex homes; empty disables Codex discovery |
| `AGENT_RECALL_PI_DIRS` | Colon-separated Pi session directories; empty disables Pi discovery |
| `AGENT_RECALL_OPENCODE_DBS` | Colon-separated OpenCode database files; empty disables OpenCode discovery |
| `AGENT_RECALL_CACHE` | Override cache filename |
| `AGENT_RECALL_DEEP_SECONDS` | Deep transcript search budget in seconds; default `20`, `0` disables it |
| `AGENT_RECALL_MODEL` | Model override for `--ranker claude`; otherwise uses Claude's configured default |
| `AGENT_RECALL_VERTEX` | Set to `1` to retain the shell's Vertex settings for Claude ranking |

Use your actual `<data-root>/opencode/opencode.db` files for OpenCode overrides so generated resume commands can select the matching data root. Multiple paths use the platform path separator (`:` on macOS/Linux).

Every `AGENT_RECALL_*` setting also accepts the corresponding `CLAUDE_RECALL_*` name. The new name takes precedence, including an empty value.

## Search and privacy

The index lives at `~/.cache/agent-recall/index-v3.json`. It contains session paths, metadata, and excerpts of user prompts, so treat it as private conversation data. Deleting it is safe; the next search rebuilds it.

Search weights first prompts, titles, later user messages, branches, and directories, with a recency adjustment. Requests to *find* another session are downranked. When fewer than three sessions match, Recall scans transcript bodies for terms that appeared only in an assistant response or tool output. OpenCode scans only the parts belonging to each session. Install `rg` for faster file scanning; `grep` or Python is used when unavailable. The scan stops at its time budget, checked between records; a very large record or slow filesystem read can overrun it.

Default CLI search invokes no model and sends no transcript text over the network. `--fast` explicitly keeps that behavior. `--ranker claude` sends a shortlist of up to 40 sessions, including prompt excerpts, to your configured Claude service. It disables picker tools and falls back to keywords if the CLI fails or returns unusable results. Claude ranking clears Vertex-specific environment settings unless `AGENT_RECALL_VERTEX=1` is set.

Using the skill places those excerpts in your current agent conversation, subject to that agent's provider and privacy settings.

Exit codes: `0` for results or a successful reindex, `1` for no match or an ambiguous ID, `2` for invalid CLI options/settings or a missing resume executable. Choosing not to resume exits successfully.

## Development

```bash
python3 -m unittest discover -s tests -v
```

See [contributing](CONTRIBUTING.md) for the source layout, test fixtures, and packaging checks.

Storage and command references: [OpenCode CLI](https://opencode.ai/docs/cli/), [OpenCode skills](https://opencode.ai/docs/skills/), [Pi session format](https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/docs/session-format.md), [Pi skills](https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/docs/skills.md). Resume flags were also checked against the installed CLIs during development.

## License

MIT
