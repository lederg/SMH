"""Authored facts — a journal number's *receipt*: value + the recipe that made it.

The journal's numbers are agent-written during a `journal-round`, at the moment
`ingest.py` has already computed them honestly from `runs/` logs. Historically the
round rendered the value into prose and discarded the provenance (which logs, which
formula); three mechanisms then spent effort recovering it (Part-A regex re-scrape,
`repro_check` reverse-engineering, the fact gate). This store captures it once, as a
keyed record, so `repro_check` becomes a deterministic *replay* of the recipe against
current logs instead of archaeology. See docs/PLAN_authored_facts_migration.md.

A record is keyed by the durable `ldp/facts_catalog._FACTS` key. Shape:

    "C1.owt_fp8_gap": {
      "value": 0.009, "kind": "delta",
      "deriv": {"op": "sub",
                "a": {"op": "EVAL", "tag": "owt_fixed_fp8"},
                "b": {"op": "EVAL", "tag": "owt_reference"}},
      "written": "2026-06-13", "round": "RE-05"
    }

`replay(record)` walks `deriv` recursively: leaf ops read a log via the LDP
`MeasurementCatalog` (adapter 4 — `pipeline.measure`, NOT figdata by name), node
ops combine sub-results. A missing log → None (fail-closed; surfaces as
path_missing upstream). A `kind:"dropped"` record (a genuine un-reproducible,
dropped WITH a reason) → None, skipped by callers.

NEVER re-runs GPU jobs (reads already-logged evidence). NEVER edits the journal.
"""
import glob
import json
import math
import os
import statistics
import sys

from smh.config import resolve_root, load_config
_ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# The repro-check anchor store is PER-INSTANCE data, not package code — it lives
# at the project ROOT (default `<ROOT>/authored_facts.json`), NOT inside the smh
# package, so an installed `smh` never ships one project's numbers to another. A
# repo may relocate it via smh.toml's top-level `authored_facts` key.
STORE = os.path.join(_ROOT, ((load_config(_ROOT) or {}).get("authored_facts")
                             or "authored_facts.json"))


# ── measurement leaves (adapter 4 — the LDP MeasurementCatalog, lazily built so
# the heavy figdata deps load only on replay; authored_facts is the LDP store, so
# its leaves resolve in LDP's terms) ─────────────────────────────────────────────
_RUNS = os.path.join(_ROOT, "runs")


DEFAULT_PROJECT = "ldp"


def _cat():
    """The LDP (default-project) MeasurementCatalog — kept as a no-arg seam so the
    existing hermetic tests (`monkeypatch.setattr(af, '_cat', lambda: fake)`) keep
    intercepting the default path unchanged."""
    from smh.pipeline.measure import measurement_for
    return measurement_for(DEFAULT_PROJECT)


def _catalog(project=None):
    """Resolve a record's MeasurementCatalog. The default project routes through
    `_cat()` (the monkeypatch seam); a co-located project (FracBits, BitDistiller)
    resolves via the registry, so its leaves replay in ITS terms."""
    if project in (None, "", DEFAULT_PROJECT):
        return _cat()
    from smh.pipeline.measure import measurement_for
    return measurement_for(project)


def _leaf(op, tag, project=None):
    """A leaf extractor: one run tag -> one regenerated value, or None. The op IS
    the catalog kind (EVAL/B2 for LDP; WIKI_PPL/ARC_C/… for BitDistiller) — a
    generic `value(tag, kind)` passthrough, so a new instance's leaves need no new
    op here."""
    return _catalog(project).value(tag, op)


def _resolve_arms(arms):
    """A list of tag-or-glob strings -> sorted list of concrete tags. A glob entry
    (contains * ? [) expands against runs/<pat>.log; a literal passes through. Sorted
    so arms_a / arms_b zip deterministically when no explicit `pairs` are given."""
    tags = []
    for a in arms:
        if any(c in a for c in "*?["):
            for p in sorted(glob.glob(os.path.join(_RUNS, f"{a}.log"))):
                tags.append(os.path.basename(p)[:-4])
        else:
            tags.append(a)
    return tags


