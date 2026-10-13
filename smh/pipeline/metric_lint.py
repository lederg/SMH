"""Metric-provenance lint — ADVISORY label→basis check (SMH-general).

The fact gate proves a number is *sourced*; it never checks that the metric
LABEL beside the number matches the catalog basis that produced it. The
motivating incident (bitdistiller RE-39, 2026-07-21/26): five means published
as "core6" were actually the legacy lm-default-6 raw mean — every digit
sourced, every label wrong.

This lint closes that gap for any project whose MeasurementCatalog opts in by
exposing (all duck-typed, same pattern as `LEAF_KINDS` in ingest):

    METRIC_LEXICON : ((regex, (kind, ...)), ...)  — prose term -> candidate kinds
    LINT_KINDS     : (kind, ...)                  — kinds resolvable for lint
                     (canonical leaves + legacy/aux bases); falls back to
                     LEAF_KINDS when absent
    lint_value(tag, kind) -> float|None           — resolver over LINT_KINDS;
                     falls back to `value` when absent
    CONFUSABLE     : (frozenset({kind, ...}), ...) — basis families a reader can
                     plausibly mix up. The advisory fires ONLY when a labeled
                     number matches a non-candidate kind from the same family as
                     a candidate (the RE-39 class); cross-family hits are dense-
                     prose label-window artifacts, not mislabels (established by
                     the 2026-07-26 backfill audit, bitdistiller/metric_audit.py).
    results_dir    : str                          — where `runs/<tag>/` artifacts
                     live (tag existence check)

A catalog without METRIC_LEXICON (LDP/FracBits RunLogMeasurement) makes
`metric_check` a no-op — `enabled: False`, ok True.

ADVISORY-FIRST (HANDOFF_metric_provenance_gate): `metric_check` never fails a
gate on its own; `ok` is True unless the caller opts into fail-closed via
`fail_closed=True` (the Phase-3 flip, a USER decision — new rows only).

Matching tolerance (evidence-based, from the backfill audit):
  * exact at the token's printed precision (facts.number_matches), then
  * ±1 ulp at printed precision — absorbs truncation and double-rounding
    (a fold script averaging already-rounded per-seed values: RE-41's 0.5050
    vs the exact mean 0.50505→0.5051), then
  * derived within-kind arm-mean differences (deltas rows quote), each with the
    same ulp slack.
Candidate values per kind: per-tag singles, seed-family means and sample σ
(rows quote "±σ"), plus a ×100 "pt" scaling for kinds whose values live in
[-1, 1] (normalized-accuracy points).
"""
from __future__ import annotations

import fnmatch
import os
import re
import statistics
from collections import defaultdict
from decimal import Decimal

from smh.pipeline.facts import number_matches, to_decimal

# Numbers incl. leading-dot forms (".5557"); at least 2 decimals are required
# downstream — coarser tokens can't establish a basis either way.
NUM_RE = re.compile(r"(?<![\w.])([-+]?(?:\d+\.\d+|\.\d+))(?!\w)")
WINDOW = 250        # chars label-end -> number-start (label left of number)
RIGHT_WINDOW = 60   # labels shortly AFTER a number also join the union
_SEED_RE = re.compile(r"_s(?:42|\d{1,2})(?=_|$)")


# ── tag extraction (Source-column conventions: brace patterns over runs/) ────
def brace_expand(s):
    """Bash-style {a,b,c} expansion (nested, empty alternatives allowed)."""
    m = re.search(r"\{([^{}]*)\}", s)
    if not m:
        return [s]
    head, tail = s[: m.start()], s[m.end():]
    out = []
    for alt in m.group(1).split(","):
        out.extend(brace_expand(head + alt + tail))
    return out


