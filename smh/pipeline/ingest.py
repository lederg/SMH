"""Phase 1 of result ingestion (docs/HANDOFF_result_ingestion.md): the
watermark-aware "what finished since last time" digest engine.

Pure compute — reads scheduler/done/*.json (the finish *events*) and Reports/
figdata.py (the honest *measurements*), emits pipeline/ingest_digest.{md,json}.
Writes NOTHING into ground truth (no journal, no plots). Advancing the watermark
is a separate, explicit `--accept` step so a re-run keeps re-surfacing the same
set until acted on (idempotent).

Two load-bearing rules from the handoff:
  1. NEVER trust done/*.json `result.realized_b2` — it uses the scheduler's
     decoder, which over-counts some grids (~2× for int248; the `_extract_b2`
     bug). Recompute via figdata._b2(tag, figdata.decode_for(tag)) — honest
     signed-levels, tail-mean.
  2. Join key = goal.claim when present, else job["group"], else session — so
     this works pre-goals (HANDOFF_experiment_goals) and sharpens once goals land.
"""
import argparse
import glob
import json
import math
import os
import re
import statistics
import sys

from smh.config import resolve_root, state_path, current_project
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from smh import journal_xref as jx  # noqa: E402  (load_segments — the co-located ledger)
from smh.pipeline.measure import measurement_for  # noqa: E402  (adapter-4: per-project b²/eval)
from smh.pipeline.config_sig import config_sig_for  # noqa: E402  (adapter-5: per-project config signature)

# The default (bare-id) project. Non-default co-located projects carry a
# `project:id` goal.claim; the default project's claims stay bare (LDP's H8, RE-23).
DEFAULT_PROJECT = "ldp"

DONE = os.path.join(ROOT, "scheduler", "done")
FAILED = os.path.join(ROOT, "scheduler", "failed")
STATE = state_path(ROOT, "ingest_state.json")
DIGEST_MD = state_path(ROOT, "ingest_digest.md")
DIGEST_JSON = state_path(ROOT, "ingest_digest.json")
ROUND_STATE = state_path(ROOT, "round_state.json")

# A move counts as "material" (worth a journal headline edit) when the mean
# learned−floor gap clears this and its sign is consistent across the points.
MATERIAL = 0.02


# ── watermark ────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE):
        try:
            s = json.load(open(STATE))
            s.setdefault("seen_ids", [])
            s.setdefault("last_ended", "")
            return s
        except (ValueError, OSError):
            pass
    return {"seen_ids": [], "last_ended": ""}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=2, sort_keys=True)


def _open_rounds():
    if not os.path.exists(ROUND_STATE):
        return {}
    try:
        d = json.load(open(ROUND_STATE))
    except (ValueError, OSError):
        return {}
    return {c: v for c, v in d.items() if v.get("status") != "queued"}


def open_rounds(blocking_only=False):
    """Claims with a `round` started (journal entry drafted) but whose experiments
    were never queued (propose.py round --finish). A FULL round (results in hand,
    finished in the same session) must close before the watermark advances — those
    block --accept. A HALF round (new hypothesis, no results; experiments may be
    days out behind new code) does NOT block — it only surfaces as a digest reminder
    (the watermark consumes RESULTS, which a half-round has none of)."""
    return sorted(c for c, v in _open_rounds().items()
                  if not (blocking_only and v.get("half")))


def pending_half_rounds():
    """Open half-rounds — declared hypotheses whose experiments aren't queued yet.
    Surfaced as a reminder, never a block."""
    return sorted(c for c, v in _open_rounds().items() if v.get("half"))


# ── figdata family maps (orphan classification + per-model floors) ─────────────
def _figdata():
    """Lazily import the instance's figure-family module (LDP: `Reports/figdata.py`).
    Instance-specific AND outside the `smh/` package (`Reports/` is gitignored), so
    it is imported on demand — never at module load — so `import smh.pipeline.ingest`
    keeps working in a packaged/subtree-split checkout that ships no figdata. Only
    `_plot_families` (orphan classification) touches it; LDP behavior is unchanged.
    A stranger's SMH repo puts its own figdata on `sys.path`."""
    reports = os.path.join(ROOT, "Reports")
    if reports not in sys.path:
        sys.path.insert(0, reports)
    import figdata
    return figdata


