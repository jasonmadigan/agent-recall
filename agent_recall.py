#!/usr/bin/env python3
"""Find and resume coding-agent sessions by what you were doing."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__version__ = "0.5.0"

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".cache" / "agent-recall" / "index-v3.json"
INDEX_VERSION = 3

STOPWORDS = frozenset(
    """
    a an the and or of to in on for with at from by as into over
    this that these those it its my me i i've i'm i'd we you your
    was were be been being is are am do did doing does have had has
    session sessions conversation conversations chat claude codex opencode pi code
    where when which what who how find looking looked look old closed
    earlier previous prior one ones thing stuff please can could would
    should want wanted like just about here folder repo repository
    using use used make made
    """.split()
)

FIND_RE = re.compile(
    r"find me the session|find the session|find an? old|resume the session|"
    r"resume a session|where was i|which session|looking for the session|"
    r"session in this (folder|repo)|can you find me the session",
    re.I,
)

COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
TAG_RE = re.compile(r"<[^>]+>")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*", re.I)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$",
    re.I,
)
BUILD_TERMS = frozenset(
    "build built building implement implementing poc prototype create created "
    "write wrote writing add added fix fixed debug".split()
)

SKIP_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<task-notification",
    "<scheduled-wakeup",
    "<background-task",
    "[Request interrupted",
    "A session-scoped Stop hook is now active",
)

# codex writes rollouts under <CODEX_HOME>/sessions/YYYY/MM/DD/. people run more
# than one home (work vs personal), so discovery has to look past ~/.codex.
CODEX_HOME_GLOB = ".codex*"
SKIP_SOURCE = "skip"
CODEX_SKIP_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<user_instructions>",
    "<INSTRUCTIONS>",
)

USER_TEXT_CAP = 24_000
FIRST_PROMPT_CAP = 2_000
TITLE_WEIGHT = 8.0
FIRST_WEIGHT = 12.0
USER_WEIGHT = 3.0
BRANCH_WEIGHT = 7.0
CWD_WEIGHT = 2.0
PHRASE_BONUS = 10.0
BODY_WEIGHT = 1.0
CLAUDE_CATALOG_CAP = 40

# deep scan: raw transcript grep, used when the indexed fields miss. the index
# only holds titles and human turns, so anything said by claude or printed by a
# tool is invisible to scoring without this.
DEEP_TIME_BUDGET = 20.0
DEEP_MIN_HITS = 3
VERTEX_ENV = (
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


@dataclass
class Session:
    path: str
    mtime_ns: int
    size: int
    session_id: str
    cwd: str = ""
    branch: str = ""
    title: str = ""
    first_prompt: str = ""
    user_text: str = ""
    created: str = ""
    modified: str = ""
    humans: int = 0
    project_dir: str = ""
    source: str = "claude"
    agent_home: str = ""
    database: str = ""

    @property
    def short_id(self) -> str:
        return self.session_id.split("-", 1)[0]


@dataclass
class Hit:
    session: Session
    score: float
    evidence: list[str] = field(default_factory=list)
    reason: str = ""


def encode_project_path(cwd: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in cwd)


def decode_project_dir(name: str) -> str:
    """Best-effort display path; encoding is lossy for hyphens vs slashes."""
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name.replace("-", "/")


def projects_root() -> Path:
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    return Path(override).expanduser() if override else PROJECTS_ROOT


def recall_env(name: str, default: str | None = None) -> str | None:
    return os.environ.get("AGENT_RECALL_" + name, os.environ.get("CLAUDE_RECALL_" + name, default))


def cache_path() -> Path:
    override = recall_env("CACHE")
    return Path(override).expanduser() if override else CACHE_PATH


def codex_homes() -> list[Path]:
    """Every codex home on this machine, newest-looking first.

    A shell wrapper that exports CODEX_HOME (a separate work login, say) leaves
    sessions somewhere ~/.codex never sees, so glob for siblings too. Set
    AGENT_RECALL_CODEX_HOMES to a colon-separated list to override, or to an
    empty string to skip codex entirely.
    """
    override = recall_env("CODEX_HOMES")
    if override is not None:
        return list(dict.fromkeys(Path(p).expanduser().resolve() for p in override.split(os.pathsep) if p))
    found: list[Path] = []
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        found.append(Path(env_home).expanduser())
    for candidate in sorted(Path.home().glob(CODEX_HOME_GLOB)):
        if (candidate / "sessions").is_dir() or (candidate / "archived_sessions").is_dir():
            found.append(candidate)
    seen: set[str] = set()
    homes: list[Path] = []
    for home in found:
        key = str(home.resolve()) if home.exists() else str(home)
        if key in seen or not home.is_dir():
            continue
        seen.add(key)
        homes.append(home.resolve())
    return homes


def iter_codex_files(home: Path) -> Iterable[Path]:
    for sub_dir in ("sessions", "archived_sessions"):
        base = home / sub_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.is_file():
                yield path


def pi_roots() -> list[Path]:
    override = recall_env("PI_DIRS")
    if override is not None:
        return [Path(p).expanduser().resolve() for p in override.split(os.pathsep) if p]
    home = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    return [home.expanduser().resolve() / "sessions"]


def opencode_databases() -> list[Path]:
    override = recall_env("OPENCODE_DBS")
    if override is not None:
        return [Path(p).expanduser().resolve() for p in override.split(os.pathsep) if p]
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return [data.expanduser().resolve() / "opencode" / "opencode.db"]


def json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def set_human_prompts(session: Session, humans: list[str]) -> None:
    session.first_prompt = humans[0][:FIRST_PROMPT_CAP] if humans else ""
    session.user_text = "\n".join(humans)[:USER_TEXT_CAP]
    session.humans = len(humans)
    if session.cwd:
        session.project_dir = encode_project_path(session.cwd)


def parse_pi_session(path: Path) -> Session | None:
    try:
        st = path.stat()
        session = Session(str(path), st.st_mtime_ns, st.st_size, "", source="pi")
        humans = []
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                entry = json_object(line)
                if entry.get("type") == "session":
                    session.session_id = str(entry.get("id") or "")
                    session.cwd = str(entry.get("cwd") or "")
                    session.created = str(entry.get("timestamp") or "")
                if isinstance(entry.get("timestamp"), str):
                    session.modified = entry["timestamp"]
                if entry.get("type") == "session_info" and entry.get("name"):
                    session.title = str(entry["name"])
                message = entry.get("message")
                if entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "user":
                    text = _content_to_text(message.get("content"))
                    if text and text.strip():
                        humans.append(text.strip())
        if not session.session_id or not humans:
            return None
        set_human_prompts(session, humans)
        return session
    except OSError:
        return None


def open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    return connection


def timestamp_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def read_opencode_sessions(database: Path, cached: dict[str, Session]) -> list[Session]:
    if not database.is_file():
        return []
    try:
        # Uncheckpointed writes live in the WAL, not in the database file.
        stamps = [database.stat()]
        wal = Path(str(database) + "-wal")
        if wal.exists():
            stamps.append(wal.stat())
        mtime = max(s.st_mtime_ns for s in stamps)
        size = sum(s.st_size for s in stamps)
        sessions = []
        with closing(open_database(database)) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT id, directory, title, time_created, time_updated "
                "FROM session WHERE parent_id IS NULL ORDER BY time_updated DESC"
            )
            for row in rows:
                key = str(database) + "#session=" + row["id"]
                old = cached.get(key)
                if old and old.mtime_ns == mtime and old.size == size:
                    sessions.append(old)
                    continue
                session = Session(
                    key, mtime, size, row["id"], source="opencode", database=str(database),
                    cwd=row["directory"], title=row["title"],
                    created=timestamp_ms(row["time_created"]), modified=timestamp_ms(row["time_updated"]),
                )
                parts = connection.execute(
                    "SELECT m.id, m.data AS message, p.data AS part FROM message m "
                    "JOIN part p ON p.message_id = m.id AND p.session_id = m.session_id "
                    "WHERE m.session_id = ? ORDER BY m.time_created, m.id, p.time_created, p.id",
                    (session.session_id,),
                )
                messages: dict[str, list[str]] = {}
                for part in parts:
                    if json_object(part["message"]).get("role") != "user":
                        continue
                    data = json_object(part["part"])
                    text = data.get("text")
                    if data.get("type") == "text" and not data.get("synthetic") and not data.get("ignored") and isinstance(text, str) and text.strip():
                        messages.setdefault(part["id"], []).append(text.strip())
                set_human_prompts(session, ["\n".join(parts) for parts in messages.values()])
                if session.humans or session.title:
                    sessions.append(session)
        return sessions
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
        print(f"Could not read OpenCode database {database}: {exc}", file=sys.stderr)
        return []


def extract_human_text(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text = _content_to_text(content)
    if not text:
        return None
    stripped = text.lstrip()
    if stripped.startswith(SKIP_PREFIXES):
        return None
    args = COMMAND_ARGS_RE.search(text)
    name = COMMAND_NAME_RE.search(text)
    if args or name:
        body = (args.group(1) if args else "").strip()
        cmd = (name.group(1) if name else "").strip()
        if body:
            return f"/{cmd} {body}".strip() if cmd else body
        if cmd:
            return f"/{cmd}"
        return None
    cleaned = TAG_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _content_to_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    text = "\n".join(parts).strip()
    return text or None


def parse_transcript(path: Path) -> Session | None:
    try:
        st = path.stat()
    except OSError:
        return None
    session_id = path.stem
    if path.parent.name == "subagents":
        return None
    sess = Session(
        path=str(path),
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        session_id=session_id,
        project_dir=path.parent.name,
        modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    humans: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                compact = line.replace(": ", ":")
                cheap = (
                    '"type":"user"' in compact
                    or '"type":"ai-title"' in compact
                    or sess.cwd == ""
                    or sess.branch == ""
                )
                if not cheap:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("cwd"):
                    sess.cwd = entry["cwd"]
                if entry.get("gitBranch"):
                    sess.branch = entry["gitBranch"]
                if not sess.created and entry.get("timestamp"):
                    sess.created = entry["timestamp"]
                if entry.get("timestamp"):
                    sess.modified = entry["timestamp"]
                if entry.get("type") == "ai-title" and entry.get("aiTitle"):
                    sess.title = str(entry["aiTitle"]).strip()
                human = extract_human_text(entry)
                if human:
                    humans.append(human)
    except OSError:
        return None
    if humans:
        sess.first_prompt = humans[0][:FIRST_PROMPT_CAP]
        blob: list[str] = []
        size = 0
        for msg in humans:
            if size >= USER_TEXT_CAP:
                break
            blob.append(msg)
            size += len(msg) + 1
        sess.user_text = "\n".join(blob)[:USER_TEXT_CAP]
        sess.humans = len(humans)
    elif not sess.title:
        return None
    return sess


def extract_codex_human_text(payload: dict) -> str | None:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") in {"input_text", "text"}
        and isinstance(block.get("text"), str)
    ]
    text = "\n".join(parts).strip()
    if not text:
        return None
    # codex replays AGENTS.md and the environment block as the first user turn
    if text.startswith(CODEX_SKIP_PREFIXES) or "<environment_context>" in text[:400]:
        return None
    cleaned = re.sub(r"\s+", " ", TAG_RE.sub(" ", text)).strip()
    return cleaned or None


def parse_codex_rollout(path: Path, home: Path) -> Session | None:
    """Index one codex rollout. Subagent forks are skipped like claude sidechains."""
    try:
        st = path.stat()
    except OSError:
        return None
    sess = Session(
        path=str(path),
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        session_id=path.stem,
        source="codex",
        agent_home=str(home),
        modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    humans: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                compact = line.replace(": ", ":")
                if not (
                    '"session_meta"' in compact
                    or '"role":"user"' in compact
                    or not sess.created
                ):
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if entry.get("type") == "session_meta":
                    source = payload.get("source")
                    if payload.get("thread_source") == "subagent" or (isinstance(source, dict) and "subagent" in source):
                        return None
                    sess.session_id = str(payload.get("id") or sess.session_id)
                    sess.cwd = str(payload.get("cwd") or "")
                    git = payload.get("git")
                    if isinstance(git, dict) and git.get("branch"):
                        sess.branch = str(git["branch"])
                if entry.get("timestamp"):
                    if not sess.created:
                        sess.created = entry["timestamp"]
                    sess.modified = entry["timestamp"]
                human = extract_codex_human_text(payload)
                if human:
                    humans.append(human)
    except OSError:
        return None
    if not humans:
        return None
    if sess.cwd:
        sess.project_dir = encode_project_path(sess.cwd)
    sess.first_prompt = humans[0][:FIRST_PROMPT_CAP]
    blob: list[str] = []
    size = 0
    for msg in humans:
        if size >= USER_TEXT_CAP:
            break
        blob.append(msg)
        size += len(msg) + 1
    sess.user_text = "\n".join(blob)[:USER_TEXT_CAP]
    sess.humans = len(humans)
    return sess


def iter_session_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        try:
            names = os.listdir(project)
        except OSError:
            continue
        for name in names:
            if name.endswith(".jsonl") and len(name) > 10:
                yield project / name


def load_cache(path: Path) -> dict[str, Session]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
        return {}
    out: dict[str, Session] = {}
    items = raw.get("sessions")
    if not isinstance(items, list):
        return {}
    for item in items:
        try:
            sess = Session(**item)
        except TypeError:
            continue
        strings = ("path", "session_id", "source", "cwd", "title", "user_text", "first_prompt",
                   "modified", "created", "branch", "project_dir", "agent_home", "database")
        numbers = ("mtime_ns", "size", "humans")
        if (all(isinstance(getattr(sess, key), str) for key in strings)
                and all(isinstance(getattr(sess, key), int) for key in numbers)):
            out[sess.path] = sess
    return out


def save_cache(path: Path, sessions: dict[str, Session]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "sessions": [asdict(s) for s in sessions.values()],
    }
    fd, tmp = tempfile.mkstemp(prefix="agent-recall-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_index(
    root: Path,
    cache_file: Path,
    rebuild: bool = False,
    codex_homes: Iterable[Path] = (),
    pi_roots: Iterable[Path] = (),
    opencode_dbs: Iterable[Path] = (),
) -> list[Session]:
    cached = {} if rebuild else load_cache(cache_file)
    current: dict[str, Session] = {}
    changed = rebuild
    sources = [(path, "claude", None) for path in iter_session_files(root)]
    for home in codex_homes:
        sources.extend((path, "codex", home) for path in iter_codex_files(home))
    for pi_root in pi_roots:
        sources.extend((path, "pi", None) for path in sorted(pi_root.rglob("*.jsonl")))
    for path, source, home in sources:
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            continue
        old = cached.get(key)
        if old and old.mtime_ns == st.st_mtime_ns and old.size == st.st_size:
            current[key] = old
            continue
        if source == "codex":
            parsed = parse_codex_rollout(path, home)
        elif source == "pi":
            parsed = parse_pi_session(path)
        else:
            parsed = parse_transcript(path)
        # remember the misses too. subagent forks and turn-less rollouts parse to
        # nothing, and re-reading them (some are tens of MB) on every run was the
        # single biggest cost in the index.
        current[key] = parsed or Session(
            path=key, mtime_ns=st.st_mtime_ns, size=st.st_size, session_id="", source=SKIP_SOURCE
        )
        changed = True
    for database in opencode_dbs:
        for session in read_opencode_sessions(database, cached):
            current[session.path] = session
            if cached.get(session.path) != session:
                changed = True
    if not rebuild and set(cached) != set(current):
        changed = True
    if changed:
        save_cache(cache_file, current)
    return [s for s in current.values() if s.source != SKIP_SOURCE]


def tokenize_query(query: str) -> tuple[list[str], list[str]]:
    raw = [m.group(0).lower() for m in TOKEN_RE.finditer(query)]
    terms = [t for t in raw if t not in STOPWORDS and len(t) > 1]
    if not terms:
        terms = [t for t in raw if len(t) > 1]
    phrases: list[str] = []
    for i in range(len(terms) - 1):
        phrases.append(f"{terms[i]} {terms[i + 1]}")
    # keep original 2-grams from the query too (including stopwords) for names
    words = [t.lower() for t in raw]
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i + 1]}"
        if phrase not in phrases and words[i] not in STOPWORDS and words[i + 1] not in STOPWORDS:
            phrases.append(phrase)
    # unique, stable
    seen: set[str] = set()
    uniq_terms = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq_terms.append(t)
    seen_p: set[str] = set()
    uniq_phrases = []
    for p in phrases:
        if p not in seen_p:
            seen_p.add(p)
            uniq_phrases.append(p)
    return uniq_terms, uniq_phrases


def _term_count(text: str, term: str) -> int:
    if not text or not term:
        return 0
    return len(re.findall(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, flags=re.I))


def _field_score(text: str, terms: list[str], phrases: list[str], weight: float) -> tuple[float, list[str]]:
    if not text:
        return 0.0, []
    lower = text.lower()
    score = 0.0
    evidence: list[str] = []
    for phrase in phrases:
        if phrase in lower:
            score += weight * PHRASE_BONUS
            evidence.append(f'phrase "{phrase}"')
    for term in terms:
        count = _term_count(lower, term)
        if count:
            score += weight * (1.0 + math.log1p(count))
    return score, evidence


def _age_decay(session: Session) -> float:
    try:
        modified = datetime.fromisoformat(session.modified.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return 1.0
    try:
        age_days = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 86400.0)
    except (OverflowError, OSError, TypeError):
        return 1.0
    return math.exp(-age_days / 180.0)


def _body_score(path: str, terms: list[str], phrases: list[str], *, deadline: float | None = None) -> tuple[float, list[str]]:
    """Grep one raw transcript. Catches text the index never stored."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return _score_body_lines(stream, terms, phrases, deadline=deadline)
    except OSError:
        return 0.0, []


