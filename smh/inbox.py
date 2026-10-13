"""Per-session failure inbox — the owner-notify channel.

A scheduler tick that harvests a *terminal* FAILED job writes a compact,
de-duplicated "cluster" notice into the OWNING session's inbox; that Claude
session surfaces unseen notices on its next turn (a SessionStart hook running
`python -m smh.cli inbox --surface`). Formalizes a loop a human/agent was doing
by hand: triage the failure, find the owner, hand them a fix-ready diagnostic.

File: ``<root>/.smh/inbox/<session>.jsonl`` — one JSON object per line, one line
per ``(kind, group, error_class)`` cluster so a trio of sibling failures with one
root collapses to ONE "3 cegt evals OOM" notice, not three. ``.smh/`` is
gitignored (``config.STATE_SUBDIR``), so this never pollutes git or a render.

Design + locked decisions: ``docs/DESIGN_owner_failure_notify.md``.

Invariants:
- **Never-raise on the WRITE path is the CALLER's job.** ``sched.notify`` already
  swallows every error; ``deliver`` is called from inside that guard. The helpers
  here keep their own I/O defensive (atomic writes, malformed lines skipped) but do
  not promise to swallow a programming error — that surfaces in tests.
- Notices are **descriptive only** (error class + one grepped log line + ids/tags/
  count/log-path). No suggested-fix string — a wrong suggestion erodes trust; the
  owner infers the fix (locked decision, 2026-07-16).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from smh import config

# Coalesce/GC windows (seconds). Overridable per call; defaults match the design.
DEFAULT_DEDUPE_TTL = 900          # merge into an unseen same-key row only this fresh
DEFAULT_INBOX_TTL = 604800        # drop any row older than this (7 d)
SEEN_RETENTION = 86400            # drop a seen row this long after it was surfaced


def inbox_dir(root: str) -> str:
    return config.state_path(root, "inbox")


# ---------------------------------------------------------------- session registry
# The binding the manual process had to discover by hand: owner token ($LDP_SESSION,
# = job["session"]) -> its tmux pane/window. Written at submit time (the submit runs
# in the owner's shell, which carries $TMUX_PANE), so the scheduler can later resolve
# a live window to nudge. Phase 3; the inbox itself never needs this.
def sessions_dir(root: str) -> str:
    return config.state_path(root, "sessions")


def session_registry_path(root: str, session: str) -> str:
    return os.path.join(sessions_dir(root), f"{config._slug(session)}.json")


def register_session(root: str, session: str, *, pane=None, window=None,
                     pid=None, now: str) -> dict:
    """Best-effort: record/refresh <session> -> {pane, window, pid, at}. Overwrites
    (a session's newest submit wins). Returns the record."""
    rec = {"session": config._slug(session), "pane": pane, "window": window,
           "pid": pid, "at": now}
    path = session_registry_path(root, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_sess_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec


def load_session(root: str, session: str):
    """Read the <session> registry record, or None if unregistered/unreadable."""
    try:
        with open(session_registry_path(root, session), "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def inbox_path(root: str, session: str) -> str:
    """The owning session's inbox file. ``session`` is already sanitized upstream
    (``sched.sanitize`` / ``$LDP_SESSION``); we slug defensively so a stray token
    can never escape the inbox dir."""
    return os.path.join(inbox_dir(root), f"{config._slug(session)}.jsonl")


# ---------------------------------------------------------------- time helpers
def _parse_iso(s: str) -> datetime:
    """Parse an ``…Z`` iso stamp to an aware UTC datetime; unparseable -> epoch
    (treated as 'infinitely old' so GC drops it and coalesce never merges into it)."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _age_s(now: str, then: str) -> float:
    return (_parse_iso(now) - _parse_iso(then)).total_seconds()


# ---------------------------------------------------------------- file I/O
def _load(path: str) -> list[dict]:
    """Read the JSONL inbox -> list of dicts. A malformed line is skipped, never
    fatal (a half-written line from a crash must not brick the whole inbox)."""
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except FileNotFoundError:
        pass
    return rows


def _dump(path: str, rows: list[dict]) -> None:
    """Atomically rewrite the inbox (temp in same dir + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_inbox_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- GC + dedupe
def _gc(rows: list[dict], now: str,
        inbox_ttl: int = DEFAULT_INBOX_TTL) -> list[dict]:
    """Self-trimming: drop rows older than ``inbox_ttl``, and seen rows retained
    past ``SEEN_RETENTION``. Keeps the inbox bounded with no cron."""
    kept = []
    for r in rows:
        age = _age_s(now, r.get("at", ""))
        if age > inbox_ttl:
            continue
        if r.get("seen") and _age_s(now, r.get("seen_at", r.get("at", ""))) > SEEN_RETENTION:
            continue
        kept.append(r)
    return kept


def _key(r: dict) -> tuple:
    return (r.get("kind"), r.get("group"), r.get("error_class"))


def deliver(root: str, *, session: str, kind: str, group, error_class: str,
            ids, tags, detail: str = "", log: str = "", now: str,
            dedupe_ttl: int = DEFAULT_DEDUPE_TTL,
            inbox_ttl: int = DEFAULT_INBOX_TTL) -> dict:
    """Upsert a cluster notice into ``session``'s inbox and return the stored row.

    Clustering (property (a): sibling failures share one root): the key is
    ``(kind, group, error_class)``. If a still-**unseen** row with that key exists
    and is fresher than ``dedupe_ttl``, we MERGE into it (union ids/tags, bump
    count, refresh ``at``) — so a burst of seeds collapses to one line. Otherwise a
    new line is appended (a recurrence AFTER the owner already read the cluster is
    genuinely new information, property (b))."""
    ids = list(ids or [])
    tags = list(tags or [])
    path = inbox_path(root, session)
    rows = _gc(_load(path), now, inbox_ttl)
    want = (kind, group, error_class)
    target = None
    for r in rows:
        if _key(r) == want and not r.get("seen") and _age_s(now, r.get("at", "")) <= dedupe_ttl:
            target = r
            break
    if target is None:
        target = {"kind": kind, "session": config._slug(session), "group": group,
                  "error_class": error_class, "ids": [], "tags": [],
                  "count": 0, "detail": detail, "log": log,
                  "at": now, "first_at": now, "seen": False}
        rows.append(target)
    # merge (dedup ids/tags, preserve order)
    for i in ids:
        if i not in target["ids"]:
            target["ids"].append(i)
    for t in tags:
        if t and t not in target["tags"]:
            target["tags"].append(t)
    target["count"] = len(target["ids"])
    target["at"] = now
    if detail:
        target["detail"] = detail
    if log:
        target["log"] = log
    _dump(path, rows)
    return target


# ---------------------------------------------------------------- reader side
def read(root: str, session: str, *, unseen_only: bool = True) -> list[dict]:
    rows = _load(inbox_path(root, session))
    return [r for r in rows if not r.get("seen")] if unseen_only else rows


def mark_seen(root: str, session: str, now: str,
              inbox_ttl: int = DEFAULT_INBOX_TTL) -> int:
    """Flip every unseen row to seen (stamping ``seen_at``), GC, rewrite. Returns
    the number newly flipped. Idempotent: a second call flips nothing."""
    path = inbox_path(root, session)
    rows = _load(path)
    n = 0
    for r in rows:
        if not r.get("seen"):
            r["seen"] = True
            r["seen_at"] = now
            n += 1
    _dump(path, _gc(rows, now, inbox_ttl))
    return n


def format_notice(r: dict) -> str:
    """One compact human block for a cluster notice (surfaced on the owner's turn)."""
    tags = r.get("tags") or []
    ids = r.get("ids") or []
    head = f"⚠ {r.get('count', len(ids))}× {r.get('kind', 'failed').upper()}"
    what = ", ".join(tags[:4]) + (" …" if len(tags) > 4 else "") if tags else \
           ", ".join(ids[:4]) + (" …" if len(ids) > 4 else "")
    lines = [f"{head}  [{r.get('group') or '—'}]  {what}".rstrip(),
             f"    class: {r.get('error_class') or 'unknown'}"]
    if r.get("detail"):
        lines.append(f"    {r['detail']}")
    if r.get("log"):
        lines.append(f"    log: {r['log']}")
    if ids:
        lines.append(f"    ids: {', '.join(ids)}")
    return "\n".join(lines)
