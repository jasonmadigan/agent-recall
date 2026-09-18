#!/usr/bin/env python3
"""Find and resume Claude Code sessions by what you were doing."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__version__ = "0.4.0"

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".cache" / "claude-recall" / "index-v1.json"
INDEX_VERSION = 2

STOPWORDS = frozenset(
    """
    a an the and or of to in on for with at from by as into over
    this that these those it its my me i i've i'm i'd we you your
    was were be been being is are am do did doing does have had has
    session sessions conversation conversations chat claude code
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
DEEP_TIME_BUDGET = float(os.environ.get("CLAUDE_RECALL_DEEP_SECONDS", "20"))
DEEP_MIN_HITS = 3
VERTEX_ENV = (
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
PICKER_MODEL = "claude-opus-5"


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
    return Path(override) if override else PROJECTS_ROOT


def cache_path() -> Path:
    override = os.environ.get("CLAUDE_RECALL_CACHE")
    return Path(override) if override else CACHE_PATH


def codex_homes() -> list[Path]:
    """Every codex home on this machine, newest-looking first.

    A shell wrapper that exports CODEX_HOME (a separate work login, say) leaves
    sessions somewhere ~/.codex never sees, so glob for siblings too. Set
    CLAUDE_RECALL_CODEX_HOMES to a colon-separated list to override, or to an
    empty string to skip codex entirely.
    """
    override = os.environ.get("CLAUDE_RECALL_CODEX_HOMES")
    if override is not None:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
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
        homes.append(home)
    return homes


def iter_codex_files(home: Path) -> Iterable[Path]:
    for sub_dir in ("sessions", "archived_sessions"):
        base = home / sub_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.is_file():
                yield path


def extract_human_text(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    if entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return None
    content = (entry.get("message") or {}).get("content")
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
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
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
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                if entry.get("type") == "session_meta":
                    if payload.get("thread_source") == "subagent" or payload.get("forked_from_id"):
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
    if raw.get("version") != INDEX_VERSION:
        return {}
    out: dict[str, Session] = {}
    for item in raw.get("sessions") or []:
        try:
            sess = Session(**item)
        except TypeError:
            continue
        out[sess.path] = sess
    return out


def save_cache(path: Path, sessions: dict[str, Session]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "sessions": [asdict(s) for s in sessions.values()],
    }
    fd, tmp = tempfile.mkstemp(prefix="claude-recall-", suffix=".json", dir=str(path.parent))
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
) -> list[Session]:
    cached = {} if rebuild else load_cache(cache_file)
    current: dict[str, Session] = {}
    changed = rebuild
    sources: list[tuple[Path, Path | None]] = [(path, None) for path in iter_session_files(root)]
    for home in codex_homes:
        sources.extend((path, home) for path in iter_codex_files(home))
    for path, home in sources:
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            continue
        old = cached.get(key)
        if old and old.mtime_ns == st.st_mtime_ns and old.size == st.st_size:
            current[key] = old
            continue
        parsed = parse_codex_rollout(path, home) if home else parse_transcript(path)
        # remember the misses too. subagent forks and turn-less rollouts parse to
        # nothing, and re-reading them (some are tens of MB) on every run was the
        # single biggest cost in the index.
        current[key] = parsed or Session(
            path=key, mtime_ns=st.st_mtime_ns, size=st.st_size, session_id="", source=SKIP_SOURCE
        )
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
    except (OverflowError, OSError):
        return 1.0
    return math.exp(-age_days / 180.0)


def _body_score(path: str, terms: list[str], phrases: list[str]) -> tuple[float, list[str]]:
    """Grep one raw transcript. Catches text the index never stored."""
    counts = {t: 0 for t in terms}
    seen_phrases: set[str] = set()
    patterns = {t: re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])") for t in terms}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lower = line.lower()
                for phrase in phrases:
                    if phrase not in seen_phrases and phrase in lower:
                        seen_phrases.add(phrase)
                for term, pat in patterns.items():
                    hits = len(pat.findall(lower))
                    if hits:
                        counts[term] += hits
    except OSError:
        return 0.0, []
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


def _grep_shortlist(paths: list[str], terms: list[str]) -> list[str] | None:
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
        try:
            proc = subprocess.run(
                [*tool, *probes, "--", *batch],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
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
    budget: float = DEEP_TIME_BUDGET,
) -> list[Hit]:
    """Newest-first raw scan. Bounded by wall clock so it stays interactive."""
    if not terms and not phrases:
        return []
    skip = skip or set()
    deadline = time.monotonic() + budget
    found: list[Hit] = []
    ordered = [s for s in sorted(sessions, key=lambda s: s.modified, reverse=True) if s.session_id not in skip]
    shortlist = _grep_shortlist([s.path for s in ordered], terms + phrases)
    if shortlist is not None:
        allowed = set(shortlist)
        ordered = [s for s in ordered if s.path in allowed]
    for sess in ordered:
        if time.monotonic() > deadline:
            break
        score, evidence = _body_score(sess.path, terms, phrases)
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
        known = {h.session.session_id for h in hits}
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
            by_id[hit.session.session_id] = hit
    if len(by_id) < DEEP_MIN_HITS:
        for hit in deep_search(scoped, terms, phrases, skip=set(by_id)):
            by_id.setdefault(hit.session.session_id, hit)
    for sess in recent:
        by_id.setdefault(sess.session_id, Hit(session=sess, score=0.0))
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
    sid_l = sid.lower()
    prefix = sid_l.split("-", 1)[0]
    for key, hit in by_id.items():
        key_l = key.lower()
        if key_l == sid_l or key_l.startswith(sid_l) or sid_l.startswith(key_l.split("-")[0]):
            return hit
        if prefix and key_l.startswith(prefix):
            return hit
    return None