def extract_tags(text, results_dir, tag_prefix=None):
    """Run tags referenced by `text`: whitespace tokens that point under
    `runs/` (or start with the artifact tag prefix), brace-expanded, `...`
    ellipses fnmatch-resolved against the results_dir listing. Only tags whose
    directory exists are returned (the lint recomputes from artifacts; a
    missing run can't support or contradict a label)."""
    try:
        listing = sorted(os.listdir(results_dir))
    except OSError:
        return []
    prefixes = ("...",) + ((tag_prefix,) if tag_prefix else ())
    tags, seen = [], set()
    for raw in text.split():
        tok = raw.strip("`;,()+*").rstrip("/")
        if "runs/" in tok:
            tok = tok.split("runs/", 1)[1]
        elif not tok.startswith(prefixes) and not (
                tag_prefix and tok.startswith(tag_prefix.rstrip("_") + "{")):
            continue
        tok = tok.split("/", 1)[0]
        for cand in brace_expand(tok):
            hits = (fnmatch.filter(listing, cand.replace("...", "*"))
                    if "..." in cand
                    else ([cand] if os.path.isdir(os.path.join(results_dir, cand))
                          else []))
            for t in hits:
                if t not in seen:
                    seen.add(t)
                    tags.append(t)
    return tags


# ── label association ────────────────────────────────────────────────────────
def _compile_lexicon(lexicon):
    return re.compile("|".join(f"(?P<L{i}>{p})" for i, (p, _) in enumerate(lexicon)),
                      re.IGNORECASE)


def labeled_numbers(text, lexicon):
    """[(display_label, candidate_kinds, number_token)] — candidate kinds are
    the UNION over every lexicon term within WINDOW chars left / RIGHT_WINDOW
    right of the number. Union, not nearest: rows write paired headers
    ("wiki-PPL / core6: 10.17/0.6353") where nearest-left mislabels the first
    of each pair; a number is only suspect when it matches NONE of the nearby
    labels' kinds. Numbers with <2 decimals are skipped."""
    lex_re = _compile_lexicon(lexicon)
    text = text.replace("**", "").replace("`", "")
    labels = [(m.end(), m.group(0), lexicon[int(m.lastgroup[1:])][1])
              for m in lex_re.finditer(text)]
    out = []
    for m in NUM_RE.finditer(text):
        tok = m.group(1)
        if len(tok.split(".")[-1]) < 2:
            continue
        if text[max(0, m.start() - 2):m.start()].strip(" ") in ("~", "≈"):
            continue  # an avowed approximation can't be basis-checked
        near = [l for l in labels
                if (l[0] <= m.start() and m.start() - l[0] <= WINDOW)
                or (l[0] > m.start() and l[0] - m.end() <= RIGHT_WINDOW)]
        left = [l for l in near if l[0] <= m.start()]
        if not left:
            continue
        kinds = tuple(dict.fromkeys(k for _e, _l, ks in near for k in ks))
        label = "+".join(dict.fromkeys(l[1].strip().lower() for l in left[-3:]))
        out.append((label, kinds, tok))
    return out


# ── value pool ───────────────────────────────────────────────────────────────
def _lint_value(cat, tag, kind):
    fn = getattr(cat, "lint_value", None)
    try:
        return fn(tag, kind) if fn else cat.value(tag, kind)
    except Exception:
        return None


def build_pool(cat, tags):
    """{kind: [(desc, value)]} primary values + {kind: [(desc, value)]} derived
    (within-kind arm-mean differences). Primary = per-tag singles, seed-family
    means and sample σ, and a ×100 "pt" alias for values in [-1, 1]."""
    kinds = tuple(getattr(cat, "LINT_KINDS", None)
                  or getattr(cat, "LEAF_KINDS", ()) or ())
    pool, derived = defaultdict(list), defaultdict(list)
    for kind in kinds:
        arms = defaultdict(list)
        for tag in tags:
            v = _lint_value(cat, tag, kind)
            if v is not None:
                pool[kind].append((tag, v))
                arms[_SEED_RE.sub("", tag)].append(v)
        arm_means = {}
        for arm, vals in arms.items():
            if len(vals) >= 2:
                m = sum(vals) / len(vals)
                pool[kind].append((f"{arm} mean(n={len(vals)})", m))
                arm_means[arm] = m
                try:
                    s = statistics.stdev(vals)
                    pool[kind].append((f"{arm} σ(n={len(vals)})", s))
                    pool[kind].append((f"{arm} SE(n={len(vals)})",
                                       s / len(vals) ** 0.5))
                except statistics.StatisticsError:
                    pass
            else:
                arm_means[arm] = vals[0]
        items = list(arm_means.items())
        for i, (a1, v1) in enumerate(items):
            for a2, v2 in items:
                if a1 != a2:
                    derived[kind].append((f"{a1}−{a2}", v1 - v2))
        for target in (pool, derived):
            target[kind].extend(
                [(f"{d} ×100pt", v * 100.0) for d, v in target[kind]
                 if -1.0 <= v <= 1.0])
    return pool, derived