def _score_body_lines(lines: Iterable[str], terms: list[str], phrases: list[str], *, deadline: float | None = None) -> tuple[float, list[str]]:
    counts = {t: 0 for t in terms}
    seen_phrases: set[str] = set()
    patterns = {t: re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])") for t in terms}
    for line in lines:
        if deadline is not None and time.monotonic() >= deadline:
            break
        lower = line.lower()
        for phrase in phrases:
            if phrase in lower:
                seen_phrases.add(phrase)
        for term, pattern in patterns.items():
            counts[term] += len(pattern.findall(lower))
    score = 0.0
    for phrase in seen_phrases:
        score += BODY_WEIGHT * PHRASE_BONUS
    for term, count in counts.items():
        if count:
            score += BODY_WEIGHT * (1.0 + math.log1p(count))
    if not score:
        return 0.0, []
    matched = sorted(t for t, c in counts.items() if c)
    label = "transcript body"
    if seen_phrases:
        label += ': phrase "' + sorted(seen_phrases)[0] + '"'
    elif matched:
        label += ": " + ", ".join(matched[:3])
    return score, [label]


def _shortlist_tool() -> list[str] | None:
    """Pick the file-matcher. ripgrep is not a nicety here.

    On 2.6 GB of transcripts, BSD grep -Fi takes ~39s and ripgrep ~0.5s, in any
    locale. Without rg the deep scan is slow enough that the time budget cuts it
    short and real matches get missed.
    """
    if shutil.which("rg"):
        return ["rg", "-l", "-i", "-F", "--no-messages"]
    if shutil.which("grep"):
        return ["grep", "-lFi", "-s"]
    return None