def __getattr__(name):
    """Expose `figdata` as a LAZY module attribute (PEP 562): `ingest.figdata`
    resolves on first access via `_figdata()` without importing it at module load,
    so external callers/tests keep referencing `ingest.figdata` while a subtree-split
    checkout still imports this module cleanly. Internal code uses `_figdata()`
    directly (a bare global name never triggers module `__getattr__`)."""
    if name == "figdata":
        return _figdata()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _plot_families():
    """Map every currently-plotted tag -> (family_name, model, is_baseline) by
    unioning the RESOLVED tag lists of both honest-Pareto group functions. A run
    whose log is on disk and matches a family pattern is already globbed here, so
    membership is exact — no pattern re-derivation. Returns (tag2fam, baselines).
      tag2fam[tag]   = {"family","model","baseline"}
      baselines[model] = [(x_effbits, y_eval), ...]  the matched-budget floor
    """
    figdata = _figdata()
    tag2fam, baselines = {}, {"owt": [], "llama": [], "llama_conv": []}
    for model, data in (("owt", figdata.pareto_honest_data()),
                        ("llama", figdata.pareto_llama_data()),
                        ("llama_conv", figdata.pareto_llama_conv_data())):
        for g in data["groups"]:
            for p in g["points"]:
                tag2fam[p["tag"]] = {"family": g["name"], "model": model,
                                     "baseline": g.get("baseline", False),
                                     "x": p["x"]}   # plotted eff bits (tag- or β-derived)
                if g.get("baseline"):
                    baselines[model].append((p["x"], p["y"]))
    for m in baselines:
        baselines[m].sort()
    return tag2fam, baselines


