"""Pull-only scheduler -> journal live-status overlay (task B).

The journal markdown is the single source of truth for *claims*; the **scheduler**
(`scheduler/{queue,running,done,cancelled}/*.json`) is the single source of truth
for *live job status*. This module joins the two at render time: it reads the
scheduler's job JSONs and groups them by the journal claim they bear on, so a
rendered view (the live panel in `journal_graph.html`) can show "what's queued /
running / finished" WITHOUT that status ever being hand-duplicated in the markdown
(EVIDENCE.md §3 is backlog-only after task B).

HARD CONTRACT (handoff forks 1-3):
  - **Pull-only.** Never writes scheduler state — only the scheduler tick mutates
    queue/running/done. This module opens JSONs read-only.
  - **build_graph stays pure.** This overlay is a render-time join; it is NEVER
    folded into `Reports/journal_graph.json` (which tests assert on). The canonical
    graph must stay a deterministic function of the markdown alone.
  - **Degrades gracefully.** If `scheduler/` is absent (CI, a fresh checkout) the
    overlay is empty and the panel renders nothing — never a crash.

job -> claim attribution (fork 2 + fork 3):
  - `goal.claim` first (set by the request-experiment skill since 2026-06-18);
  - else a best-effort `group -> claim` hint for pre-goals legacy jobs;
  - else the **"unattributed"** bucket. Display-only, never a hard error, never
    backfilled into the job JSONs.
"""

import glob
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))   # pipeline/
SCHED_DIR = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scheduler")

# The scheduler state dirs, in lifecycle order. `cancelled/` is included so a
# preempted/killed job is still visible (display-only).
STATE_DIRS = ("queue", "running", "done", "cancelled")

# Fork 3: pre-2026-06-18 jobs predate the goals feature (`goal: None`) but carry a
# `group`. A small best-effort group->claim hint keeps the big legacy sweeps
# attributed; everything unmatched buckets as "unattributed". Display-only — these
# are NOT authoritative provenance (the authoritative join is goal.claim, and for
# RE rows the `runs/<tag>.log` Source column, see handoff fork 2).
GROUP_CLAIM_HINTS = {
    # Llama precision study -> RE-18/RE-21 (honest Pareto on BabyLlama).
    "llama-pareto": "RE-21", "llama-pareto-wide": "RE-21",
    "llama-honest": "RE-21", "llama-fprand2": "RE-21",
    "llama-fpsec2": "RE-21", "llama-fixedfp": "RE-21",
    "llama-converge": "RE-23",
    # C4 paired static / warm-start / low-budget family -> C4 (RE-20).
    "c4-paired": "C4", "c4-intrand": "C4", "c4-intrand-wide": "C4",
    "c4-warm-wide": "C4", "c4-warm-narrow": "C4", "c4-warm-narrow-v2": "C4",
    "c4-warm-wide-v2": "C4", "c4-lowbudget": "C4", "c4-lowbudget-static": "C4",
    "c4-low-v2": "C4", "c4-low-v2-static": "C4", "seed-reinforce": "C4",
    # INT 2/4/8 rungs -> RE-22.
    "int248-learn": "RE-22", "int248-rand": "RE-22", "int248-learn-b": "RE-22",
    # Fractional exact-gradient dynuni -> RE-19.
    "dynuni-frac-exact": "RE-19",
    # Honest-Pareto fill (token+signed) -> RE-17.
    "honest-dynuni": "RE-17", "fp-anchors": "RE-17", "fp-lowbudget": "RE-17",
}


def _load_jobs(sched_dir):
    """Read every job JSON across the state dirs. Tags each with its `_state`
    (the dir it came from). Skips unreadable/corrupt files silently — a half-written
    JSON mid-tick must never crash a render."""
    jobs = []
    for state in STATE_DIRS:
        d = os.path.join(sched_dir, state)
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    job = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict):
                continue
            job["_state"] = state
            jobs.append(job)
    return jobs


def claim_of(job):
    """job -> claim id (RE-NN / C-NN / H-NN / Q-NN) or None (fork 2/3 ladder)."""
    goal = job.get("goal")
    if isinstance(goal, dict):
        claim = goal.get("claim")
        if claim:
            return claim
    group = job.get("group")
    if group and group in GROUP_CLAIM_HINTS:
        return GROUP_CLAIM_HINTS[group]
    return None


def _slim(job):
    """Display-only projection of a job for the live panel."""
    res = job.get("result") or {}
    return {
        "id": job.get("id"),
        "tag": job.get("tag"),
        "config": job.get("config"),
        "state": job.get("_state"),
        "exit": job.get("exit"),
        "eval_loss": res.get("eval_loss"),
        "log": res.get("log"),
    }


def _empty_buckets():
    return {s: [] for s in STATE_DIRS}


def build_overlay(sched_dir=None):
    """Group live scheduler jobs by the journal claim they bear on.

    Returns a dict::

        {
          "present": bool,          # was scheduler/ there to read?
          "claims": {claim_id: {queue:[...], running:[...], done:[...],
                                cancelled:[...]}},
          "unattributed": {same four buckets},
          "counts": {state: int},   # totals across all claims + unattributed
        }

    Pure read; never mutates `sched_dir`. Absent dir -> present=False, empty groups.
    """
    sched_dir = sched_dir or SCHED_DIR
    overlay = {
        "present": os.path.isdir(sched_dir),
        "claims": {},
        "unattributed": _empty_buckets(),
        "counts": {s: 0 for s in STATE_DIRS},
    }
    if not overlay["present"]:
        return overlay

    for job in _load_jobs(sched_dir):
        state = job["_state"]
        slim = _slim(job)
        claim = claim_of(job)
        if claim:
            bucket = overlay["claims"].setdefault(claim, _empty_buckets())
        else:
            bucket = overlay["unattributed"]
        bucket[state].append(slim)
        overlay["counts"][state] += 1
    return overlay


if __name__ == "__main__":
    import sys

    ov = build_overlay(sys.argv[1] if len(sys.argv) > 1 else None)
    if not ov["present"]:
        print("scheduler/ absent — empty overlay")
        sys.exit(0)
    c = ov["counts"]
    print(f"jobs: {c['queue']} queued · {c['running']} running · "
          f"{c['done']} done · {c['cancelled']} cancelled")
    for claim in sorted(ov["claims"]):
        b = ov["claims"][claim]
        print(f"  {claim}: "
              f"{len(b['queue'])}q {len(b['running'])}r {len(b['done'])}d")
    u = ov["unattributed"]
    print(f"  unattributed: {len(u['queue'])}q {len(u['running'])}r "
          f"{len(u['done'])}d {len(u['cancelled'])}c")
