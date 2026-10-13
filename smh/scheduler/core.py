"""Portable scheduler primitives — no SSH/GPU/queue-path coupling.

SMH-extraction E2b, step 1: the genuinely instance-agnostic helpers pulled out
of `scheduler/sched.py` so a `git subtree split --prefix=smh` ships them. These
are pure functions (time/duration formatting, id sanitation, JSON load, and the
combinatorial slot-fit placement algebra) — they take everything they need as
arguments and touch no module-global queue path. The ROOT-coupled io/state
machine (save_job/all_jobs/deps_done/…) stays in the instance for now and moves
behind a store in a later step; keeping it there preserves the tests'
`monkeypatch.setattr(sched, "ROOT", …)` contract.

`scheduler/sched.py` imports every name back into its own namespace, so
`sched.fit_slots(...)` etc. resolve unchanged.
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

# Canonical queue-state dir names — generic to any SMH filesystem queue (a job
# lives as one json file; its directory IS its status). The instance owns WHERE
# the queue root is (`root` arg); these are the sub-dirs under it.
STATES = ("queue", "running", "done", "failed", "cancelled")
DIRS = STATES + ("cancel", "preempt", "outbox", ".seq")


# ---------------------------------------------------------------- time / id
def future_iso(seconds, base=None):
    """now_iso()-format stamp `seconds` in the future (from `base` or now)."""
    t0 = (datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
          if base else datetime.now(timezone.utc))
    return (t0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])")


def parse_duration(s):
    """'9h' / '30m' / '90s' / '3600' / compound '2h20m' / '1h30m45s' -> seconds
    (int). A bare number is seconds. Raises ValueError on junk."""
    s = str(s).strip().lower()
    mult = {"h": 3600, "m": 60, "s": 1}
    parts = _DUR_RE.findall(s)
    if parts:
        if _DUR_RE.sub("", s).strip():      # leftover after stripping h/m/s terms
            raise ValueError(f"bad duration: {s!r}")
        return int(round(sum(float(n) * mult[u] for n, u in parts)))
    return int(round(float(s)))            # bare number = seconds


def sanitize(s):
    return re.sub(r"[^a-z0-9-]", "-", s.lower())


def load(path):
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------- fs / queue state (root-param)
# Read-side state-machine helpers, parameterized by the queue `root` (and, for
# tag_in_use, the runs dir). The instance passes its live `ROOT`/`RUNS` so the
# tests' monkeypatch(sched.ROOT) still redirects these. The WRITE primitives
# (atomic_write/save_job) + the clock (now_iso/now_compact) stay in the instance
# for now — several tests monkeypatch them to intercept, which only works while
# their callers resolve them through the instance namespace.
def ensure_dirs(root):
    for d in DIRS:
        os.makedirs(os.path.join(root, d), exist_ok=True)


def find_job(root, job_id):
    """Return (path, state) for a job id, or (None, None)."""
    q = glob.glob(os.path.join(root, "queue", f"*_{job_id}.json"))
    if q:
        return q[0], "queue"
    for state in STATES[1:]:
        p = os.path.join(root, state, f"{job_id}.json")
        if os.path.exists(p):
            return p, state
    return None, None


def all_jobs(root):
    """Yield (job, state, path) across every state dir."""
    for state in STATES:
        pat = "*.json"
        for p in sorted(glob.glob(os.path.join(root, state, pat))):
            try:
                yield load(p), state, p
            except (json.JSONDecodeError, OSError):
                continue                     # mid-write temp / race — skip


def next_id(root, session):
    """Per-session counter, bumped under flock so concurrent submits from the
    same session can't collide (read-modify-write is serialized)."""
    session = sanitize(session) or "default"
    seq = os.path.join(root, ".seq", session)
    fd = os.open(seq, os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)        # released on close
        try:
            data = f.read().strip()
            n = int(data) if data else 0
            n += 1
            f.seek(0)
            f.truncate()
            f.write(str(n))
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return f"{session}-{n:04d}"


def tag_in_use(runs, root, tag):
    if glob.glob(os.path.join(runs, f"{tag}.log")):
        return "runs/"
    for job, state, _ in all_jobs(root):
        if job.get("tag") == tag:
            return state
    return None


def _running_jobs(root):
    return [load(p) for p in glob.glob(os.path.join(root, "running", "*.json"))]


def deps_done(root, job):
    """A dependency is satisfied when a finished job matches it by **id OR tag** —
    callers may reference a producer either way (e.g. a chain submitted with
    `--deps <tag>` rather than the autogenerated id). Only `done/` counts
    (success); a failed/cancelled producer never satisfies a dep. Load errors on a
    mid-write done file are skipped, never raised, so this can't wedge the tick."""
    deps = job.get("deps")
    if not deps:
        return True
    done = set()
    for p in glob.glob(os.path.join(root, "done", "*.json")):
        try:
            d = load(p)
        except (json.JSONDecodeError, OSError):
            continue                            # mid-write/race -> skip, retry next tick
        done.add(d["id"])
        if d.get("tag"):
            done.add(d["tag"])
    return all(d in done for d in deps)