def _grep_shortlist(paths: list[str], terms: list[str], *, deadline: float | None = None) -> list[str] | None:
    """Narrow the deep scan before paying for python scoring.

    Multiple -e patterns give union semantics, matching what _body_score would
    accept, and -l stops at the first hit in each file. Returns None when no
    matcher is usable, so the caller falls back to scanning everything.
    """
    tool = _shortlist_tool()
    if not paths or not terms or tool is None:
        return None
    probes: list[str] = []
    for term in terms:
        probes.extend(("-e", term))
    keep: list[str] = []
    chunk = 500
    for start in range(0, len(paths), chunk):
        batch = paths[start : start + chunk]
        remaining = deadline - time.monotonic() if deadline is not None else 120
        if remaining <= 0:
            return keep
        try:
            proc = subprocess.run(
                [*tool, *probes, "--", *batch],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(0.001, remaining),
            )
        except subprocess.TimeoutExpired:
            return keep
        except OSError:
            return None
        if proc.returncode not in (0, 1):
            return None
        keep.extend(line for line in proc.stdout.splitlines() if line)
    return keep


def deep_search(
    sessions: list[Session],
    terms: list[str],
    phrases: list[str],
    *,
    skip: set[str] | None = None,
    budget: float | None = None,
) -> list[Hit]:
    """Newest-first raw scan. Bounded by wall clock so it stays interactive."""
    if not terms and not phrases:
        return []
    skip = skip or set()
    deadline = time.monotonic() + (DEEP_TIME_BUDGET if budget is None else budget)
    found: list[Hit] = []
    ordered = [s for s in sorted(sessions, key=lambda s: s.modified, reverse=True) if s.path not in skip]
    shortlist = _grep_shortlist([s.path for s in ordered if not s.database], terms + phrases, deadline=deadline)
    if shortlist is not None:
        allowed = set(shortlist)
        ordered = [s for s in ordered if s.database or s.path in allowed]
    for sess in ordered:
        if time.monotonic() > deadline:
            break
        if sess.database:
            try:
                with closing(open_database(Path(sess.database))) as connection:
                    rows = connection.execute("SELECT data FROM part WHERE session_id = ?", (sess.session_id,))
                    score, evidence = _score_body_lines((row[0] for row in rows), terms, phrases, deadline=deadline)
            except (OSError, sqlite3.Error):
                continue
        else:
            score, evidence = _body_score(sess.path, terms, phrases, deadline=deadline)
        if score <= 0:
            continue
        found.append(Hit(session=sess, score=score * _age_decay(sess), evidence=evidence))
    found.sort(key=lambda h: h.score, reverse=True)
    return found