def _match(tok, entries, ulps=0):
    d = to_decimal(tok)
    if d is None:
        return None
    exp = d.as_tuple().exponent
    ulp = Decimal(1).scaleb(exp if isinstance(exp, int) and exp < 0 else 0)
    for desc, v in entries:
        if ulps == 0:
            if number_matches(tok, v):
                return desc, v
        else:
            dv = to_decimal(v)
            if dv is not None and abs(dv - d) <= ulp * ulps:
                return desc, v
    return None


# ── the check ────────────────────────────────────────────────────────────────
def metric_check(text, cat, tags=None, fail_closed=False):
    """Lint `text`'s labeled numbers against the catalog bases of the run tags
    it references. Returns {enabled, ok, checked, findings:[...]} — a finding is
    a labeled number that matches NO candidate basis (exact, ±1 ulp, derived)
    but DOES exactly match a confusable non-candidate basis. `ok` is False only
    when `fail_closed` and findings exist (Phase-3 flip; advisory by default)."""
    lexicon = getattr(cat, "METRIC_LEXICON", None)
    if not lexicon:
        return {"enabled": False, "ok": True, "checked": 0, "findings": []}
    if tags is None:
        tags = extract_tags(text, getattr(cat, "results_dir", ""),
                            tag_prefix=getattr(cat, "TAG_PREFIX", None))
    pool, derived = build_pool(cat, tags)
    groups = tuple(getattr(cat, "CONFUSABLE", ()) or ())
    findings, checked = [], 0
    for label, kinds, tok in labeled_numbers(text, lexicon):
        checked += 1
        if any(_match(tok, pool.get(k, []), u) or _match(tok, derived.get(k, []), u)
               for u in (0, 1) for k in kinds):
            continue
        confusable = set().union(*[g for g in groups if g & set(kinds)]) - set(kinds) \
            if groups else set()
        for k in sorted(confusable):
            # only point values (singles / seed-means) convict a label; σ/SE
            # spread stats collide across kinds at 3-4 decimals far too often
            hit = _match(tok, [e for e in pool.get(k, [])
                               if "σ(" not in e[0] and "SE(" not in e[0]])
            if hit:
                findings.append({"label": label, "kinds": list(kinds),
                                 "number": tok, "matched_kind": k,
                                 "matched": hit[0], "value": hit[1]})
                break
    return {"enabled": True, "ok": not (fail_closed and findings),
            "checked": checked, "findings": findings}


def metric_report(res):
    if not res.get("enabled"):
        return "METRIC LINT: n/a (no METRIC_LEXICON on this project's catalog)"
    n = len(res["findings"])
    status = "clean" if n == 0 else ("FAIL" if not res["ok"] else "ADVISORY")
    lines = [f"METRIC LINT: {res['checked']} labeled number(s) checked -> "
             f"{status}" + (f", {n} suspect label(s)" if n else "")]
    for f in res["findings"]:
        lines.append(
            f"  label `{f['label']}` beside {f['number']} matches NO "
            f"{'/'.join(f['kinds'])} value, but equals {f['matched_kind']} = "
            f"{f['value']:.6g} [{f['matched']}] — wrong basis or wrong label; "
            f"name the basis per the §2 metric glossary")
    return "\n".join(lines)