def picker_prompt_path() -> Path:
    return Path(__file__).resolve().parent / "skills" / "find-session" / "references" / "pick-session.md"


def build_picker_prompt(query: str, payload: list[dict], limit: int) -> str:
    path = picker_prompt_path()
    try:
        template = path.read_text(encoding="utf-8")
    except OSError:
        template = (
            "Rank candidate Claude Code sessions for resume.\n\n"
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
    CLAUDE_RECALL_VERTEX=1 to keep whatever the shell already has.
    """
    env = os.environ.copy()
    if env.get("CLAUDE_RECALL_VERTEX"):
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
    for hit in hits:
        s = hit.session
        payload.append(
            {
                "id": s.session_id,
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
        prompt,
    ]
    cmd[1:1] = ["--model", os.environ.get("CLAUDE_RECALL_MODEL") or PICKER_MODEL]
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
    by_id = {h.session.session_id: h for h in hits}
    ordered: list[Hit] = []
    seen: set[str] = set()
    for item in picked:
        hit = resolve_hit(by_id, item["id"])
        if not hit or hit.session.session_id in seen:
            continue
        hit.reason = item.get("reason") or hit.reason
        if hit.reason:
            hit.evidence = [hit.reason, *hit.evidence]
        ordered.append(hit)
        seen.add(hit.session.session_id)
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
        return ["codex", "resume", session.session_id]
    args = ["claude", "--resume", session.session_id]
    if fork:
        args.append("--fork-session")
    return args


def resume_env(session: Session) -> dict[str, str]:
    """Codex reads its store from CODEX_HOME; a non-default home must be named."""
    if session.source != "codex" or not session.agent_home:
        return {}
    home = Path(session.agent_home)
    if home == Path.home() / ".codex":
        return {}
    return {"CODEX_HOME": str(home)}


def display_resume(session: Session, *, fork: bool = False) -> str:
    cmd = shlex.join(resume_command(session, fork=fork))
    env = resume_env(session)
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    return prefix + cmd


def resume_session(session: Session, extra: list[str], *, fork: bool) -> int:
    cwd = session.cwd if session.cwd and Path(session.cwd).is_dir() else os.getcwd()
    if fork and session.source == "codex":
        print("codex has no --fork-session; resuming in place.", file=sys.stderr)
    args = resume_command(session, fork=fork)
    args.extend(extra)
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
        return exact[0]
    prefix = [s for s in sessions if s.session_id.lower().startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ccrecall",
        description="Find a Claude Code session by describing what you were doing. Heuristics shortlist locally, then a picker prompt ranks with Claude.",
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
        help="Skip Claude; rank with keywords only",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--source",
        choices=("all", "claude", "codex"),
        default="all",
        help="Which agent's sessions to search (default: all)",
    )
    p.add_argument("--fork", action="store_true", help="Pass --fork-session when resuming")
    p.add_argument("--reindex", action="store_true", help="Rebuild the session index")
    p.add_argument("--print-cmd", action="store_true", help="Print the resume command instead of execing")
    p.add_argument(
        "--prompt",
        nargs=argparse.REMAINDER,
        default=[],
        help="Prompt to send after resume (everything after --prompt)",
    )
    p.add_argument("--version", action="version", version=f"ccrecall {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    query = " ".join(args.query).strip()
    extra = list(args.prompt)
    if extra and extra[0] == "--":
        extra = extra[1:]

    root = projects_root()
    if not root.is_dir():
        print(f"No Claude Code sessions found at {root}", file=sys.stderr)
        return 2

    homes = [] if args.source == "claude" else codex_homes()
    sessions = build_index(root, cache_path(), rebuild=args.reindex, codex_homes=homes)
    if args.source != "all":
        sessions = [s for s in sessions if s.source == args.source]
    if args.reindex and not query:
        print(f"Indexed {len(sessions)} sessions.")
        return 0

    if query and UUID_RE.match(query) and " " not in query:
        found = lookup_id(sessions, query)
        if found:
            hits = [Hit(session=found, score=1.0, evidence=["id prefix match"])]
            fell_back = False
        else:
            print(f"No session id starting with {query}", file=sys.stderr)
            return 1
    else:
        use_claude = (not args.fast) and bool(query)
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
        cmd = resume_command(chosen.session, fork=args.fork)
        cmd.extend(extra)
        env = resume_env(chosen.session)
        prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
        print(prefix + shlex.join(cmd))
        return 0
    print(f"Resuming {chosen.session.session_id} in {chosen.session.cwd or '.'}", file=sys.stderr)
    return resume_session(chosen.session, extra, fork=args.fork)


if __name__ == "__main__":
    sys.exit(main())