def score_session(session: Session, query: str, terms: list[str], phrases: list[str]) -> Hit:
    evidence: list[str] = []
    score = 0.0
    for text, weight, label in (
        (session.title, TITLE_WEIGHT, "title"),
        (session.first_prompt, FIRST_WEIGHT, "first prompt"),
        (session.user_text, USER_WEIGHT, "user messages"),
        (session.branch, BRANCH_WEIGHT, "branch"),
        (session.cwd, CWD_WEIGHT, "cwd"),
    ):
        part, ev = _field_score(text, terms, phrases, weight)
        if part:
            score += part
            if ev:
                evidence.append(f"{label}: " + ", ".join(ev[:2]))
            elif label in {"title", "first prompt", "branch"}:
                evidence.append(label)
    if FIND_RE.search(session.first_prompt or ""):
        score *= 0.12
        evidence.insert(0, "downranked (this looks like a find-session prompt)")
    if any(t in BUILD_TERMS for t in terms) and session.humans >= 8:
        score *= 1.0 + min(session.humans, 200) / 500.0
    score *= _age_decay(session)
    return Hit(session=session, score=score, evidence=evidence)


def in_scope(session: Session, here: str | None, all_projects: bool) -> bool:
    if all_projects or not here:
        return True
    cwd = session.cwd.rstrip("/")
    here_n = here.rstrip("/")
    if cwd == here_n or cwd.startswith(here_n + os.sep):
        return True
    if encode_project_path(here_n) == session.project_dir:
        return True
    return False


