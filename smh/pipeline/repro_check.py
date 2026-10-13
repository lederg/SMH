"""Repro-check — re-anchor journal numbers to regenerated evidence (out-of-loop).

The in-loop hard gate (gate.py) checks prose -> journal. This adds the missing
journal -> runs/ link, so the chain prose -> journal -> evidence is closed:

  Part A  resolution check  — every curated fact still resolves; number bag stable
                             (catches regex-rot in facts._FACTS). Swept across ALL
                             co-located namespaces (ldp + fracbits + bitdistiller +
                             smh): each segment's number bag must be non-empty and a
                             project that declares a facts_catalog must fully resolve.
  Part B  evidence anchoring — a curated fact's journal value must equal the value
                             REPLAYED from its authored-fact record (the recipe
                             captured at write time): pipeline.authored_facts walks
                             the record's `deriv` against current logs (figdata
                             leaves), compared with the SAME Decimal comparator the
                             gate uses (facts.number_matches) — one number-semantics.
                             A `kind:"dropped"` record (procedure not saved / fragile
                             σ) is honest non-coverage, surfaced but not checked.
                             So Part B now catches exactly EVIDENCE-DRIFT: a record
                             that was right at write time whose log moved under it.

Never re-runs GPU jobs (reads already-logged evidence) and never edits the journal
(a drift is a signal for a human). See docs/PLAN_repro_check.md.

CLI:  python pipeline/repro_check.py        # run + write state; nonzero exit if red
"""
import json
import os
import sys
from datetime import datetime, timezone

from smh.config import resolve_root, state_path, load_config, project_config, instantiate
_ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _ROOT)
from smh.pipeline.facts import structured_facts, journal_numbers, number_matches  # noqa: E402
from smh.pipeline import authored_facts  # noqa: E402  (replay engine + the record store)
from smh import journal_xref as jx       # noqa: E402  (per-project segment text)

STATE = state_path(_ROOT, "repro_state.json")
MIN_NUMS = 200          # parser-break tripwire (today ~369 LDP tokens)
STALE_S = 2 * 86400     # >2d since last run -> distrust (stale-as-red)
DEFAULT_PROJECT = "ldp"


# Part B is driven entirely by pipeline/authored_facts.json — the committed store of
# per-fact recipes (value + deriv + source logs), keyed by the same _FACTS keys.
# `authored_facts.replay(record)` regenerates each non-dropped record's value from
# current logs (figdata leaves). The hand-maintained ANCHORS dict + _delta closures
# this module used to carry are RETIRED — see docs/PLAN_authored_facts_migration.md.
def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed_s(ts):
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return float("inf")            # unparseable timestamp -> infinitely stale


# --- Part A -------------------------------------------------------------------
def check_resolution(text=None):
    """Every curated fact resolves + the number bag is non-empty/stable. Returns
    (ok, problems[])."""
    facts = structured_facts(text)
    problems = []
    unresolved = sorted(k for k, v in facts.items() if v is None)
    if unresolved:
        problems.append(f"unresolved facts (regex drifted from journal): {unresolved}")
    n = len(journal_numbers(text))
    if n < MIN_NUMS:
        problems.append(f"journal_numbers collapsed to {n} (< {MIN_NUMS}) — parser break")
    return (not problems), problems


# --- Part A, swept across the co-located non-default namespaces ----------------
def _co_projects(root):
    """The registered project ids from smh.toml [projects.*], in file order."""
    cfg = load_config(root) or {}
    return list((cfg.get("projects") or {}).keys())


def _project_catalog(project, root):
    """The FactsCatalog a NON-default project declares in smh.toml, or None. Only
    projects with a curated facts_catalog (ldp, bitdistiller) have regexes to rot;
    fracbits/smh have none, so only their number bag is checked."""
    pc = project_config(root, project) or {}
    ref = pc.get("facts_catalog")
    return instantiate(ref) if ref else None