def _diffs(node, project=None):
    """Per-pair differences for a paired statistic. Pairing is either explicit
    (`pairs`: [[tag_a, tag_b], ...]) or by zipping sorted resolved arms (`arms_a`,
    `arms_b`). `extract` (default EVAL) is the per-run leaf op. Returns the list of
    extract(a) − extract(b), or None if arms misalign / any run fails to resolve
    (fail-closed: a missing log can't masquerade as a smaller n)."""
    extract = node.get("extract", "EVAL")
    if "pairs" in node:
        pairs = node["pairs"]
    else:
        A, B = _resolve_arms(node["arms_a"]), _resolve_arms(node["arms_b"])
        if len(A) != len(B):
            return None
        pairs = list(zip(A, B))
    if not pairs:
        return None
    out = []
    for a, b in pairs:
        va, vb = _leaf(extract, a, project), _leaf(extract, b, project)
        if va is None or vb is None:
            return None
        out.append(va - vb)
    return out


# ── op registry — the formal vocabulary (converges with gate.py::_derived) ────────
# Each op is f(node, project) -> value|None. Node ops recurse via _eval; `project`
# threads the record's namespace down so leaves resolve against the right catalog.
# Adding a fact family that needs a new STATISTIC adds an op here; a new LEAF kind
# needs nothing (any catalog `value(tag, kind)` is a generic leaf — see _eval).
def _op_sub(node, project=None):
    a, b = _eval(node["a"], project), _eval(node["b"], project)
    return None if a is None or b is None else a - b


def _op_sum(node, project=None):
    a, b = _eval(node["a"], project), _eval(node["b"], project)
    return None if a is None or b is None else a + b


def _op_sqrt(node, project=None):
    c = _eval(node["child"], project)
    return None if c is None or c < 0 else math.sqrt(c)


def _op_mean_delta(node, project=None):
    """Mean paired difference over a seed/arm set (e.g. C4 dynuni − static)."""
    d = _diffs(node, project)
    return None if not d else statistics.mean(d)


def _op_paired_se(node, project=None):
    """Standard error of the paired mean: std(diffs, ddof=1) / sqrt(n)."""
    d = _diffs(node, project)
    if not d or len(d) < 2:
        return None
    return statistics.stdev(d) / math.sqrt(len(d))


def _op_significance(node, project=None):
    """The journal's 'Nσ': |mean(diffs)| / SE — how many standard errors the paired
    mean sits from zero. (The σ facts pin THIS, not a standard deviation.)"""
    d = _diffs(node, project)
    if not d or len(d) < 2:
        return None
    se = statistics.stdev(d) / math.sqrt(len(d))
    return None if se == 0 else abs(statistics.mean(d)) / se


# Node ops only. The LEAF ops (EVAL/B2 and any instance kind — WIKI_PPL, ARC_C …)
# are NOT enumerated here: a node with a `tag` field is a generic `value(tag, op)`
# passthrough (see _eval), so a new instance's leaves replay with zero new entries.
_REGISTRY = {
    "sub": _op_sub,
    "sum": _op_sum,
    "sqrt": _op_sqrt,
    "mean_delta": _op_mean_delta,
    "paired_se": _op_paired_se,
    "significance": _op_significance,
}


def _eval(node, project=None):
    op = node.get("op")
    fn = _REGISTRY.get(op)
    if fn is not None:                       # a node (combinator/statistic) op
        return fn(node, project)
    if "tag" in node:                        # a generic leaf: value(tag, kind=op)
        return _leaf(op, node["tag"], project)
    raise ValueError(f"unknown op: {op!r}")


# ── public API ────────────────────────────────────────────────────────────────
def _project_of(record):
    """The record's project namespace: an explicit `project` field wins; else
    derived from a `project:id` in its `round`/`claim` (e.g. 'bitdistiller:RE-01');
    else the default (LDP). So an LDP record (bare round 'RE-05') stays LDP."""
    if record.get("project"):
        return record["project"]
    for field in ("round", "claim"):
        v = record.get(field) or ""
        if ":" in v:
            return v.split(":", 1)[0]
    return DEFAULT_PROJECT


def replay(record):
    """Re-execute a record's recipe against current logs. Returns the regenerated
    value, or None if dropped / un-anchored / any source log is missing."""
    if record.get("kind") == "dropped" or "deriv" not in record:
        return None
    return _eval(record["deriv"], _project_of(record))


def load(path=STORE):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def save(store, path=STORE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