def scope_sessions(
    sessions: list[Session], here: str | None, all_projects: bool
) -> tuple[list[Session], bool]:
    scoped = [s for s in sessions if in_scope(s, here, all_projects)]
    if not scoped and here and not all_projects:
        return sessions, True
    return scoped, False


def search(
    sessions: list[Session],
    query: str,
    *,
    here: str | None,
    all_projects: bool,
    limit: int,
) -> tuple[list[Hit], bool]:
    scoped, fell_back = scope_sessions(sessions, here, all_projects)
    if not query.strip():
        hits = [Hit(session=s, score=0.0) for s in scoped]
        hits.sort(key=lambda h: h.session.modified, reverse=True)
        return hits[:limit], fell_back
    terms, phrases = tokenize_query(query)
    hits = [score_session(s, query, terms, phrases) for s in scoped]
    hits = [h for h in hits if h.score > 0]
    hits.sort(key=lambda h: h.score, reverse=True)
    if len(hits) < DEEP_MIN_HITS:
        known = {h.session.path for h in hits}
        hits.extend(deep_search(scoped, terms, phrases, skip=known))
        hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit], fell_back


def candidate_pool(
    sessions: list[Session],
    query: str,
    *,
    here: str | None,
    all_projects: bool,
    limit: int = CLAUDE_CATALOG_CAP,
) -> tuple[list[Hit], bool]:
    scoped, fell_back = scope_sessions(sessions, here, all_projects)
    recent = sorted(scoped, key=lambda s: s.modified, reverse=True)[:limit]
    if not query.strip():
        return [Hit(session=s, score=0.0) for s in recent], fell_back
    terms, phrases = tokenize_query(query)
    scored = [score_session(s, query, terms, phrases) for s in scoped]
    scored.sort(key=lambda h: h.score, reverse=True)
    by_id: dict[str, Hit] = {}
    for hit in scored:
        if hit.score > 0:
            by_id[hit.session.path] = hit
    if len(by_id) < DEEP_MIN_HITS:
        for hit in deep_search(scoped, terms, phrases, skip=set(by_id)):
            by_id.setdefault(hit.session.path, hit)
    for sess in recent:
        by_id.setdefault(sess.path, Hit(session=sess, score=0.0))
    pool = list(by_id.values())
    pool.sort(key=lambda h: (h.score, h.session.modified), reverse=True)
    return pool[:limit], fell_back


def _prompt_lines(session: Session, n: int = 4) -> list[str]:
    lines = [ln.strip() for ln in (session.user_text or "").split("\n") if ln.strip()]
    return [_one_line(ln, 220) for ln in lines[:n]]


