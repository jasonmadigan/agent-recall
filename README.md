# Claude Recall

Find an old Claude Code or Codex session by describing what you were doing, then resume it.

```bash
ccrecall "the session that built the auth middleware"
ccrecall "auth middleware" --resume
```

`claude --resume` matches a session **id**, a **name**, or the interactive picker's title filter. It does not search by what you were doing.

`ccrecall` does:

1. **Local heuristics** — scan transcripts under `~/.claude/projects` and Codex rollouts under every `CODEX_HOME`, then shortlist by first prompt, later user messages, title, git branch, and recency
2. **A deep scan** — when the index barely matches, grep the raw transcripts, which catches topics only Claude or a tool ever named
3. **A picker prompt** — send that shortlist to Claude (`claude -p`, or `/find-session` in an already-open session) to choose the session that actually did the work
4. **Resume** — optionally exec `claude --resume <id>` or `codex resume <id>`

## Install

From this repository:

```bash
./install.sh
```

That symlinks:

- `~/.local/bin/ccrecall` → `claude_recall.py` (`~/.local/bin` must be on `PATH`)
- `~/.claude/skills/find-session` → this repo's skill

Requires Python 3.9+. The `claude` CLI is used for the picker step and for `--resume`. `--fast` stops after heuristics if Claude is unavailable.

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
ccrecall "the session where I was doing X"     # heuristics, then Claude ranks
ccrecall                                       # recent sessions in this project
ccrecall "auth middleware" --resume            # jump into the top pick
ccrecall "auth middleware" --pick              # numbered picker, then resume
ccrecall "auth middleware" --all               # every project, not just cwd
ccrecall "auth middleware" --here ~/Work/other
ccrecall "auth middleware" --json              # scripts
ccrecall "auth middleware" --id                # print the top session id only
ccrecall "auth middleware" --fast              # heuristics only, skip Claude
ccrecall "auth middleware" --source codex      # one agent only (all | claude | codex)
ccrecall abc123 --resume                       # look up by id prefix, then resume
ccrecall "auth middleware" --resume --prompt "where did we leave off?"
ccrecall "auth middleware" --resume --fork     # pass --fork-session
ccrecall "auth middleware" --resume --print-cmd
ccrecall --reindex
```

Default scope is **this directory's sessions**, Claude and Codex alike, matched on the session's recorded cwd. `--all` searches everything. If cwd has no sessions, the CLI falls back to all projects and prints a note.

`--resume` `cd`s into the session's original working directory (when it still exists) and execs `claude --resume <id>` or `codex resume <id>`, exporting `CODEX_HOME` when the session came from a non-default Codex home.

```text
Asking Claude to pick among 18 sessions…
 1.  abc123ef  2026-08-18  ~/Work/my-app  [feat/auth-middleware]
    rewrite the auth middleware to use the new session store
    This is the implementation session, not the later hunt for it.
    claude --resume abc123ef-0000-0000-0000-000000000000
```

Set `CLAUDE_RECALL_MODEL` to override the model used for the picker.

## Inside Claude Code

```text
/find-session "the session that built the auth middleware"
```

The skill runs local heuristics (`ccrecall --fast --json`), then applies the same picker prompt (`skills/find-session/references/pick-session.md`) in the current session so it does not spawn a nested `claude -p`. Resuming starts a **new** Claude invocation; it cannot attach the current TUI to another transcript.

## Codex sessions

Codex writes rollouts to `<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`. `ccrecall` searches every Codex home it can find: `$CODEX_HOME`, `~/.codex`, and any `~/.codex*` sibling that holds a `sessions/` or `archived_sessions/` directory. A shell wrapper that exports a second home for a separate login is the normal reason those siblings exist, and sessions in them are easy to lose.

Results are tagged `(codex)` and carry a resume command that names the home when it is not the default:

```bash
ccrecall "keeping agents in sync" --all
#  1.   56.8  01a0ae6f  2026-09-18  ~  (codex)
#     CODEX_HOME=~/.codex-work codex resume 01a0ae6f-…
```

Subagent forks (`thread_source: subagent`) are skipped, as are rollouts whose only user turn is the replayed `AGENTS.md` and environment block.

```bash
ccrecall "…" --source codex      # codex only
ccrecall "…" --source claude     # claude only
CLAUDE_RECALL_CODEX_HOMES=       # empty disables codex; colon-separated list overrides discovery
```

Codex has no `--fork-session`, so `--fork` is ignored for those sessions.

## How it works

Claude Code stores transcripts as JSONL at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.

`ccrecall` indexes those files (skipping `subagents/`) into `~/.cache/claude-recall/index-v1.json`, keyed by path + mtime + size. It extracts first human prompt (slash-command `<command-args>` counts as the prompt), later user messages, `aiTitle`, git branch, and cwd. Files that parse to nothing are cached as misses so large rollouts are not re-read on every run.

**Heuristics** shortlist keyword matches plus recent sessions (cap 40). Tool output is ignored, and first prompts that are themselves a find/resume request are downranked.

**The deep scan** runs when fewer than three sessions match, and greps the raw transcripts for the query terms. The index only holds titles and human turns, so a topic that only Claude or a tool ever wrote down is invisible without it. `rg` does the file matching when present; BSD `grep -Fi` is the fallback and is roughly eighty times slower on a multi-gigabyte transcript set, slow enough that the time budget can cut the scan short. Install ripgrep.

```bash
CLAUDE_RECALL_DEEP_SECONDS=20    # deep scan wall-clock budget
```

**The picker prompt** then ranks that shortlist: prefer the session that *did* the work over one that later asked to find it, or that only mentioned the topic. From the CLI this is `claude -p`. From `/find-session` it is the current model. If `claude` is missing or the pick cannot be parsed, you get the heuristic list.

The picker runs against your personal Claude login: Vertex refuses the Anthropic publisher models unless data sharing is enabled on the GCP project, which returns 403 and silently drops you back to keyword ranking. `CLAUDE_RECALL_VERTEX=1` keeps whatever the shell already exports, and `CLAUDE_RECALL_MODEL` overrides the model.

```bash
ccrecall --fast           # heuristics only
ccrecall --reindex        # rebuild the cache after unusual transcript edits
```

## Related

| You want | Use |
|-|-|
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