def check_projects(root=None):
    """Part-A resolution swept across the co-located NON-default namespaces
    (fracbits/bitdistiller/smh). The default project (ldp) is covered by
    check_resolution(); here every other segment must yield a non-empty number bag,
    and one that declares a facts_catalog must fully resolve it (the regex-rot guard,
    per-project). Returns (ok, problems[], per[]). Same fail-closed spirit — a drift
    in ANY namespace reds the whole verdict."""
    root = root or _ROOT
    problems, per = [], []
    for p in _co_projects(root):
        if p == DEFAULT_PROJECT:
            continue
        txt = jx.segment_text(p, root) or ""
        n = len(journal_numbers(txt))
        if n == 0:
            problems.append(f"[{p}] journal_numbers empty — segment/parser break")
        cat = _project_catalog(p, root)
        unresolved = []
        if cat is not None:
            facts = structured_facts(txt, cat)
            unresolved = sorted(k for k, v in facts.items() if v is None)
            if unresolved:
                problems.append(f"[{p}] unresolved facts (regex drifted): {unresolved}")
        per.append({"project": p, "nums": n, "has_catalog": cat is not None,
                    "unresolved": unresolved})
    return (not problems), problems, per


# --- Part B -------------------------------------------------------------------
def check_anchors(text=None):
    """Each NON-dropped authored-fact record is replayed from current logs; its
    journal value must equal the replay. Returns (ok, rows[]) with rows =
    [key, status, journal_val, evidence_val]; status in verified | mismatch |
    value_not_found | path_missing. Dropped records (honest non-coverage) are
    skipped here and listed separately. Fail-closed on any non-verified."""
    facts = structured_facts(text)
    store = authored_facts.load()
    rows = []
    for key, rec in sorted(store.items()):
        if rec.get("kind") == "dropped":
            continue
        jval = facts.get(key)
        if jval is None:
            rows.append([key, "value_not_found", None, None])
            continue
        try:
            ev = authored_facts.replay(rec)
        except Exception:
            ev = None
        if ev is None:
            rows.append([key, "path_missing", jval, None])
            continue
        status = "verified" if number_matches(str(jval), ev) else "mismatch"
        rows.append([key, status, jval, round(ev, 6)])
    ok = all(r[1] == "verified" for r in rows)
    return ok, rows


# --- Part C: config provenance (adapter-5) ------------------------------------
# Closes prose -> config -> job-record. For each RE row that USES the config
# convention (carries a `**⚙ sig**` token) or references a run whose config is
# known-invalid, re-derive the configs from the row's Source tags' job records and
# flag: (drift) a ⚙ token that is not a real signature of any of the row's runs;
# (undisclosed) an invalid/collapse config among the runs that the row never
# shows. Historical rows with no token and no resolvable-invalid run are skipped —
# best-effort retrofit, so the un-backfilled past never reds the build.
import re as _re
_RE_ROW = _re.compile(r"^\|\s*(RE-\d+[a-z]?)\s*\|")


def _evidence_files(root):
    """(project, evidence_path) for every co-located ledger, default first."""
    out, seen = [], set()
    for p in [DEFAULT_PROJECT] + _co_projects(root):
        files = jx.ledger_files(p, root)
        if not files:
            continue
        ev = os.path.join(root, files[1])
        if ev not in seen and os.path.exists(ev):
            seen.add(ev)
            out.append((p, ev))
    return out