def parse_claude_results(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        outer = json.loads(text)
    except json.JSONDecodeError:
        outer = None
    if outer is not None:
        extracted = _results_from_payload(outer)
        if extracted:
            return extracted
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
        extracted = _results_from_payload(parsed)
        if extracted:
            return extracted
    except json.JSONDecodeError:
        pass
    obj = re.search(r"\{[^{}]*\"results\"[^{}]*\[.*\]\s*\}", text, re.S)
    if not obj:
        obj = re.search(r"\{.*\"results\".*\}", text, re.S)
    blob = obj.group(0) if obj else None
    if not blob:
        arr = re.search(r"\[\s*\{.*\"id\".*\}\s*\]", text, re.S)
        blob = arr.group(0) if arr else None
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return []
    return _results_from_payload(parsed)


def _results_from_payload(payload) -> list[dict]:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and payload[0].get("type"):
            for event in payload:
                if not isinstance(event, dict) or event.get("type") != "result":
                    continue
                inner = event.get("result")
                if isinstance(inner, str):
                    return parse_claude_results(inner)
                return _results_from_payload(inner)
            return []
        return _normalize_result_items(payload)
    if isinstance(payload, dict):
        if payload.get("type") == "result" and "result" in payload:
            inner = payload["result"]
            if isinstance(inner, str):
                return parse_claude_results(inner)
            return _results_from_payload(inner)
        if "results" in payload:
            return _normalize_result_items(payload["results"])
    return []


def _normalize_result_items(items) -> list[dict]:
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, str):
            out.append({"id": item, "reason": ""})
            continue
        if not isinstance(item, dict) or item.get("type"):
            continue
        sid = item.get("id")
        if sid:
            out.append({"id": str(sid), "reason": str(item.get("reason") or "").strip()})
    return out


def resolve_hit(by_id: dict[str, Hit], sid: str) -> Hit | None:
    if sid in by_id:
        return by_id[sid]
    matches = [hit for key, hit in by_id.items() if key.lower().startswith(sid.lower())]
    return matches[0] if len(matches) == 1 else None


def picker_prompt_path() -> Path:
    relative = Path("skills/find-session/references/pick-session.md")
    checkout = Path(__file__).resolve().parent / relative
    return checkout if checkout.is_file() else Path(sys.prefix) / "share" / "agent-recall" / relative


def build_picker_prompt(query: str, payload: list[dict], limit: int) -> str:
    path = picker_prompt_path()
    try:
        template = path.read_text(encoding="utf-8")
    except OSError:
        template = (
            "Rank candidate coding-agent sessions for resume.\n\n"
            "## Query\n\n{{QUERY}}\n\n"
            "## Candidates\n\n{{SESSIONS_JSON}}\n\n"
            "## Rules\n\n"
            "Prefer the session that actually did the work, not one that later "
            "asked to find it, and not a drive-by mention.\n\n"
            "## Output\n\n"
            'Return ONLY JSON: {"results": [{"id": "<session-id>", "reason": "<one line>"}]}. '
            "At most {{LIMIT}} results.\n"
        )
    return (
        template.replace("{{QUERY}}", query)
        .replace("{{SESSIONS_JSON}}", json.dumps(payload, indent=2))
        .replace("{{LIMIT}}", str(limit))
    )


def picker_env() -> dict[str, str]:
    """Run the picker on the personal account, not Vertex.

    Vertex refuses the anthropic publisher models unless data sharing is
    enabled on the GCP project, which kills ranking with a 403. Set
    AGENT_RECALL_VERTEX=1 to keep whatever the shell already has.
    """
    env = os.environ.copy()
    if recall_env("VERTEX"):
        return env
    for name in VERTEX_ENV:
        env.pop(name, None)
    return env


def claude_find(
    hits: list[Hit],
    query: str,
    *,
    limit: int,
    claude_bin: str = "claude",
) -> tuple[list[Hit], str | None]:
    if len(hits) < 2:
        return hits[:limit], None
    payload = []
    for number, hit in enumerate(hits, 1):
        s = hit.session
        payload.append(
            {
                "id": f"candidate-{number}",
                "session_id": s.session_id,
                "source": s.source,
                "title": s.title,
                "first_prompt": _one_line(s.first_prompt, 320),
                "user_prompts": _prompt_lines(s),
                "branch": s.branch,
                "cwd": s.cwd,
                "modified": (s.modified or "")[:10],
                "humans": s.humans,
            }
        )
    prompt = build_picker_prompt(query, payload, limit)
    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "text",
        "--max-turns",
        "1",
        "--effort",
        "low",
        "--no-session-persistence",
        "--tools",
        "",
        prompt,
    ]
    model = recall_env("MODEL")
    if model:
        cmd[1:1] = ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=picker_env(),
        )
    except subprocess.TimeoutExpired:
        return hits[:limit], "Claude timed out; falling back to keyword ranking."
    except OSError as exc:
        return hits[:limit], f"Could not run claude ({exc}); falling back to keyword ranking."
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "claude failed").strip().splitlines()
        detail = err[-1] if err else "claude failed"
        return hits[:limit], f"{detail}; falling back to keyword ranking."
    picked = parse_claude_results(proc.stdout)
    if not picked:
        return hits[:limit], "Claude returned no parseable picks; falling back to keyword ranking."
    by_id = {f"candidate-{i}": h for i, h in enumerate(hits, 1)}
    ordered: list[Hit] = []
    seen: set[str] = set()
    for item in picked:
        hit = resolve_hit(by_id, item["id"])
        if not hit or hit.session.path in seen:
            continue
        hit.reason = item.get("reason") or hit.reason
        if hit.reason:
            hit.evidence = [hit.reason, *hit.evidence]
        ordered.append(hit)
        seen.add(hit.session.path)
        if len(ordered) >= limit:
            break
    if not ordered:
        return hits[:limit], "Claude's picks did not match any local session; falling back to keyword ranking."
    return ordered, None


