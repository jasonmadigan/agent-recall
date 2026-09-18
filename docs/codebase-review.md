# Codebase sanity review — September 2026

Reviewed discovery, parsing, caching, scoring, model-result handling, resume commands, installation, packaging, and documentation while adding OpenCode and Pi support. This was a functional review and regression pass, not a security audit.

## Fixed

| Finding | Effect | Change and regression coverage |
| --- | --- | --- |
| CLI required the Claude projects directory before indexing Codex | Codex-only installations could not search | Discovery works with absent source directories; CLI tests run without Claude storage or executables |
| Every Codex fork was treated as a subagent | Human-created forks disappeared from search | Recognize subagent source metadata and retain normal forks |
| Default Codex home omitted its environment override | An inherited `CODEX_HOME` could resume the wrong store | Explicitly restore the session's default home when an override is inherited |
| Candidate lists used bare session IDs as unique keys | Copies or cross-source ID collisions could overwrite results | Use storage paths internally, unique candidate IDs for model ranking, and reject ambiguous direct lookup |
| Model-result matching accepted overlong or ambiguous prefixes | An invalid model ID could select an unrelated session | Accept only exact IDs or unique prefixes |
| Deep-search subprocess timeout ignored the configured scan budget | File matching could run for 120 seconds despite a shorter budget | Pass the remaining budget to each subprocess and check it between body records |
| JSON parsers assumed every record/cache root was an object | Malformed but valid JSON could crash indexing | Validate record and cache shapes; tolerate partial JSONL records |
| Codex fork documentation was stale | `--fork` resumed in place instead of creating a fork | Use the available `codex fork` command |
| Follow-up arguments and printed resume commands assumed Claude behavior | OpenCode treated a prompt as a project path; commands could start from the wrong cwd | Source-specific prompt arguments and shell-quoted directory/environment prefixes |
| Installer could link inside an existing skill directory | A conflicting installation could be reported as successful | Refuse regular-file/directory conflicts and test both CLI-only and full installation |
| Version metadata, package description, and skill text disagreed with the implementation | Installation and usage docs described different capabilities | Align metadata at 0.5.0, package the ranking prompt, and document all four sources |

## Validation and limits

The automated suite covers synthetic Claude/Codex/Pi JSONL, OpenCode SQLite including WAL updates/deletion, cache reuse/corruption, CLI behavior without Claude, ID collisions, resume arguments, and installer conflicts. A read-only smoke test also indexed this development machine's histories from all four agents.

Interactive resumes and paid model-ranking requests are not run by the tests. Flags were checked against the installed agents' help. OpenCode support targets its SQLite layout; legacy JSON stores and remote servers remain outside the current reader. Pi search covers all branches in a file, while resume opens its current leaf. The deep-scan budget is checked between records and cannot interrupt an individual filesystem read.
