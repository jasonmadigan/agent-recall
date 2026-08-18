#!/usr/bin/env python3
"""Find and resume Claude Code sessions by what you were doing."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__version__ = "0.1.0"

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".cache" / "claude-recall" / "index-v1.json"
INDEX_VERSION = 1

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

USER_TEXT_CAP = 24_000
FIRST_PROMPT_CAP = 2_000
TITLE_WEIGHT = 8.0
FIRST_WEIGHT = 12.0
USER_WEIGHT = 3.0
BRANCH_WEIGHT = 7.0
CWD_WEIGHT = 2.0
PHRASE_BONUS = 10.0


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

    @property
    def short_id(self) -> str:
        return self.session_id.split("-", 1)[0]


@dataclass
class Hit:
    session: Session
    score: float
    evidence: list[str] = field(default_factory=list)


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


def build_index(root: Path, cache_file: Path, rebuild: bool = False) -> list[Session]:
    cached = {} if rebuild else load_cache(cache_file)
    current: dict[str, Session] = {}
    changed = rebuild
    for path in iter_session_files(root):
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            continue
        old = cached.get(key)
        if old and old.mtime_ns == st.st_mtime_ns and old.size == st.st_size:
            current[key] = old
            continue
        parsed = parse_transcript(path)
        if parsed:
            current[key] = parsed
            changed = True
    if not rebuild and set(cached) != set(current):
        changed = True
    if changed:
        save_cache(cache_file, current)
    return list(current.values())


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
    try:
        modified = datetime.fromisoformat(session.modified.replace("Z", "+00:00"))
        age_days = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 86400.0)
        score *= math.exp(-age_days / 180.0)
    except (ValueError, TypeError, OSError):
        pass
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


def search(
    sessions: list[Session],
    query: str,
    *,
    here: str | None,
    all_projects: bool,
    limit: int,
) -> tuple[list[Hit], bool]:
    scoped = [s for s in sessions if in_scope(s, here, all_projects)]
    fell_back = False
    if not scoped and here and not all_projects:
        scoped = sessions
        fell_back = True
    if not query.strip():
        hits = [Hit(session=s, score=0.0) for s in scoped]
        hits.sort(key=lambda h: h.session.modified, reverse=True)
        return hits[:limit], fell_back
    terms, phrases = tokenize_query(query)
    hits = [score_session(s, query, terms, phrases) for s in scoped]
    hits = [h for h in hits if h.score > 0]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit], fell_back


def llm_rerank(hits: list[Hit], query: str, claude_bin: str = "claude") -> list[Hit]:
    if len(hits) < 2:
        return hits
    payload = []
    for i, hit in enumerate(hits, 1):
        s = hit.session
        payload.append(
            {
                "n": i,
                "id": s.session_id,
                "title": s.title,
                "first_prompt": s.first_prompt[:400],
                "branch": s.branch,
                "cwd": s.cwd,
                "modified": s.modified,
                "humans": s.humans,
                "heuristic_score": round(hit.score, 2),
            }
        )
    prompt = (
        "Pick which Claude Code session(s) the user is asking for.\n"
        f"Query: {query}\n\n"
        "Sessions (JSON):\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return ONLY a JSON array of session ids, best first. "
        "Prefer the session that actually did the work, not one that later "
        "asked to find it. Example: [\"abc-...\", \"def-...\"]"
    )
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return hits
    if proc.returncode != 0:
        return hits
    text = proc.stdout.strip()
    match = re.search(r"\[[^\]]+\]", text, re.S)
    if not match:
        return hits
    try:
        ids = json.loads(match.group(0))
    except json.JSONDecodeError:
        return hits
    if not isinstance(ids, list):
        return hits
    by_id = {h.session.session_id: h for h in hits}
    ordered: list[Hit] = []
    seen: set[str] = set()
    for item in ids:
        sid = str(item)
        hit = by_id.get(sid)
        if not hit:
            for key, cand in by_id.items():
                if key.startswith(sid) or sid.startswith(key.split("-")[0]):
                    hit = cand
                    break
        if hit and hit.session.session_id not in seen:
            ordered.append(hit)
            seen.add(hit.session.session_id)
    for hit in hits:
        if hit.session.session_id not in seen:
            ordered.append(hit)
    return ordered


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
        lines.append(
            f"{i:>2}. {hit.score:6.1f}  {s.short_id}  {when}  {loc}{branch}"
        )
        lines.append(f"    {title}")
        if hit.evidence:
            lines.append("    " + "; ".join(hit.evidence[:3]))
        lines.append(f"    claude --resume {s.session_id}")
    return "\n".join(lines)


def _one_line(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def hits_json(hits: list[Hit], query: str, fell_back: bool) -> str:
    return json.dumps(
        {
            "query": query,
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
                    "resume": f"claude --resume {h.session.session_id}",
                }
                for i, h in enumerate(hits, 1)
            ],
        },
        indent=2,
    )


def resume_session(session: Session, extra: list[str], *, fork: bool) -> int:
    cwd = session.cwd if session.cwd and Path(session.cwd).is_dir() else os.getcwd()
    args = ["claude", "--resume", session.session_id]
    if fork:
        args.append("--fork-session")
    args.extend(extra)
    os.chdir(cwd)
    os.execvp("claude", args)
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
        description="Find and resume a Claude Code session by describing what you were doing.",
    )
    p.add_argument("query", nargs="*", help="Natural-language description or session id prefix")
    p.add_argument("--all", action="store_true", help="Search every project, not just this directory")
    p.add_argument("--here", default=os.getcwd(), help="Project directory to search (default: cwd)")
    p.add_argument("--limit", type=int, default=8, help="Max results (default: 8)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--resume", action="store_true", help="Resume the top match")
    p.add_argument("--pick", action="store_true", help="Interactive numbered picker, then resume")
    p.add_argument("--id", action="store_true", help="Print only the top session id")
    p.add_argument("--llm", action="store_true", help="Rerank top matches with `claude -p`")
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

    sessions = build_index(root, cache_path(), rebuild=args.reindex)
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
        hits, fell_back = search(
            sessions,
            query,
            here=args.here,
            all_projects=args.all,
            limit=args.limit,
        )
        if args.llm and hits:
            hits = llm_rerank(hits, query)

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

    cmd = ["claude", "--resume", chosen.session.session_id]
    if args.fork:
        cmd.append("--fork-session")
    cmd.extend(extra)
    if args.print_cmd:
        print(shlex.join(cmd))
        return 0
    print(f"Resuming {chosen.session.session_id} in {chosen.session.cwd or '.'}", file=sys.stderr)
    return resume_session(chosen.session, extra, fork=args.fork)


if __name__ == "__main__":
    sys.exit(main())