def format_hits(hits: list[Hit], query: str) -> str:
    if not hits:
        q = f' for "{query}"' if query else ""
        return f"No matching sessions{q}."
    lines = []
    for i, hit in enumerate(hits, 1):
        s = hit.session
        when = (s.modified or "")[:10] or "?"
        loc = s.cwd or decode_project_dir(s.project_dir)
        loc = loc.replace(str(Path.home()), "~")
        title = s.title or _one_line(s.first_prompt, 90) or "(no title)"
        branch = f"  [{s.branch}]" if s.branch else ""
        if s.source != "claude":
            branch += f"  ({s.source})"
        if hit.reason:
            lines.append(f"{i:>2}. {s.short_id}  {when}  {loc}{branch}")
        else:
            lines.append(
                f"{i:>2}. {hit.score:6.1f}  {s.short_id}  {when}  {loc}{branch}"
            )
        lines.append(f"    {title}")
        if hit.reason:
            lines.append(f"    {hit.reason}")
        elif hit.evidence:
            lines.append("    " + "; ".join(hit.evidence[:3]))
        lines.append("    " + display_resume(s))
    return "\n".join(lines)


def _one_line(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def hits_json(hits: list[Hit], query: str, fell_back: bool) -> str:
    finder = "claude" if any(h.reason for h in hits) else "keyword"
    return json.dumps(
        {
            "query": query,
            "finder": finder,
            "fell_back_to_all_projects": fell_back,
            "results": [
                {
                    "rank": i,
                    "score": round(h.score, 3),
                    "session_id": h.session.session_id,
                    "title": h.session.title,
                    "first_prompt": h.session.first_prompt,
                    "cwd": h.session.cwd,
                    "branch": h.session.branch,
                    "modified": h.session.modified,
                    "created": h.session.created,
                    "humans": h.session.humans,
                    "evidence": h.evidence,
                    "reason": h.reason,
                    "source": h.session.source,
                    "agent_home": h.session.agent_home,
                    "resume": display_resume(h.session),
                }
                for i, h in enumerate(hits, 1)
            ],
        },
        indent=2,
    )


def resume_command(session: Session, *, fork: bool = False) -> list[str]:
    if session.source == "codex":
        return ["codex", "fork" if fork else "resume", session.session_id]
    if session.source == "pi":
        return ["pi", "--fork" if fork else "--session", session.path]
    if session.source == "opencode":
        return ["opencode", "--session", session.session_id] + (["--fork"] if fork else [])
    args = ["claude", "--resume", session.session_id]
    if fork:
        args.append("--fork-session")
    return args


def resume_env(session: Session) -> dict[str, str]:
    """Codex reads its store from CODEX_HOME; a non-default home must be named."""
    if session.source == "opencode" and session.database:
        return {"XDG_DATA_HOME": str(Path(session.database).parent.parent)}
    if session.source != "codex" or not session.agent_home:
        return {}
    home = Path(session.agent_home)
    if home == Path.home() / ".codex" and not os.environ.get("CODEX_HOME"):
        return {}
    return {"CODEX_HOME": str(home)}


def resume_prompt(session: Session, extra: list[str]) -> list[str]:
    if not extra:
        return []
    prompt = " ".join(extra)
    return ["--prompt", prompt] if session.source == "opencode" else [prompt]


def display_resume(session: Session, *, fork: bool = False, extra: list[str] | None = None) -> str:
    cmd = shlex.join(resume_command(session, fork=fork) + resume_prompt(session, extra or []))
    env = resume_env(session)
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    if session.cwd and Path(session.cwd).is_dir():
        prefix = f"cd {shlex.quote(session.cwd)} && " + prefix
    return prefix + cmd


def resume_session(session: Session, extra: list[str], *, fork: bool) -> int:
    cwd = session.cwd if session.cwd and Path(session.cwd).is_dir() else os.getcwd()
    args = resume_command(session, fork=fork)
    args.extend(resume_prompt(session, extra))
    if not shutil.which(args[0]):
        print(f"{args[0]} CLI not on PATH; install it to resume this session.", file=sys.stderr)
        return 2
    os.environ.update(resume_env(session))
    os.chdir(cwd)
    os.execvp(args[0], args)
    return 1  # unreachable


def pick(hits: list[Hit]) -> Hit | None:
    if not hits:
        return None
    try:
        raw = input(f"Resume which session? [1-{len(hits)} / n] ").strip()
    except EOFError:
        return None
    if raw.lower() in {"", "y", "yes"}:
        return hits[0]
    if raw.lower() in {"n", "no", "q"}:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(hits):
        return hits[int(raw) - 1]
    print("Not a valid choice.", file=sys.stderr)
    return None


def lookup_id(sessions: list[Session], token: str) -> Session | None:
    token = token.lower()
    exact = [s for s in sessions if s.session_id.lower() == token]
    if exact:
        return exact[0] if len(exact) == 1 else None
    prefix = [s for s in sessions if s.session_id.lower().startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="agent-recall",
        description="Find and resume Claude Code, Codex, OpenCode, and Pi sessions by describing what you were doing.",
    )
    p.add_argument("query", nargs="*", help="Natural-language description or session id prefix")
    p.add_argument("--all", action="store_true", help="Search every project, not just this directory")
    p.add_argument("--here", default=os.getcwd(), help="Project directory to search (default: cwd)")
    p.add_argument("--limit", type=int, default=8, help="Max results (default: 8)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--resume", action="store_true", help="Resume the top match")
    p.add_argument("--pick", action="store_true", help="Interactive numbered picker, then resume")
    p.add_argument("--id", action="store_true", help="Print only the top session id")
    p.add_argument(
        "--fast",
        action="store_true",
        help="Use local keyword ranking (also the default)",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--source",
        choices=("all", "claude", "codex", "opencode", "pi"),
        default="all",
        help="Which agent's sessions to search (default: all)",
    )
    p.add_argument("--ranker", choices=("keyword", "claude"), default="keyword",
                   help="Ranking backend (default: keyword; skills use the current agent)")
    p.add_argument("--fork", action="store_true", help="Fork the selected session when resuming")
    p.add_argument("--reindex", action="store_true", help="Rebuild the session index")
    p.add_argument("--print-cmd", action="store_true", help="Print the resume command instead of execing")
    p.add_argument(
        "--prompt",
        nargs=argparse.REMAINDER,
        default=[],
        help="Prompt to send after resume (everything after --prompt)",
    )
    p.add_argument("--version", action="version", version=f"agent-recall {__version__}")
    args = p.parse_args(argv)
    if args.limit < 1:
        p.error("--limit must be at least 1")
    args.here = str(Path(args.here).expanduser().resolve())
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global DEEP_TIME_BUDGET
    try:
        deep_budget = float(recall_env("DEEP_SECONDS", "20"))
        if not math.isfinite(deep_budget) or deep_budget < 0:
            raise ValueError
    except ValueError:
        print("AGENT_RECALL_DEEP_SECONDS must be a non-negative number.", file=sys.stderr)
        return 2
    DEEP_TIME_BUDGET = deep_budget
    query = " ".join(args.query).strip()
    extra = list(args.prompt)
    if extra and extra[0] == "--":
        extra = extra[1:]

    root = projects_root()
    sessions = build_index(
        root, cache_path(), rebuild=args.reindex, codex_homes=codex_homes(),
        pi_roots=pi_roots(), opencode_dbs=opencode_databases(),
    )
    if args.source != "all":
        sessions = [s for s in sessions if s.source == args.source]
    if args.reindex and not query:
        print(f"Indexed {len(sessions)} sessions.")
        return 0

    if query and (UUID_RE.match(query) or query.startswith("ses_")) and " " not in query:
        found = lookup_id(sessions, query)
        if found:
            hits = [Hit(session=found, score=1.0, evidence=["id prefix match"])]
            fell_back = False
        else:
            print(f"No unique session id starting with {query}; try a full id and --source", file=sys.stderr)
            return 1
    else:
        use_claude = (not args.fast) and (args.ranker == "claude" or args.llm) and bool(query)
        if use_claude and shutil.which("claude"):
            pool, fell_back = candidate_pool(
                sessions,
                query,
                here=args.here,
                all_projects=args.all,
            )
            print(f"Asking Claude to pick among {len(pool)} sessions…", file=sys.stderr)
            hits, err = claude_find(pool, query, limit=args.limit)
            if err:
                print(err, file=sys.stderr)
                hits, fell_back = search(
                    sessions,
                    query,
                    here=args.here,
                    all_projects=args.all,
                    limit=args.limit,
                )
        else:
            if use_claude and not shutil.which("claude"):
                print(
                    "claude CLI not on PATH; using keyword ranking. Pass --fast to skip Claude.",
                    file=sys.stderr,
                )
            hits, fell_back = search(
                sessions,
                query,
                here=args.here,
                all_projects=args.all,
                limit=args.limit,
            )

    if args.json:
        print(hits_json(hits, query, fell_back))
        return 0 if hits else 1

    if fell_back:
        print("(no sessions in this directory; searched all projects)", file=sys.stderr)

    if args.id:
        if not hits:
            return 1
        print(hits[0].session.session_id)
        return 0

    text = format_hits(hits, query)
    print(text)
    if not hits:
        return 1

    chosen: Hit | None = None
    if args.pick:
        chosen = pick(hits)
        if chosen is None:
            return 0
    elif args.resume:
        chosen = hits[0]

    if chosen is None:
        return 0

    if args.print_cmd:
        print(display_resume(chosen.session, fork=args.fork, extra=extra))
        return 0
    print(f"Resuming {chosen.session.session_id} in {chosen.session.cwd or '.'}", file=sys.stderr)
    return resume_session(chosen.session, extra, fork=args.fork)


if __name__ == "__main__":
    sys.exit(main())
