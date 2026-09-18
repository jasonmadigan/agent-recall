# Contributing

Agent Recall uses the Python standard library. Python 3.9 and newer are supported; CI runs the unittest suite on 3.9 and 3.12.

## Source layout

- `agent_recall.py`: discovery, source parsers, cache, search/ranking, CLI, and resume commands.
- `claude_recall.py`: compatibility entry point for the former module and command.
- `skills/find-session/`: shared skill and ranking prompt.
- `commands/` and `.claude-plugin/`: Claude plugin entry point and metadata.
- `install.sh`: links the CLI and shared skill into user directories.
- `tests/`: synthetic transcripts, temporary SQLite databases, and CLI/installer regression checks.

## Verify a change

```bash
python3 -m unittest discover -s tests -v
python3 agent_recall.py --help
bash -n install.sh
python3 -m pip wheel --no-deps . -w dist
```

Tests use temporary directories and synthetic session data. Keep real transcripts, prompts, databases, and credentials out of fixtures. Mock ranking subprocesses; tests should not call a model or open an interactive agent.

When changing a source adapter, cover discovery, extraction of human prompts, cache invalidation, and the corresponding resume command. SQLite tests must include WAL writes and session-specific deep search. A malformed source should not hide valid results from other sources.

When changing the CLI or environment settings, update the README and skill examples. Keep the version in `agent_recall.py`, `pyproject.toml`, and `.claude-plugin/plugin.json` consistent. Verify both `agent-recall` and `ccrecall` after a wheel install.

Create commits with `git commit --signoff`.