def _token_row_status(tokens, tags, desc, project):
    """[status, detail] for one RE row that carries `**⚙ sig**` token(s).
    Own-sig test first; a token outside the row's own sigs may still be a
    DISCLOSURE token — a sig the fact gate FORCES onto the row for an
    invalid/superseded run under the same claim that the row never re-ran
    ("disclosed for provenance", RE-33 house style). Such a token is accepted iff
    it is a real signature of some run under a claim the row's OWN runs were
    submitted under, and only when at least one token DOES match an own sig — a
    fully mislabeled row (no own-sig token at all) still hard-fails as drift."""
    from smh.pipeline import config_sig as cs
    sigs, invalid = {}, {}
    for t in tags:
        s = cs.signature_for_tag(t, project)
        sigs[s.string] = s
        if s.invalid:
            invalid[s.string] = s.invalid
    drift = sorted(t for t in tokens if t not in sigs)
    rescued = []
    if drift and any(t in sigs for t in tokens):
        claims = {c for c in (cs.claim_for_tag(t) for t in tags) if c}
        wide = set().union(*map(cs.sigs_for_claim, claims)) if claims else set()
        rescued = sorted(t for t in drift if t in wide)
        drift = sorted(t for t in drift if t not in wide)
    undisclosed = sorted(sig for sig in invalid if sig not in desc)
    if drift:
        return ["drift", f"tokens not real: {drift}"]
    if undisclosed:
        return ["undisclosed", f"invalid config not shown: {undisclosed}"]
    detail = f"{len(tokens)} token(s), {len(tags)} run(s)"
    if rescued:
        detail += f", {len(rescued)} disclosure token(s) claim-verified"
    return ["verified", detail]


def check_config_provenance(root=None):
    """Re-derive each RE row's configs from its runs and check the prose against
    them. Returns (ok, rows[]) with rows = [re_id, status, detail]; status in
    verified | drift | undisclosed | unresolved | skipped. Fail-closed on
    drift/undisclosed only."""
    from smh.pipeline import config_sig as cs
    root = root or _ROOT
    cs.reset_cache()
    rows = []
    for project, ev in _evidence_files(root):
        for line in open(ev, errors="replace"):
            m = _RE_ROW.match(line)
            if not m:
                continue
            reid = m.group(1)
            cells = [c.strip() for c in line.replace(r"\|", "\x00").split("|")]
            desc = cells[4].replace("\x00", "|") if len(cells) > 4 else ""
            source = cells[5].replace("\x00", "|") if len(cells) > 5 else ""
            tokens = cs.config_tokens(desc)
            tags = cs.tags_in_source(source)
            if not tags:
                if tokens:
                    rows.append([reid, "unresolved",
                                 "config token present but no Source tag resolves "
                                 "to a job record (GC'd?)"])
                continue
            tag = f"{project}:{reid}"
            # ENFORCE only on rows that engage the convention (carry a ⚙ token).
            # An un-backfilled row that references an invalid run is flagged
            # `backfill_owed` (non-fatal) — best-effort retrofit must not red the
            # build on the pre-existing past; the backfill converts it, and from
            # then on drift/undisclosed hard-fail.
            if not tokens:
                invalid = sorted({s.string for s in
                                  (cs.signature_for_tag(t, project) for t in tags)
                                  if s.invalid})
                if invalid:
                    rows.append([tag, "backfill_owed",
                                 f"invalid config in runs, no ⚙ token yet: "
                                 f"{invalid}"])
                else:
                    rows.append([tag, "skipped", "no config token (pre-backfill)"])
                continue
            rows.append([tag] + _token_row_status(tokens, tags, desc, project))
    ok = not any(r[1] in ("drift", "undisclosed") for r in rows)
    return ok, rows


# --- driver + state -----------------------------------------------------------
def run(text=None):
    """Run every part, stamp + persist the verdict, return it. Part A now sweeps the
    default project (check_resolution) AND the co-located namespaces (check_projects);
    Part B anchoring stays scoped to the projects that carry an authored-fact store
    (LDP today)."""
    res_ok, res_problems = check_resolution(text)
    proj_ok, proj_problems, proj_rows = check_projects()
    anc_ok, anc_rows = check_anchors(text)
    cfg_ok, cfg_rows = check_config_provenance()
    store = authored_facts.load()
    facts = structured_facts(text)
    dropped = sorted(k for k, r in store.items() if r.get("kind") == "dropped")
    # a curated fact with NO record at all = real coverage gap. This BLOCKS (red):
    # a new journal-round conclusion must be anchored OR dropped-with-reason, never
    # silently un-receipted. The red IS the trigger to author/drop its record — no
    # human memory, no skill scaffold (see docs/PLAN_authored_facts_migration.md).
    no_record = sorted(set(facts) - set(store))
    verdict = {
        "ts": _now_iso(),
        "ok": res_ok and proj_ok and anc_ok and cfg_ok and not no_record,
        "resolution": res_problems + proj_problems,
        "projects": proj_rows,
        "anchors": anc_rows,
        "config_provenance": cfg_rows,
        "dropped": dropped,
        "no_record": no_record,
    }
    save_state(verdict)
    return verdict


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return None