def _floor_at(baseline_pts, x):
    """Linear-interpolate the random/static floor eval at effective-bits x. None
    outside the covered range or with <2 baseline points (can't bracket)."""
    if x is None or len(baseline_pts) < 2:
        return None
    xs = [p[0] for p in baseline_pts]
    if x < xs[0] or x > xs[-1]:
        return None
    for (x0, y0), (x1, y1) in zip(baseline_pts, baseline_pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return (y0 + y1) / 2
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


# ── measurement (HONEST — never the scheduler's stored b²) ─────────────────────
def _leaf_values(cat, tag):
    """A non-`eval_loss` catalog's leaves for `tag` (e.g. BitDistiller's WIKI_PPL +
    commonsense accs), {kind: value} over the present ones. Empty for an
    `eval`/`b²`-shaped catalog (LDP/FracBits have no `LEAF_KINDS` attr) — so the
    fallback is inert on those and the LDP path stays byte-identical. Gated on the
    attribute, NOT the project name."""
    kinds = getattr(cat, "LEAF_KINDS", None)
    if not kinds:
        return {}
    out = {}
    for k in kinds:
        try:
            v = cat.value(tag, k)
        except (KeyError, OSError, ValueError, TypeError):
            v = None
        if v is not None:
            out[k] = v
    return out


def measure(tag, project=None):
    """Honest eval + effective bits for a finished run, via the project's
    MeasurementCatalog (adapter 4) — NOT figdata by name, so a co-located project
    (FracBits) decodes its own β. Returns {eval, bits, b2, synced, leaves}.
    synced=False when the run's evidence isn't in runs/ yet (remote finish not
    rsynced). `leaves` is None for an eval-loss project; for a leaf-kind project
    (BitDistiller) eval/bits/b² are None and `leaves` carries the per-kind values."""
    cat = measurement_for(project)
    ev = cat.eval(tag)
    if ev is None:
        # eval-loss project (LDP/FracBits): no eval -> not synced (unchanged).
        # leaf-kind project (BitDistiller): fall back to its LEAF_KINDS so the
        # honest digest can summarise a run with no `eval_loss`/learned-β leaf.
        leaves = _leaf_values(cat, tag)
        if leaves:
            return {"eval": None, "bits": None, "b2": None, "synced": True,
                    "leaves": leaves}
        return {"eval": None, "bits": None, "b2": None, "synced": False,
                "leaves": None}
    try:                       # the b² decoder can raise on empty/partial beta rows
        b2 = cat.b2(tag)
    except (ZeroDivisionError, ValueError, OSError, KeyError, TypeError):
        b2 = None
    bits = math.sqrt(b2) if b2 else None
    return {"eval": ev, "bits": bits, "b2": b2, "synced": True, "leaves": None}


def claim_of(job):
    """Join key: goal.claim (goals handoff) -> group -> session. Read-with-fallback
    so this works before goals land and sharpens automatically after."""
    g = job.get("goal") or {}
    return g.get("claim") or job.get("group") or job.get("session") or "unclaimed"


def project_of(claim):
    """The namespace of a (possibly namespaced) claim id: 'fracbits:H1' -> 'fracbits';
    a bare 'H8'/'fracbits_t3b2' group -> the current-project default. The co-located
    ledger stamps non-default projects as 'project:id'; the default project stays
    bare. The bare fallback routes through the one session-default resolver
    (config.current_project), so a pinned session redefines it (§2.1); absent any
    pin it is DEFAULT_PROJECT ('ldp'), byte-identical to before."""
    return claim.split(":", 1)[0] if ":" in claim else current_project(ROOT)


def kind_of(job):
    """goal.kind (science|scaffold) when goals are populated, else None. Routes the
    recommendation: scaffold (infra runs like ddp/lagrangian) -> §4 changelog, never
    an RE row or a plot family. None (pre-goals) is treated as science-eligible — we
    can't prove a job is scaffold, so we don't silently drop it."""
    return (job.get("goal") or {}).get("kind")


# ── scan ───────────────────────────────────────────────────────────────────────
def _read_jobs(d):
    out = []
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            out.append(json.load(open(f)))
        except (ValueError, OSError):
            continue
    return out


def collect(state):
    """Newcomers since the watermark, classified. Returns (claims, failures)."""
    seen = set(state["seen_ids"])
    tag2fam, baselines = _plot_families()

    done = [j for j in _read_jobs(DONE)
            if j.get("status") == "done" and j.get("exit") == 0
            and j.get("id") not in seen]
    failures = [{"id": j["id"], "tag": j.get("tag"), "claim": claim_of(j),
                 "ended": j.get("ended"), "exit": j.get("exit")}
                for j in _read_jobs(FAILED) if j.get("id") not in seen] + \
               [{"id": j["id"], "tag": j.get("tag"), "claim": claim_of(j),
                 "ended": j.get("ended"), "exit": j.get("exit")}
                for j in _read_jobs(DONE)
                if j.get("status") == "done" and j.get("exit") not in (0, None)
                and j.get("id") not in seen]

    claims = {}
    for j in done:
        tag = j.get("tag")
        fam = tag2fam.get(tag)
        m = measure(tag, project_of(claim_of(j))) if tag else {
            "eval": None, "bits": None, "b2": None, "synced": False}
        if m["bits"] is None and fam is not None:   # uni-style: bits from the tag
            m["bits"] = fam.get("x")
        if fam is None:
            plot = "unsynced" if not m["synced"] else "orphan"
        else:
            plot = "plotted"
        model = fam["model"] if fam else None
        floor = _floor_at(baselines.get(model, []), m["bits"]) if model else None
        delta = (m["eval"] - floor) if (m["eval"] is not None
                                        and floor is not None) else None
        # adapter-5: the result's authoritative CONFIG, machine-derived from the
        # job record (never the mislabelable prose). `config_invalid` is a ⚠ reason
        # iff the config is in the project's known-collapse registry (per-token fp4).
        csig = config_sig_for(project_of(claim_of(j))).signature(j)
        rec = {"id": j["id"], "tag": tag, "config": j.get("config"),
               "ended": j.get("ended"), "context": j.get("context"),
               "kind": kind_of(j),
               "eval": m["eval"], "bits": m["bits"], "b2": m["b2"],
               "leaves": m.get("leaves"),   # leaf-kind project (BitDistiller): per-kind values
               # config_sig is DISPLAY (sweep-augmented below, mutated in place);
               # config_sig_plain stays the canonical catalog string — the config
               # GATE must match prose tokens against THIS, or no token can pass
               # once a heterogeneous claim (PTQ+KD arms) marks most knobs swept.
               "config_sig": csig.string, "config_sig_plain": csig.string,
               "config_invalid": csig.invalid,
               "config_knobs": csig.knobs, "config_raw": csig.raw,
               "synced": m["synced"], "plot": plot,
               "family": fam["family"] if fam else None, "model": model,
               "baseline": fam["baseline"] if fam else None,
               "floor": floor, "delta_vs_floor": delta}
        claims.setdefault(claim_of(j), []).append(rec)
    # variance-aware: within each claim, promote any SWEPT knob (an LR/warmup
    # sweep the curated string would hide) onto every arm's config_sig, so the
    # swept axis is visible and arms stay distinguishable. ≥2 non-noise swept axes
    # = a confounded ladder (Q12's granularity+budget) — flagged on each rec.
    from smh.pipeline.config_sig import augment_with_sweeps
    for recs in claims.values():
        swept = augment_with_sweeps(recs)
        for r in recs:
            r["config_swept"] = swept
            r["config_confound"] = swept if len(swept) >= 2 else []
    return claims, failures


def journal_claims():
    """Known claim IDs across every co-located project, namespaced to match
    `goal.claim`: the DEFAULT project's ids stay bare ('H8','RE-23' — LDP jobs
    carry bare goal.claim), a non-default project's are 'project:id'
    ('fracbits:H1'). So a known FracBits claim is recognized, and the default
    project's 'H1' never collides with 'fracbits:H1'. `H[1-9][0-9]?` not `H\\d+`
    so 'H100' (the GPU) doesn't leak in — the regex gotcha from the goals handoff."""
    if getattr(journal_claims, "_c", None) is not None:
        return journal_claims._c
    import re
    # `(?<![\w:])` rejects an id that is part of a `project:id` CROSS-reference
    # (e.g. 'ldp:H8' mentioned inside the fracbits segment) — those are links to
    # ANOTHER project's claim, not a definition in this one. Without it a
    # co-located segment over-claims every foreign id it links to.
    pat = r"(?<![\w:])(RE-\d+|H[1-9][0-9]?|C\d+|Q\d+)\b"
    c = set()
    try:
        for proj, txt in jx.load_segments(ROOT):
            ids = set(re.findall(pat, txt))
            c |= ids if proj == DEFAULT_PROJECT else {f"{proj}:{i}" for i in ids}
    except OSError:
        c = set()
    journal_claims._c = c
    return c


def _agg(points):
    """Aggregate move for a claim's LEARNED points: n, mean Δ vs floor, σ. Only
    over non-baseline points with a computable delta."""
    ds = [p["delta_vs_floor"] for p in points
          if p["delta_vs_floor"] is not None and not p.get("baseline")]
    if not ds:
        return {"n": 0, "mean_delta": None, "sigma": None, "sign_consistent": None}
    mean = sum(ds) / len(ds)
    same = sum(1 for x in ds if (x < 0) == (mean < 0))
    return {"n": len(ds), "mean_delta": mean,
            "sigma": statistics.pstdev(ds) if len(ds) > 1 else 0.0,
            "sign_consistent": same / len(ds)}


def recommend(claim, points, agg):
    """Analyze a claim's new points → a recommended action + options for the user
    to discuss. Heuristic + computable (the Phase-2 LLM/human writes the actual
    prose); this frames the decision. Returns {action, confidence, rationale,
    options:[...]}.
      - orphan present        → new family: needs a figdata tuple + likely new RE row
      - unsynced/no-measure   → pull logs from the remotes, re-run
      - known claim, material → tighten that RE row (n, Δ, σ move)
      - known claim, flat     → reinforces only; bump n, no headline change
      - unknown claim, material→ candidate NEW finding (no journal row yet)
      - unknown claim, flat   → park; not journal-worthy yet
    """
    # scaffold/infra (goal.kind=scaffold) routes to the §4 changelog, NEVER an RE
    # row or a plot family — short-circuit before the orphan/material branches so
    # ddp/lagrangian/smoke runs can't over-trigger new-family/new-finding.
    kind = next((p.get("kind") for p in points if p.get("kind")), None)
    if kind == "scaffold":
        return {"action": "park", "confidence": "low",
                "rationale": f"'{claim}' is scaffold/infra (goal.kind=scaffold) — "
                "belongs in the §4 changelog (C-entry), not an RE row or a plot family.",
                "options": ["note in the §4 changelog (C-entry)",
                            "no journal change", "hold"]}
    known = claim in journal_claims()
    proj = project_of(claim)
    orphans = [p["tag"] for p in points if p["plot"] == "orphan"]
    unsynced = [p["tag"] for p in points if p["plot"] == "unsynced"]
    n, md, sig = agg["n"], agg["mean_delta"], agg["sign_consistent"]
    material = (md is not None and abs(md) >= MATERIAL and n >= 2
                and (sig or 0) >= 0.75)
    conf = "low"
    if n >= 3 and (sig or 0) == 1.0 and (agg["sigma"] or 1) < abs(md or 0):
        conf = "high"
    elif material:
        conf = "med"

    # A co-located non-default project (FracBits) is EXPECTED to be orphan to LDP's
    # figdata families — its tags must NOT be pushed into LDP's figure catalog
    # (that's the project's own concern, plotted in its own Pareto). So skip the
    # new-family branch and route measured runs straight to a journal edit in the
    # project's own EVIDENCE file.
    if proj != DEFAULT_PROJECT:
        # "measured" includes leaf-kind points (BitDistiller has no eval_loss leaf —
        # its evidence is WIKI_PPL + commonsense accs in p["leaves"]).
        measured = [p for p in points if p["eval"] is not None or p.get("leaves")]
        if not measured:
            return {"action": "pull-logs" if unsynced else "park",
                    "confidence": "low",
                    "rationale": f"{len(points)} run(s) for '{claim}' "
                    f"({proj} namespace) not measurable yet "
                    f"({'logs not synced' if unsynced else 'no eval'}).",
                    "options": ["rsync logs from the remotes, re-run the digest",
                                "hold"]}
        tail = (f"Known claim — draft/extend its RE rows in {proj.upper()}_EVIDENCE.md."
                if known else
                f"'{claim}' is not a known {proj} claim — mint it first (half-round).")
        return {"action": "new-finding" if known else "park",
                "confidence": conf,
                "rationale": f"{len(measured)} measured run(s) for '{claim}' in the "
                f"'{proj}' namespace (own graph — figures are {proj}'s own, NOT LDP "
                f"figdata). {tail}",
                "options": [f"round {claim}: draft RE row(s) in the {proj} evidence file",
                            f"queue more seeds → pipeline/propose.py queue {claim} --seeds …",
                            "hold for review"]}
    if orphans:
        return {"action": "new-family", "confidence": conf,
                "rationale": f"{len(orphans)} run(s) match no plotted family — a new "
                "tag prefix. Needs a figdata group tuple (color/marker) and, if the "
                "claim is new, an RE row.",
                "options": ["assign a figdata family tuple + draft an RE row",
                            "fold into an existing family if it's a naming variant",
                            "hold — wait for more seeds before naming a family"]}
    if unsynced and not material:
        return {"action": "pull-logs", "confidence": "low",
                "rationale": f"{len(unsynced)} finished run(s) have no local log yet "
                "(remote finish not rsynced) — can't measure honestly.",
                "options": ["rsync betas+logs from the remotes, re-run the digest",
                            "skip for now"]}
    # the common post-results move BEFORE journaling: queue more runs to firm a
    # finding up. Surfaced whenever confidence is short of 'high'.
    queue_opt = (f"queue more seeds → pipeline/propose.py queue {claim} --seeds …"
                 if conf != "high" else None)

    def _opts(*o):
        return [x for x in o if x]

    if known and material:
        return {"action": "tighten-row", "confidence": conf,
                "rationale": f"{n} new point(s) move {claim} by {md:+.4f} vs floor "
                f"(sign-consistent {sig:.0%}) — updates the existing row's n/Δ/σ.",
                "options": _opts(f"tighten {claim}: recompute n/Δ/σ with these points",
                                 queue_opt,
                                 f"split out a sub-finding if the regime differs from {claim}",
                                 "hold for review")}
    if known:
        return {"action": "reinforce", "confidence": "low",
                "rationale": f"{n or len(points)} new point(s) for {claim} with no "
                f"material move ({'Δ '+format(md,'+.4f') if md is not None else 'no learned Δ'}"
                ") — reinforces, no headline change.",
                "options": [f"bump {claim} sample count n only",
                            "no journal change (note in changelog)",
                            "hold"]}
    if material:
        return {"action": "new-finding", "confidence": conf,
                "rationale": f"claim '{claim}' has no journal row and shows a material "
                f"move ({md:+.4f}, {n} pts, sign-consistent {sig:.0%}) — candidate NEW "
                "RE row.",
                "options": _opts(f"draft a new RE row for '{claim}'",
                                 queue_opt,
                                 "map these runs onto an existing claim instead",
                                 "hold for more seeds")}
    return {"action": "park", "confidence": "low",
            "rationale": f"claim '{claim}' is not a journal ID and shows no material "
            "move — not journal-worthy yet.",
            "options": ["park (no action)", "keep accumulating seeds",
                        "relabel onto a known claim"]}


# ── emit ───────────────────────────────────────────────────────────────────────
def accept_ids(dg, sel=None):
    """The id-set (and its max `ended`) to consume on --accept. `sel=None` -> every
    id in the digest (full accept, the default). Otherwise only the chosen claims'
    point ids AND their failure ids — selective accept, so unselected claims keep
    re-surfacing in later digests until their owners handle them."""
    if sel is None:
        return set(dg["new_ids"]), dg["latest_ended"]
    consumed = [(p["id"], p.get("ended") or "")
                for c in dg["claims"] if c["claim"] in sel for p in c["points"]] + \
               [(f["id"], f.get("ended") or "")
                for f in dg["failures"] if f["claim"] in sel]
    return {i for i, _ in consumed}, max([e for _, e in consumed] + [""])


# #3 of the stuck-as-question fix (docs/AUDIT_stuck_as_question.md §5): fold the
# graph's Q→H graduation advisory into the round digest so the re-kind rides the
# results loop the human already runs — not a separate `--check` nobody reads. We
# CONSUME the graph advisory (one predicate, in journal_xref) rather than re-derive
# it here, so the two can never drift.
_GRAD_ADVISORY_RE = re.compile(
    r"([\w-]+):(\w+) is kind=question .*?graduation candidate")


def graduation_candidates(project=None):
    """[(project, id)] the journal graph flags as live questions that carry a
    prediction + experiment evidence — hypotheses that never graduated. Scoped to
    `project` when given (the round's current project), else all segments."""
    g = jx.build_graph(jx.load_segments(ROOT))
    out = []
    for a in g.get("advisories", []):
        m = _GRAD_ADVISORY_RE.search(a)
        if m and (project is None or m.group(1) == project):
            out.append((m.group(1), m.group(2)))
    return out


def build_digest(claims, failures):
    new_ids = sorted(p["id"] for ps in claims.values() for p in ps) + \
              sorted(f["id"] for f in failures)
    latest = max([p["ended"] or "" for ps in claims.values() for p in ps] +
                 [f["ended"] or "" for f in failures] + [""])
    out = {"latest_ended": latest, "new_ids": new_ids,
           "n_new": sum(len(ps) for ps in claims.values()),
           "n_failed": len(failures), "claims": [], "failures": failures,
           "orphans": [],
           "graduation": graduation_candidates(current_project(ROOT))}
    for claim in sorted(claims):
        ps = sorted(claims[claim], key=lambda p: p.get("bits") or 0)
        agg = _agg(ps)
        kind = next((p.get("kind") for p in ps if p.get("kind")), None)
        out["claims"].append({"claim": claim, "known": claim in journal_claims(),
                              "kind": kind, "points": ps, "aggregate": agg,
                              "recommendation": recommend(claim, ps, agg)})
        # the orphans list means "needs an LDP figdata family tuple" — scaffold/infra
        # runs (goal.kind=scaffold) never become a plot family, so they're excluded
        # here (still visible in their claim's point table). None kind = science-
        # eligible (pre-goals), so it stays in. A NON-default project (FracBits,
        # BitDistiller) is its OWN graph — its tags are plotted in its own Pareto,
        # never LDP's figdata — so they're excluded too (they'd otherwise demand a
        # bogus LDP figure tuple).
        out["orphans"] += [p["tag"] for p in ps
                           if p["plot"] == "orphan" and p.get("kind") != "scaffold"
                           and project_of(claim) == DEFAULT_PROJECT]
    return out


def _fmt(v, n=4):
    return "—" if v is None else f"{v:.{n}f}"


def _cfg_cell(p):
    """The machine-derived config signature for a digest point, with a ⚠ when the
    config is in the known-collapse registry (per-token fp4 → non-deployable)."""
    sig = p.get("config_sig") or "—"
    return f"⚠ {sig}" if p.get("config_invalid") else sig


def _config_notes(points):
    """Callouts under a claim's table: (1) any known-invalid/collapse config in the
    results — so an author can't quietly headline a non-deployable cell; (2) a
    CONFOUNDED ladder — ≥2 config axes swept at once (Q12's granularity+budget), so
    a 'budget-dependent' reading isn't stated as a clean single-axis effect."""
    out = []
    bad, seen = [p for p in points if p.get("config_invalid")], set()
    if bad:
        out.append("> ⚠ **Invalid-config results present — do NOT headline as deployable:**")
        for p in bad:
            key = (p["config_sig"], p["config_invalid"])
            if key in seen:
                continue
            seen.add(key)
            out.append(f"> - `{p['config_sig']}` — {p['config_invalid']}")
    confound = next((p.get("config_confound") for p in points
                     if p.get("config_confound")), None)
    if confound:
        out.append(f"> ⚠ **Confounded ladder — {len(confound)} config axes vary at "
                   f"once** ({', '.join(confound)}). Don't state a single-axis effect; "
                   f"hold all but one fixed to attribute a move.")
    return out + [""] if out else []


def _graduation_section(dg):
    """Round-digest block for the graph's Q→H graduation candidates (#3). A
    live question carrying a prediction + experiment evidence is a hypothesis that
    never graduated — re-kind it as part of this round. Empty → no block."""
    grad = dg.get("graduation") or []
    if not grad:
        return []
    L = ["## Graduation candidates (Q→H — a re-kind, not a result)", "",
         "These live questions carry a prediction AND experiment evidence — "
         "hypotheses that never graduated (docs/AUDIT_stuck_as_question.md). "
         "Re-kind in this round (keep the id; hypothesis, or **mechanism** if it "
         "names a diagnosed cause):", ""]
    for proj, cid in grad:
        L.append(f"- **{proj}:{cid}** → re-kind `kind: question` → `hypothesis` "
                 f"(see bitdistiller:Q16→H12 / Q9→mechanism)")
    return L + [""]


def render_md(dg):
    L = ["# Ingestion digest — what finished since last time", ""]
    if not dg["n_new"] and not dg["n_failed"]:
        # Nothing FINISHED — but a stuck graduation candidate is still actionable,
        # so surface it even on an otherwise-empty round.
        grad = _graduation_section(dg)
        L += grad if grad else ["_Nothing new since the watermark._"]
        return "\n".join(L) + "\n"
    L += [f"**{dg['n_new']} new** finished run(s), **{dg['n_failed']} failed**, "
          f"latest ended `{dg['latest_ended']}`.", ""]
    L += _graduation_section(dg)

    # top-of-digest decision board: one recommended action per claim, for discussion
    L += ["## Recommended actions (for discussion)", "",
          "| claim | known? | action | conf | move |", "|---|---|---|---|---|"]
    for c in dg["claims"]:
        a, r = c["aggregate"], c["recommendation"]
        mv = f"{_fmt(a['mean_delta'])} (n={a['n']})" if a["n"] else "—"
        L.append(f"| {c['claim']} | {'yes' if c['known'] else 'NEW'} | "
                 f"**{r['action']}** | {r['confidence']} | {mv} |")
    L.append("")

    for c in dg["claims"]:
        a, r = c["aggregate"], c["recommendation"]
        head = f"## {c['claim']}  ·  {len(c['points'])} new point(s)"
        if a["n"]:
            head += (f"  ·  learned−floor **{_fmt(a['mean_delta'])}** "
                     f"(σ {_fmt(a['sigma'])}, n={a['n']})")
        L += [head, "",
              f"**Recommend → {r['action']}** ({r['confidence']} confidence). "
              f"{r['rationale']}", "",
              "_Options:_"] + [f"  {i+1}. {o}" for i, o in enumerate(r["options"])]
        if any(p.get("leaves") for p in c["points"]):
            # leaf-kind project (BitDistiller): no eval/bits/floor — render the
            # per-kind benchmark leaves (PPL + commonsense accs) instead.
            cols = []
            for p in c["points"]:
                for k in (p.get("leaves") or {}):
                    if k not in cols:
                        cols.append(k)
            cols.sort(key=lambda k: (k != "WIKI_PPL", k))   # PPL first, then accs
            L += ["",
                  "| tag | config | " + " | ".join(cols) + " | plot |",
                  "|---|---|" + "---|" * (len(cols) + 1)]
            for p in c["points"]:
                lv = p.get("leaves") or {}
                tag = p["tag"] + ("" if p["synced"] else " ⚠unsynced")
                cells = " | ".join(_fmt(lv.get(k)) for k in cols)
                L.append(f"| `{tag}` | {_cfg_cell(p)} | {cells} | {p['plot']} |")
            L += [""] + _config_notes(c["points"])
        else:
            L += ["",
                  "| tag | config | eff bits | eval | Δ vs floor | plot | family |",
                  "|---|---|---|---|---|---|---|"]
            for p in c["points"]:
                tag = p["tag"] + ("" if p["synced"] else " ⚠unsynced")
                L.append(f"| `{tag}` | {_cfg_cell(p)} | {_fmt(p['bits'],3)} | "
                         f"{_fmt(p['eval'])} | {_fmt(p['delta_vs_floor'])} | "
                         f"{p['plot']} | {p['family'] or '—'} |")
            L += [""] + _config_notes(c["points"])
    if dg["orphans"]:
        L += ["## Orphans — need a figdata family tuple (color/marker, human picks)",
              ""] + [f"- `{t}`" for t in sorted(set(dg["orphans"]))] + [""]
    if dg["failures"]:
        L += ["## Failed (reported, not ingested)", ""]
        L += [f"- `{f['tag']}` (id `{f['id']}`, exit {f['exit']}, claim {f['claim']})"
              for f in dg["failures"]] + [""]
    half = pending_half_rounds()
    if half:
        L += ["## Declared hypotheses — experiments still pending (reminder)", "",
              "These half-rounds minted a journal hypothesis but haven't queued their "
              "experiments yet (often new code in flight). Not blocking — finish each "
              "with `propose.py round <claim> --finish` once its configs are submitted:",
              ""] + [f"- `{c}`" for c in half] + [""]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Phase-1 result-ingestion digest.")
    ap.add_argument("--accept", action="store_true",
                    help="advance the watermark over this digest's ids (Phase 2/3 "
                         "calls this AFTER the journal diff is approved + plots "
                         "refreshed). Without it, nothing is consumed.")
    ap.add_argument("--force", action="store_true",
                    help="advance even if a round is still open (experiments drafted "
                         "into the journal but not yet queued). Use with care.")
    ap.add_argument("--claim",
                    help="comma-separated claim(s) to accept (default: ALL claims in "
                         "the digest). Selective accept consumes only the chosen "
                         "claims' ids (incl. their failures), so a per-claim round "
                         "can journal a subset and leave other claims re-surfacing.")
    a = ap.parse_args()

    state = load_state()
    claims, failures = collect(state)
    dg = build_digest(claims, failures)

    os.makedirs(os.path.dirname(DIGEST_JSON), exist_ok=True)
    json.dump(dg, open(DIGEST_JSON, "w"), indent=2)
    open(DIGEST_MD, "w").write(render_md(dg))
    sys.stdout.write(render_md(dg))

    # selective accept: restrict the consumed id-set AND the open-round block to the
    # chosen claims. The watermark is a set of seen ids (collect() filters by it, not
    # by time), so consuming a subset is sound — unselected claims keep re-surfacing.
    sel = None
    if a.claim:
        sel = {c.strip() for c in a.claim.split(",") if c.strip()}
        in_digest = ({c["claim"] for c in dg["claims"]}
                     | {f["claim"] for f in dg["failures"]})
        unknown = sel - in_digest
        if unknown:
            sys.exit(f"\n[--claim: not in this digest: {', '.join(sorted(unknown))} "
                     f"— claims here: {', '.join(sorted(in_digest))}]")

    pending = open_rounds(blocking_only=True)
    if sel is not None:
        pending = [c for c in pending if c in sel]   # only block on SELECTED claims
    if a.accept and pending and not a.force:
        sys.stderr.write(
            "\n[REFUSED --accept: open round(s) " + ", ".join(pending) + " — the "
            "journal entry named experiments that were never queued. Run "
            "`python pipeline/propose.py round <claim> --finish` (or --cancel), "
            "or re-run with --force.]\n")
        sys.exit(3)

    if a.accept:
        ids, ended = accept_ids(dg, sel)
        if ids:
            state["seen_ids"] = sorted(set(state["seen_ids"]) | ids)
            state["last_ended"] = max(state["last_ended"], ended)
            save_state(state)
            scope = f" for {', '.join(sorted(sel))}" if sel else ""
            sys.stderr.write(f"\n[accepted {len(ids)} ids{scope}; watermark "
                             f"-> {state['last_ended']}]\n")
        else:
            sys.stderr.write("\n[--accept: nothing new to consume]\n")


if __name__ == "__main__":
    main()