def queued_jobs(root):
    """Queued jobs in dispatch order. PAUSED jobs (hold=True) are withheld here —
    this is the single chokepoint the tick reads, so a held job is neither
    dispatched nor offered as a preemption candidate, yet keeps its place in the
    queue and reappears the moment `resume` clears the flag."""
    out = []
    for p in sorted(glob.glob(os.path.join(root, "queue", "*.json"))):
        try:
            job = load(p)
        except (json.JSONDecodeError, OSError):
            continue
        if job.get("hold"):                 # paused -> not dispatchable this tick
            continue
        out.append((job, p))
    out.sort(key=lambda jp: (jp[0]["priority"], jp[0]["created"]))   # urgent first
    return out


# ---------------------------------------------------------------- slot fit
_RANK = {"empty": 0, "mine": 1, "foreign": 2}

# Default gpu_type -> speed rank (higher = faster). Config `gpu_speed` overrides
# per pool; unlisted/unknown type -> 0 (neutral, tightest-fit alone breaks ties).
_GPU_SPEED = {"h100": 2, "a100": 1}


def _box_by_name(cfg, name):
    return next((b for b in cfg["pool"] if b["name"] == name), None)


def _speed_rank(slot):
    """Speed rank of a slot's card (higher=faster). Prefers the value stamped onto
    the slot by compute_slots from cfg['gpu_speed']; falls back to the module
    default map by gpu_type. Unknown -> 0 so speed never disturbs old tightest-fit
    ordering for a type nobody ranked. See docs/DESIGN_speed_aware_placement.md."""
    r = slot.get("speed_rank")
    if r is not None:
        return r
    return _GPU_SPEED.get((slot.get("gpu_type") or "").lower(), 0)


def fit_slots(slots, need, affinity=None, prefer_slow=False):
    """Placeable slots for a `need`-GB job under optional gpu-type affinity,
    ordered by placement preference: empty > mine > foreign, then FASTER gpu_type,
    then tightest-fit. Preferring speed keeps heavy compute-bound trains off a
    slower card that merely fits tighter; tightest-fit only breaks speed ties, so
    headroom is still reserved *within* a speed class (two H100s -> tighter wins).
    affinity (e.g. 'h100'/'a100') restricts to boxes of that gpu_type; None=any.

    prefer_slow FLIPS the speed term to ascending (slower gpu_type first), keeping
    fast cards open for the heavy jobs that actually gain from them — the caller
    sets it for short jobs (ISSUE_scheduler_speed_lookahead heuristic 3). Only the
    speed direction changes; state rank (empty>mine>foreign) and the tightest-fit
    tiebreak are untouched, so a short job still prefers an idle slow card over a
    busy one. Default False = today's faster-first ordering (cold-start jobs, whose
    length is unknown, always take this path — never pushed to a slow card blind)."""
    speed_dir = 1 if prefer_slow else -1
    return sorted([s for s in slots if s["avail_gb"] >= need
                   and (not affinity or s.get("gpu_type") == affinity)],
                  key=lambda s: (_RANK.get(s["state"], 3), speed_dir * _speed_rank(s),
                                 s["avail_gb"]))


def fit_multi(slots, need, n_gpu, affinity=None, prefer_slow=False):
    """N same-box slots for one DDP job (each card >= need GB), or None.

    Single-node only: all N cards must live on one box (cross-box DDP would need
    WAN rendezvous — out of scope). Picks the box whose N best-fitting eligible
    cards are collectively the most idle (sum of state rank, then tightest fit),
    so we prefer an all-empty box and never scatter a job across boxes. n_gpu==1
    reduces to fit_slots()[:1]. prefer_slow threads through to fit_slots (short-job
    inverse-speed preference — heuristic 3); it only reorders single-card fits, DDP
    box selection is speed-agnostic (state rank + count)."""
    elig = fit_slots(slots, need, affinity, prefer_slow)   # already preference-ordered
    if n_gpu <= 1:
        return elig[:1] or None
    by_box = {}
    for s in elig:
        by_box.setdefault(s["remote_name"], []).append(s)
    best = None
    for box_slots in by_box.values():
        if len(box_slots) < n_gpu:
            continue
        pick = box_slots[:n_gpu]                     # fit_slots order = best first
        score = sum(_RANK.get(s["state"], 3) for s in pick)
        if best is None or score < best[0]:
            best = (score, pick)
    return best[1] if best else None


def _slot_for(slots, remote_name, gpu):
    for s in slots:
        if s["remote_name"] == remote_name and s["gpu"] == gpu:
            return s
    return None