def save_state(verdict):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(verdict, f, indent=2)
    os.replace(tmp, STATE)


def build_status(stale_s=STALE_S):
    """For build.py's guard. green = ok AND fresh. Returns (green, reason)."""
    st = load_state()
    if st is None:
        return False, "repro-check has never run (no repro_state.json)"
    if not st.get("ok"):
        bad = [r for r in st.get("anchors", []) if r[1] != "verified"]
        norec = st.get("no_record", [])
        parts = []
        if st.get("resolution"):
            parts.append(f"resolution={st['resolution']}")
        if bad:
            parts.append(f"drift={bad}")
        bad_cfg = [r for r in st.get("config_provenance", [])
                   if r[1] in ("drift", "undisclosed")]
        if bad_cfg:
            parts.append(f"config-drift={[(r[0], r[1]) for r in bad_cfg]}")
        if norec:
            parts.append(f"NO RECORD (author or drop in authored_facts.json): {norec}")
        return False, f"repro-check RED @ {st.get('ts')}: " + "  ".join(parts)
    if _elapsed_s(st.get("ts", "")) > stale_s:
        return False, (f"repro-check STALE (last ok {st.get('ts')}, "
                       f"> {stale_s / 86400:.0f}d) — cron may have stopped")
    return True, f"repro-check green @ {st.get('ts')}"


def report(verdict):
    lines = [f"REPRO-CHECK: {'PASS' if verdict['ok'] else 'FAIL'} @ {verdict['ts']}"]
    for p in verdict["resolution"]:
        lines.append(f"  resolution: {p}")
    for pr in verdict.get("projects", []):
        cat = "catalog" if pr.get("has_catalog") else "no-catalog"
        lines.append(f"  project {pr['project']:<13} nums={pr['nums']:<5} ({cat})"
                     + (f"  UNRESOLVED={pr['unresolved']}" if pr.get("unresolved") else ""))
    for key, status, jv, ev in verdict["anchors"]:
        flag = "" if status == "verified" else "  <-- DRIFT"
        lines.append(f"  {status:<15} {key:<22} journal={jv} evidence={ev}{flag}")
    cfg = verdict.get("config_provenance", [])
    bad_cfg = [r for r in cfg if r[1] in ("drift", "undisclosed")]
    if cfg:
        n_ver = sum(1 for r in cfg if r[1] == "verified")
        lines.append(f"  config-provenance: {n_ver} verified, {len(bad_cfg)} bad "
                     f"of {len(cfg)} RE rows checked")
        for reid, status, detail in bad_cfg:
            lines.append(f"    {status:<12} {reid:<18} {detail}  <-- CONFIG DRIFT")
    n_anc = len(verdict["anchors"])
    n_drop = len(verdict.get("dropped", []))
    n_norec = len(verdict.get("no_record", []))
    lines.append(f"  anchored {n_anc}/{n_anc + n_drop + n_norec} curated facts "
                 f"({n_drop} dropped — procedure not saved / fragile σ"
                 + (f"; {n_norec} NO RECORD" if n_norec else "") + ")")
    if n_norec:
        lines.append(f"  no_record (coverage gap, add via journal-round): "
                     f"{verdict['no_record']}")
    return "\n".join(lines)


if __name__ == "__main__":
    v = run()
    print(report(v))
    sys.exit(0 if v["ok"] else 1)
