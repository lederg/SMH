"""Phase 2 of result ingestion (docs/HANDOFF_result_ingestion.md): act on a
post-results discussion decision.

The digest (Phase 1) presents a recommendation + options per claim. The user
discusses, then picks. A pick lands in ONE of two sinks — both common, siblings:

  journal  — draft a minimal journal edit and HARD-GATE it: every number in the
             draft must be sourced from the digest's verified facts ∪ the journal
             (fail-closed, reusing pipeline/gate.py). The LLM (the in-session
             assistant) writes the prose; the gate makes that safe. Output is a
             reviewed edit, never an auto-commit.

  queue    — the common move BEFORE journaling: queue follow-up experiments
             (more seeds, a matched baseline, a new budget rung). Each follow-up
             is cloned from a base config and stamped with `goal.claim = <claim>`
             — so when it finishes, the NEXT ingest auto-groups its result under
             the same claim and the recommendation sharpens (low→high confidence,
             new-finding→tighten-row). This CLOSES THE LOOP.

CLI:
  propose.py journal <claim>            assemble drafting context -> stdout + file
  propose.py gate <claim> <edit_file>   gate a drafted edit (exit 1 if unsourced)
  propose.py queue <claim> --seeds 7,8  plan follow-up configs+submits (dry-run)
  propose.py queue <claim> --seeds 7,8 --write   write configs + print submits
"""
import argparse
import glob
import json
import math
import os
import re
import statistics
import sys

from smh.config import resolve_root, state_path, resolve_current_project
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Reports"))
from smh.pipeline import gate as _gate                       # noqa: E402
from smh.pipeline.facts import journal_numbers, NUM_RE       # noqa: E402
from smh import journal_xref as jx                                # noqa: E402  (load_journal — two-file contract)

DIGEST_JSON = state_path(ROOT, "ingest_digest.json")
CONTEXT = state_path(ROOT, "propose_context.md")
DONE = os.path.join(ROOT, "scheduler", "done")
CONFIGS = os.path.join(ROOT, "configs")

# A co-located non-default project carries a `project:id` claim; the default
# project (LDP) stays bare. The round resolves and gates against the claim's OWN
# project segment so a FracBits edit can't cite an LDP number, and vice-versa.
DEFAULT_PROJECT = "ldp"


def _resolved_project(claim):
    """(project, source) for a claim: a `project:id` prefix is explicit and always
    wins (`ldp:H8` reaches LDP); a bare id resolves via the session-default
    precedence (config.current_project) so a pinned session redefines the bare-id
    fallback — see docs/DESIGN_smh_session_default_project.md §2.1/§7."""
    if ":" in claim:
        return claim.split(":", 1)[0], "explicit"
    return resolve_current_project(ROOT)


def _project_of(claim):
    return _resolved_project(claim)[0]


def _echo_project(claim):
    """Guardrail (§2.1): when a minting/gating command's project came from a default
    tier (env pin / session / global) rather than an explicit id, echo it to stderr
    so a mis-pin is visible before the edit. Silent for an explicit prefix and for
    the plain repo default (keeps the LDP path noise-free)."""
    proj, source = _resolved_project(claim)
    if source not in ("explicit", "repo default"):
        sys.stderr.write(f"[project: {proj} ({source})]\n")


def _bare_id(claim):
    """The claim id without its namespace ('fracbits:H1' -> 'H1', 'H8' -> 'H8')."""
    return claim.split(":", 1)[1] if ":" in claim else claim


def _project_journal_text(claim):
    """The journal text the claim is resolved/gated against: the whole LDP ledger
    for a default-project claim (byte-unchanged), else just that project's segment."""
    proj = _project_of(claim)
    if proj == DEFAULT_PROJECT:
        return jx.load_journal(ROOT)
    return jx.segment_text(proj, ROOT) or ""


def _project_journal_numbers(claim):
    """The journal-side allow-list for the gate, scoped to the claim's project.
    LDP keeps the catalog path (byte-unchanged); a co-located project gates against
    its own segment's numbers only."""
    if _project_of(claim) == DEFAULT_PROJECT:
        return journal_numbers()                       # LDP catalog — unchanged
    return journal_numbers(_project_journal_text(claim))


def _sink_files(claim):
    """(journal_file, evidence_file) for the claim's project — where a claim edit
    and an RE row respectively land. By the two-file convention the JOURNAL file
    holds claims (§1), the EVIDENCE file holds RE rows (§2)."""
    files = jx.ledger_files(_project_of(claim), ROOT) or ("JOURNAL.md", "EVIDENCE.md")
    journal = next((f for f in files if "JOURNAL" in f.upper()), files[0])
    evidence = next((f for f in files if "EVIDENCE" in f.upper()), files[-1])
    return journal, evidence


# ── shared: pull a claim out of the digest ─────────────────────────────────────
def load_digest():
    if not os.path.exists(DIGEST_JSON):
        sys.exit("error: no ingest_digest.json — run pipeline/ingest.py first")
    return json.load(open(DIGEST_JSON))


def find_claim(claim, digest=None):
    """The digest entry for a claim, or None — the SOFT lookup. Returns None when
    there's no digest at all (a half-round: a new artifact with no results yet) or
    the claim simply isn't in it. Callers that REQUIRE results use claim_entry."""
    if digest is None and not os.path.exists(DIGEST_JSON):
        return None
    d = digest or load_digest()
    return next((c for c in d.get("claims", []) if c["claim"] == claim), None)


def claim_entry(claim, digest=None):
    """The digest entry for a claim, or exit — the STRICT lookup (results required)."""
    c = find_claim(claim, digest)
    if c is None:
        d = digest if digest is not None else (load_digest()
                                               if os.path.exists(DIGEST_JSON) else {})
        sys.exit(f"error: claim '{claim}' not in the current digest "
                 f"(have: {', '.join(x['claim'] for x in d.get('claims', []))})")
    return c


def digest_numbers(claim, digest=None):
    """The verified numeric facts for a claim — the allow-list the journal edit
    may cite (in addition to numbers already in the journal). Every value the
    gate will accept as 'sourced from this digest'. Empty for a half-round (no
    results) — there the design numbers come from _planned_numbers instead."""
    c = find_claim(claim, digest)
    if c is None:
        return set()
    nums = set()
    a = c["aggregate"]
    for k in ("mean_delta", "sigma"):
        if a.get(k) is not None:
            nums.add(round(a[k], 4))
    if a.get("n"):
        nums.add(float(a["n"]))
    for p in c["points"]:
        for k in ("eval", "bits", "b2", "delta_vs_floor", "floor"):
            if p.get(k) is not None:
                nums.add(round(p[k], 4))
    # Leaf-kind catalogs (e.g. bitdistiller: WIKI_PPL + commonsense accs) carry their
    # measured values in point["leaves"], NOT in the LDP-style eval/floor fields above.
    # Allow-list every leaf value, and — so a reseed round can cite a firming σ — also
    # the pipeline-computed n/mean/sample-σ of any leaf measured on ≥2 points (the
    # honest aggregate the leaf-kind digest doesn't precompute; the gate then derives
    # a gap vs a journal baseline as |mean − base|).
    leaf_vals = {}
    for p in c["points"]:
        for lk, lv in (p.get("leaves") or {}).items():
            # non-finite leaves (NaN evals of diverged arms) would poison the
            # aggregate stats below — statistics.stdev raises on NaN input
            if isinstance(lv, (int, float)) and math.isfinite(float(lv)):
                nums.add(round(float(lv), 4))
                leaf_vals.setdefault(lk, []).append(float(lv))
    for vs in leaf_vals.values():
        if len(vs) >= 2:
            nums.add(float(len(vs)))
            nums.add(round(statistics.mean(vs), 4))
            nums.add(round(statistics.stdev(vs), 4))
    return nums


# ── journal sink ───────────────────────────────────────────────────────────────
def _journal_section(claim):
    """The existing journal block for a known claim (RE-NN / H1 / C-NN / D1), else
    None. Heuristic: from the first line mentioning the id to the next heading/RE-id.
    Namespace-aware: a 'project:id' claim is resolved within ITS project's segment
    (so FracBits' H1 is found in FRACBITS_*, not LDP's H1)."""
    cid = _bare_id(claim)
    if not re.match(r"(RE-\d+|H[1-9][0-9]?|C\d+|D\d+|Q\d+)$", cid):
        return None
    txt = _project_journal_text(claim).splitlines()
    start = next((i for i, ln in enumerate(txt)
                  if re.search(rf"\b{re.escape(cid)}\b", ln)), None)
    if start is None:
        return None
    end = start + 1
    # `#### D<n>:` decision headings are 4-deep, so the block terminator allows up
    # to 4 hashes (a following decision ends the prior one's span).
    while end < len(txt) and not re.match(r"(#{1,4} |\| ?RE-\d+|\*\*RE-\d+)", txt[end]):
        end += 1
    return "\n".join(txt[start:min(end, start + 30)])


def journal_context(claim, queued=None):
    """Assemble the journal-edit drafting context. When `queued` (a list of planned
    follow-up configs from a `round`) is given, the entry MUST name them in an
    'In flight:' line — the journal records the finding AND the experiments it
    spawned in the same edit (they were decided in the same discussion).

    HALF-ROUND: when the claim has no digest entry (a NEW artifact, no results yet),
    the context flips to hypothesis-drafting mode — the verified facts are the
    experimental DESIGN (the planned overrides), not results, and the entry states
    a prediction + the experiments queued to test it."""
    c = find_claim(claim)
    sec = _journal_section(claim)
    if c is None:                              # half-round: new hypothesis, no results
        return _new_hypothesis_context(claim, queued, sec)
    rec, agg = c["recommendation"], c["aggregate"]
    facts = {"claim": claim, "known": c["known"], "aggregate": agg,
             "points": [{**{k: p.get(k) for k in ("tag", "bits", "eval",
                         "delta_vs_floor", "family", "plot")},
                         "config": p.get("config_sig"),
                         "config_invalid": p.get("config_invalid")}
                        for p in c["points"]]}
    # adapter-5: the machine-derived config each point ran. The RE row MUST carry
    # it (bold ⚙ prefix) so a per-token-collapse cell can't be prose-labeled
    # "uniform W4A4"; the gate re-derives from the job record and fails on drift.
    bad = sorted({p.get("config_invalid") for p in c["points"]
                  if p.get("config_invalid")})
    L = [f"# Draft a journal edit for {claim}", "",
         f"**Recommended action:** {rec['action']} ({rec['confidence']} confidence)",
         f"**Rationale:** {rec['rationale']}", "",
         "## Rules (HARD)", "",
         "- Every number you write must come from the verified facts below or "
         "already be in the journal — the gate FAILS CLOSED on any other number.",
         "- **Config provenance (FAILS CLOSED):** begin the RE row's Description "
         "with the machine-derived config as a bold token — `**⚙ <config>**` — "
         "using the EXACT `config` string from the verified facts below. The gate "
         "re-derives it from the run's job record and rejects a missing/mismatched "
         "prefix. Do NOT paraphrase the config in prose in place of the token.",
         "- Follow science-vs-infra: a science finding is an RE row / H-section "
         "update; infra/tooling is a §4 changelog C-entry, NOT an RE row.",
         "- Convert any relative date to absolute. Minimal edit — tighten n/Δ/σ "
         "on an existing row, or add ONE new RE row; do not rewrite the section."]
    if bad:
        L += ["- **⚠ INVALID-CONFIG RESULT PRESENT — do NOT headline as deployable.** "
              "A point ran a known-collapse/non-deployable config: "
              + "; ".join(bad) + " State it AS a collapse/known-bad cell, never as "
              "the deployable floor."]
    if queued:
        L += ["- **REQUIRED (this is a `round`):** add an `In flight:` line to the "
              "entry naming the experiments queued below (tag + what each varies). "
              "The journal records BOTH the finding and the experiments it spawned. "
              "Tags/config names are not numbers — they pass the gate."]
    L += ["", "## Verified facts (the allow-list)", "", "```json",
          json.dumps(facts, indent=2), "```", ""]
    if queued:
        L += [f"## Experiments queued for {claim} — name these in the `In flight:` line",
              ""]
        L += [f"- `{p['config']}`" + (f"  ({p['overrides']})" if p.get("overrides")
                                       else "") for p in queued]
        L += [""]
    if sec:
        L += [f"## Current journal block for {claim} (edit this)", "", "```",
              sec, "```", ""]
    else:
        L += [f"## No existing journal block for '{claim}'", "",
              "This claim has no journal id yet — if the move is real, draft a NEW "
              "RE row (next free RE-NN) following the evidence-table format.", ""]
    jfile, efile = _sink_files(claim)
    L += ["## After drafting", "",
          f"Save your edit to a file, then gate it:", "",
          f"    python pipeline/propose.py gate {claim} <your_edit_file>", "",
          f"Gate PASS → apply via a reviewed Edit (RE rows → {efile} §2; "
          f"claims H/C/Q → {jfile} §1; full write contract: JOURNAL.md §5.1). "
          "Never auto-commit science."]
    if queued:
        L += ["", f"**THEN — do NOT forget — realize the queued experiments:**", "",
              f"    python pipeline/propose.py round {claim} --finish", "",
              "Until you do, `ingest.py --accept` will REFUSE to advance the "
              "watermark for this round (the experiments aren't running yet)."]
    out = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(CONTEXT), exist_ok=True)
    open(CONTEXT, "w").write(out)
    return out


def _new_hypothesis_context(claim, queued, sec):
    """Half-round drafting context: a NEW artifact with no results yet. The verified
    facts are the experimental DESIGN (the planned overrides), so the entry may state
    the budget/α/T it actually queues — and nothing else, since there are no results
    to cite. The whole point is to mint the hypothesis + name the experiments that
    will test it, in one gated edit."""
    plan = [{"config": p["config"], "overrides": p.get("overrides", "")}
            for p in (queued or [])]
    facts = {"claim": claim, "results": "none yet — new hypothesis",
             "design (the allow-list)": plan}
    L = [f"# Draft a NEW hypothesis for {claim} (half-round — no results yet)", "",
         "You are minting a new artifact and queuing its first experiments — there "
         "are no results to review. State the PREDICTION and the experimental design.",
         "", "## Rules (HARD)", "",
         "- Numbers you write must be journal numbers OR design numbers from the "
         "queued configs below — the gate FAILS CLOSED on anything else. There are "
         "no results yet, so do NOT state eval/Δ/σ outcomes; state the prediction.",
         "- Science vs infra: a hypothesis is a new `H`/`RE`/`Q` entry; infra is a §4 "
         "changelog C-entry. Convert relative dates to absolute. Add ONE entry.",
         "- **REQUIRED:** add an `In flight:` line naming the experiments queued below "
         "(tag + what each varies) — the entry is the spec for what you're running.",
         "", "## Verified facts (the allow-list = the design)", "", "```json",
         json.dumps(facts, indent=2), "```", ""]
    if plan:
        L += [f"## Experiments queued for {claim} — name these in the `In flight:` line",
              ""] + [f"- `{p['config']}`" + (f"  ({p['overrides']})" if p["overrides"]
                                              else "") for p in plan] + [""]
    if sec:
        L += [f"## An entry already mentions {claim} (edit, don't duplicate)", "",
              "```", sec, "```", "",
              "⚠ This id already appears in the journal — if you meant to ADD DATA to "
              "an existing claim, stop and run a full round (with a digest) instead.", ""]
    else:
        L += [f"## No existing journal entry for '{claim}'", "",
              "Draft a NEW entry (next free `RE-NN` / a new `H`/`Q`) in the evidence "
              "format — a prediction + the In flight experiments, no results.", ""]
    jfile, efile = _sink_files(claim)
    L += ["## After drafting", "", "Save your edit to a file, then gate it:", "",
          f"    python pipeline/propose.py gate {claim} <your_edit_file>", "",
          f"Gate PASS → apply via a reviewed Edit (new claim H/Q → {jfile} §1; "
          f"new RE row → {efile} §2; full write contract: JOURNAL.md §5.1; never "
          "auto-commit). **THEN — do NOT forget — realize the experiments:**", "",
          f"    python pipeline/propose.py round {claim} --finish", "",
          "Until you do, `ingest.py --accept` REFUSES to advance over this round."]
    out = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(CONTEXT), exist_ok=True)
    open(CONTEXT, "w").write(out)
    return out


def _planned_numbers(claim):
    """A half-round's design numbers — the verified facts a new-hypothesis entry may
    cite (you can state the α/T/budget/model-size you're actually queuing, nothing
    invented). For a derive-spec these are the override (+ seed) values; for a
    --configs spec they're every number parsed from the pointed config files at
    round-open time (so an entry against brand-new code can cite that design)."""
    spec = _load_rounds().get(claim, {}).get("spec", {})
    nums = set()
    for var in spec.get("variants") or []:
        for v in (var.get("overrides") or {}).values():
            try:
                nums.add(round(float(v), 4))
            except (TypeError, ValueError):
                pass
    for s in spec.get("seeds") or []:
        nums.add(float(s))
    for d in spec.get("design_numbers") or []:   # --configs path (pre-parsed)
        nums.add(round(float(d), 4))
    return nums


# ── R4 artifact anchor (narrow): a number is also sourced when a run artifact
# RESOLVES it via MeasurementCatalog.value(tag, kind). NARROW = only the tags the
# entry itself declares (its `In flight:` / `evidence:` / `Supports:` context) may
# resolve — mirrors the derived_set narrowing, so a number can't be laundered from
# an unrelated coincidental catalog value.
_TAG_RE = re.compile(r"`([A-Za-z0-9][\w./-]+)`")   # backtick-quoted run tag / config


def _catalog_kinds(cat):
    """The measurement kinds resolvable from `cat`. A leaf-kind catalog (bitdistiller)
    advertises `LEAF_KINDS`; a RunLogMeasurement (LDP/FracBits) has EVAL/B2 + any
    extra_kinds. Enumerated so a declared tag resolves against every leaf it owns."""
    leaf = getattr(cat, "LEAF_KINDS", None)
    if leaf:
        return tuple(leaf)
    return ("EVAL", "B2") + tuple(getattr(cat, "_extra", {}) or ())


def _entry_tags(text):
    """Narrow tag set: backtick-quoted run tags on the entry's In flight: /
    evidence: / Supports: lines (the experiments & artifacts this entry declares)."""
    tags = set()
    for line in text.splitlines():
        if re.search(r"in[-\s]?flight:|evidence:|supports:", line, re.I):
            tags |= set(_TAG_RE.findall(line))
    return tags


def _artifact_numbers(text, claim):
    """Numbers machine-resolved from run artifacts for the entry's declared tags
    (narrow). A value is artifact-sourced iff MeasurementCatalog.value(tag, kind)
    returns it for some declared tag and some catalog kind — a stronger anchor than
    journal-text membership (R4). Empty when the entry declares no resolvable tag,
    or the project has no MeasurementCatalog (e.g. smh)."""
    tags = _entry_tags(text)
    if not tags:
        return set()
    try:
        from smh.pipeline.measure import measurement_for
        cat = measurement_for(_project_of(claim))
    except Exception:
        return set()
    nums = set()
    for tag in tags:
        for kind in _catalog_kinds(cat):
            try:
                v = cat.value(tag, kind)
            except Exception:
                v = None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                nums.add(round(float(v), 4))
    return nums


def gate_journal_edit(text, claim):
    """Fail-closed number check. Allow-list = journal numbers ∪ the claim's verified
    digest facts (a full round) ∪ the claim's queued design numbers (a half-round) ∪
    numbers a run ARTIFACT resolves for the entry's declared tags (R4 — machine-
    anchored, so a half-round may cite a measured value WHEN the artifact backs it,
    without laundering it through prose). The half-round boundary is thus its honest
    form — no verdict/status claims without a journaled round, verdict-language rules
    unchanged — not 'no numbers that look like results'. Structural holes are
    project-scoped (gate.structural_for). Returns gate.verify()'s dict."""
    jset = _project_journal_numbers(claim) | digest_numbers(claim) | _planned_numbers(claim)
    # SCOPE KNOB (off): gate.verify accepts derived_set= to confine pairwise gap
    # derivation to this claim's own numbers (digest ∪ planned), stopping a result
    # being manufactured from two coincidental journal values. Left broad for now —
    # narrowing can false-FAIL a legit gap computed from two journal refs, so
    # enable + tune against real drafts:
    #   derived_set=digest_numbers(claim) | _planned_numbers(claim)
    res = _gate.verify(text, jset, structural=_gate.structural_for(_project_of(claim)),
                       artifact_set=_artifact_numbers(text, claim))
    # adapter-5 config-provenance teeth: the RE row must carry each result's
    # machine-derived config and disclose any known-collapse config (fail-closed
    # on drift / undisclosed-invalid). Facts come from the claim's digest points.
    cfg = _gate.config_check(text, _claim_configs(claim))
    res["config"] = cfg
    res["ok"] = res["ok"] and cfg["ok"]
    # metric-provenance lint (label→basis, ADVISORY): a labeled number must match
    # its label's catalog basis, not a confusable sibling basis (the RE-39 class:
    # "core6" numbers that were lm-default-6). Never flips `ok` yet — the
    # fail-closed flip for new rows is a pending user decision
    # (HANDOFF_metric_provenance_gate Phase 3). No-op for catalogs without
    # METRIC_LEXICON (LDP/FracBits).
    try:
        from smh.pipeline import metric_lint
        from smh.pipeline.measure import measurement_for
        res["metric"] = metric_lint.metric_check(
            text, measurement_for(_project_of(claim)))
    except Exception:
        res["metric"] = None
    return res


def _claim_configs(claim):
    """The distinct machine-derived config signatures for a claim's digest points:
    `[{"sig", "invalid"}]`. Empty when the claim has no results in the digest
    (half-round) or none carried a config — the config gate then no-ops."""
    c = find_claim(claim)
    if not c:
        return []
    seen, out = set(), []
    for p in c.get("points", []):
        # canonical string (config_sig_plain), NOT the sweep-augmented display
        # config_sig — a prose token can never equal the augmented form (it embeds
        # every swept knob), which false-FAILs the drift check on any mixed
        # (PTQ+KD) claim. Fall back for pre-plain digests.
        sig = p.get("config_sig_plain") or p.get("config_sig")
        if sig and sig not in seen:
            seen.add(sig)
            out.append({"sig": sig, "invalid": p.get("config_invalid")})
    return out


# ── queue sink (the loop-closer) ───────────────────────────────────────────────
def _tag_config(tag):
    """Locate the config path a finished tag was produced from (done record)."""
    for p in glob.glob(os.path.join(DONE, "*.json")):
        try:
            j = json.load(open(p))
        except (ValueError, OSError):
            continue
        if j.get("tag") == tag and j.get("config"):
            return j["config"]            # e.g. owt_int248b/int248b_lo_h3.5_s6
    return None


def _inject_goal(text, claim, kind, gtext):
    """Add/replace a single-line flow `goal:` block at the top of a flow-yaml
    config. Forward-compatible with HANDOFF_experiment_goals (config = source of
    truth); ignored by the current sched.py, validated once goals land."""
    block = f"goal: {{kind: {kind}, claim: {claim}, text: {gtext}}}"
    if re.search(r"^goal:", text, re.M):
        return re.sub(r"^goal:.*$", block, text, count=1, flags=re.M)
    return block + "\n" + text


def _set_field(text, key, value):
    """Override a single flow-yaml scalar/list field in place (`key: old` ->
    `key: value`). Returns (text, found). Matches both scalars and `[...]` lists."""
    pat = re.compile(rf"(\b{re.escape(key)}:\s*)(\[[^\]]*\]|[^,}}\n]+)")
    if not pat.search(text):
        return text, False
    return pat.sub(rf"\g<1>{value}", text, count=1), True


def _reseed(text, name, new_seed):
    """Sugar: clone a config to a new seed (the degenerate single-field override)."""
    text, _ = _set_field(text, "seed", new_seed)
    if re.search(r"_s\d+$", name):
        name = re.sub(r"_s\d+$", f"_s{new_seed}", name)
    else:
        name = f"{name}_s{new_seed}"
    return text, name


def _derive(base_text, base_name, overrides, new_name=None):
    """Author a NEW experiment from a base config: apply arbitrary field overrides
    (alpha, T, formats, scale_gran, …), name it. A missing field is an error (we
    don't silently no-op a requested change). Seed-only override auto-names `_sN`."""
    text = base_text
    for k, v in overrides.items():
        text, found = _set_field(text, k, v)
        if not found:
            raise KeyError(f"field '{k}' not in base config — can't override safely")
    if new_name:
        name = new_name
    elif list(overrides) == ["seed"]:
        s = overrides["seed"]
        name = (re.sub(r"_s\d+$", f"_s{s}", base_name) if re.search(r"_s\d+$", base_name)
                else f"{base_name}_s{s}")
    else:
        raise ValueError("a non-seed override needs an explicit name (--name)")
    return text, name


def followups(claim, seeds=None, variants=None, kind="science", base_tags=None,
              write=False, gtext=None):
    """Plan (and optionally write) follow-up EXPERIMENTS for a claim — new arms,
    budget rungs, grids, estimators, or just more seeds — each cloned from a base
    config and stamped goal.claim=<claim>. Closes the loop: their results
    auto-group under the claim in the next ingest.

    variants: list of {overrides:{field:value,...}, name:str|None} — the general
              case (new experiments). `seeds` is sugar for seed-only variants.
    base_tags: tags whose configs to derive from; default = the claim's points.
    """
    if variants is None:
        if not seeds:
            sys.exit("error: pass --seeds (sugar) or --from/--set/--name (new arm)")
        variants = [{"overrides": {"seed": s}, "name": None} for s in seeds]
    if base_tags is None:                  # default = the claim's plotted points
        c = claim_entry(claim)
        base_tags = [p["tag"] for p in c["points"] if p.get("plot") == "plotted"]
    if not base_tags:
        sys.exit(f"error: no base tags for '{claim}' (pass --from/--base-tags)")
    if gtext is None:
        gtext = f"{claim} follow-up ({len(variants)} variant/s)"
    plans, seen = [], set()
    for tag in base_tags:
        cfg = _tag_config(tag)
        if not cfg or cfg in seen:
            continue
        seen.add(cfg)
        src = os.path.join(CONFIGS, cfg + ".yaml")
        if not os.path.exists(src):
            continue
        base_text = open(src).read()
        cdir, cname = cfg.split("/", 1) if "/" in cfg else ("", cfg)
        for var in variants:
            text, newname = _derive(base_text, cname, var["overrides"], var.get("name"))
            text = _inject_goal(text, claim, kind, gtext)
            relcfg = f"{cdir}/{newname}" if cdir else newname
            dst = os.path.join(CONFIGS, relcfg + ".yaml")
            ov = var["overrides"]
            plan = {"config": relcfg, "path": dst,
                    "overrides": ",".join(f"{k}={v}" for k, v in ov.items()),
                    "submit": f"python scheduler/sched.py submit {relcfg}",
                    "exists": os.path.exists(dst)}
            if write and not plan["exists"]:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                open(dst, "w").write(text)
            plans.append(plan)
    return plans


# ── round (the loop-correct combined move) ──────────────────────────────────────
# A `round` is the realistic unit of work: in ONE discussion you decide the journal
# update AND the experiments that follow from it. Formally the claim must exist in
# the journal before its science experiments can be queued (the goals handoff
# validates kind==science → claim ∈ journal), so the order is: draft the journal
# entry (which NAMES the experiments, in flight), apply it, THEN realize the queue.
# The round leaves a pending record so the queue step can't be silently skipped —
# `ingest.py --accept` refuses to advance the watermark while a round is open.
ROUND_STATE = state_path(ROOT, "round_state.json")


def _load_rounds():
    if os.path.exists(ROUND_STATE):
        try:
            return json.load(open(ROUND_STATE))
        except (ValueError, OSError):
            pass
    return {}


def _save_rounds(d):
    os.makedirs(os.path.dirname(ROUND_STATE), exist_ok=True)
    json.dump(d, open(ROUND_STATE, "w"), indent=2, sort_keys=True)


def open_rounds():
    """Claims whose round has been started (journal entry drafted) but whose
    experiments have NOT been queued yet — the un-forgettable debt."""
    return sorted(c for c, v in _load_rounds().items()
                  if v.get("status") != "queued")


def resolve_queue_spec(a):
    """The shared --seeds | --from/--set/--name parsing for both `queue` and
    `round`. Returns followups() kwargs (sans claim/write)."""
    bt = a.base_tags.split(",") if a.base_tags else None
    if a.overrides:                            # new-arm path
        if not a.frm or not a.name:
            sys.exit("error: --set needs --from <base tag> and --name <new tag>")
        ov = {}
        for kv in a.overrides.split(","):
            k, _, v = kv.partition("=")
            ov[k.strip()] = v.strip()
        return {"variants": [{"overrides": ov, "name": a.name}], "kind": a.kind,
                "base_tags": [a.frm],
                "gtext": f"{a.claim}: new arm {a.name} ({a.overrides})"}
    if a.seeds:                                # seed sugar
        return {"seeds": [int(s) for s in a.seeds.split(",") if s.strip()],
                "kind": a.kind, "base_tags": bt, "gtext": None}
    sys.exit("error: pass --seeds (sugar) or --from/--set/--name (new arm)")


def _config_numbers(relpath):
    """Every number in a config file's text — the design an entry pointing at it may
    cite. Reused for the gate allow-list (a --configs half-round, possibly against
    brand-new code, has no base/override to derive design numbers from)."""
    src = os.path.join(CONFIGS, relpath + ".yaml")
    if not os.path.exists(src):
        return set()
    return {round(float(m), 4) for m in NUM_RE.findall(open(src).read())}


def _pointed_plans(relpaths):
    """Plans for configs that ALREADY EXIST (authored by hand, often against new
    code) — `round --configs`. No derivation: validate each, build its submit line."""
    plans = []
    for rel in relpaths:
        src = os.path.join(CONFIGS, rel + ".yaml")
        if not os.path.exists(src):
            sys.exit(f"error: config '{rel}' not found at {src} — author it first "
                     "(write the YAML, and any new mode it needs, before pointing here)")
        plans.append({"config": rel, "path": src, "overrides": "",
                      "submit": f"python scheduler/sched.py submit {rel}",
                      "exists": True})
    return plans


def start_round(claim, spec):
    """Plan (dry-run) the queue, record it as a PENDING round, and build a journal
    context that names the planned experiments. Returns (plans, context, half).
    `half` is True when the claim has no digest entry — a half-round (new artifact,
    no results), where the journal context drafts a hypothesis from the design.

    spec is either a derive-spec ({variants|seeds, base_tags, kind, gtext}) or a
    point-spec ({configs:[relpaths], kind, gtext}) for configs that already exist."""
    if "configs" in spec:
        plans = _pointed_plans(spec["configs"])
        spec = {**spec, "design_numbers": sorted(set().union(
            *(_config_numbers(c) for c in spec["configs"])) or set())}
    else:
        plans = followups(claim, write=False, **spec)
    if not plans:
        sys.exit(f"error: no experiments planned for '{claim}' "
                 "(check --from/--seeds/--configs)")
    half = find_claim(claim) is None
    rounds = _load_rounds()
    rounds[claim] = {"status": "journal-pending", "spec": spec, "half": half,
                     "configs": [p["config"] for p in plans]}
    _save_rounds(rounds)
    ctx = journal_context(claim, queued=plans)
    return plans, ctx, half


def finish_round(claim):
    """Realize a pending round: WRITE/validate the configs + return the submit lines,
    then mark the round queued (clears any ingest --accept block). A derive-spec
    writes the cloned configs; a --configs point-spec just re-validates the existing
    ones (their code/design was authored by hand) and prints the submits."""
    rounds = _load_rounds()
    if claim not in rounds:
        sys.exit(f"error: no open round for '{claim}' — start one with "
                 f"`propose.py round {claim} --from … --set … --name …` or --configs")
    spec = rounds[claim]["spec"]
    if "configs" in spec:
        plans = _pointed_plans(spec["configs"])   # re-validate they still exist
    else:
        plans = followups(claim, write=True, **{k: v for k, v in spec.items()
                                                if k != "design_numbers"})
    rounds[claim]["status"] = "queued"
    _save_rounds(rounds)
    return plans


def cancel_round(claim):
    rounds = _load_rounds()
    if rounds.pop(claim, None) is None:
        sys.exit(f"error: no open round for '{claim}'")
    _save_rounds(rounds)


# ── CLI ────────────────────────────────────────────────────────────────────────
def _add_queue_args(p):
    """The --seeds | --from/--set/--name spec shared by `queue` and `round`."""
    p.add_argument("--seeds", help="sugar: comma-separated new seeds, e.g. 7,8,9")
    p.add_argument("--from", dest="frm", help="base tag to derive a new arm from")
    p.add_argument("--set", dest="overrides",
                   help="new-arm field overrides, e.g. alpha=3,T=4.5,scale_gran=token")
    p.add_argument("--name", help="explicit tag for the new arm (required with --set)")
    p.add_argument("--kind", default="science", choices=["science", "scaffold"])
    p.add_argument("--base-tags", help="comma-separated base tags (default: claim's points)")


def _print_plans(claim, plans, write):
    print(f"# Follow-up experiments for {claim} "
          f"({'WROTE configs' if write else 'DRY-RUN — add --write'}):\n")
    for p in plans:
        mark = "exists" if p["exists"] else ("written" if write else "new")
        print(f"  [{mark:>7}] {p['config']}")
    print("\n# submit (carries goal.claim → next ingest auto-groups results):\n")
    for p in plans:
        print(p["submit"])


def main():
    ap = argparse.ArgumentParser(description="Phase-2: act on a digest decision.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("journal", help="assemble journal-edit drafting context")
    j.add_argument("claim")
    g = sub.add_parser("gate", help="gate a drafted journal edit (fail-closed)")
    g.add_argument("claim"); g.add_argument("edit_file")
    q = sub.add_parser("queue", help="plan follow-up experiments for a claim")
    q.add_argument("claim")
    _add_queue_args(q)
    q.add_argument("--write", action="store_true", help="write configs (default: dry-run)")
    r = sub.add_parser("round", help="combined journal+queue move (the loop unit): "
                       "draft an entry that NAMES the experiments, then realize them")
    r.add_argument("claim")
    _add_queue_args(r)
    r.add_argument("--configs",
                   help="comma-separated relpaths of configs that ALREADY EXIST "
                        "(authored by hand, often against new code) — point at them "
                        "instead of deriving from a base; the half-round entry point")
    r.add_argument("--finish", action="store_true",
                   help="realize a pending round's experiments (write configs + submits)")
    r.add_argument("--cancel", action="store_true", help="drop a pending round")
    a = ap.parse_args()
    _echo_project(a.claim)

    if a.cmd == "journal":
        sys.stdout.write(journal_context(a.claim))
    elif a.cmd == "gate":
        res = gate_journal_edit(open(a.edit_file).read(), a.claim)
        print(_gate.report(res))
        if res.get("config"):
            print(_gate.config_report(res["config"]))
        if res.get("metric") and res["metric"].get("enabled"):
            from smh.pipeline.metric_lint import metric_report
            print(metric_report(res["metric"]))
        sys.exit(0 if res["ok"] else 1)
    elif a.cmd == "queue":
        plans = followups(a.claim, write=a.write, **resolve_queue_spec(a))
        if not plans:
            sys.exit(f"error: no follow-ups generated for '{a.claim}'")
        _print_plans(a.claim, plans, a.write)
    elif a.cmd == "round":
        if a.cancel:
            cancel_round(a.claim)
            print(f"# round for {a.claim} cancelled.")
            return
        if a.finish:                           # realize the queued experiments
            plans = finish_round(a.claim)
            print(f"# round {a.claim} FINISHED — experiments realized.\n")
            _print_plans(a.claim, plans, write=True)
            print(f"\n# submit them, then later: python pipeline/ingest.py --accept")
            return
        # start: plan the queue (dry-run) + build a journal context naming them.
        # --configs points at already-authored configs (the from-scratch / new-code
        # path); otherwise derive from a base (--from/--seeds).
        if a.configs:
            spec = {"configs": [c.strip() for c in a.configs.split(",") if c.strip()],
                    "kind": a.kind, "gtext": f"{a.claim}: experiments {a.configs}"}
        else:
            spec = resolve_queue_spec(a)
        plans, _, half = start_round(a.claim, spec)
        if half:
            print(f"# HALF-ROUND for {a.claim} — '{a.claim}' is NOT in the digest, so "
                  "this is treated as a\n#   NEW hypothesis (no results). Drafting a "
                  "prediction + its first experiments.\n#   (If you meant an existing "
                  "claim, check the spelling / run a full round.)\n")
        else:
            print(f"# ROUND opened for {a.claim} — journal entry FIRST, then queue.\n")
        print(f"Drafting context (names the experiments) → {CONTEXT}\n")
        _print_plans(a.claim, plans, write=False)
        # a from-scratch (half) round may stay open for days while you write code +
        # configs, so it does NOT block ingest --accept — it surfaces as a reminder.
        blocks = " (blocks ingest --accept until done)" if not half else ""
        print(f"\n# NEXT (in order):")
        print(f"#  1. draft the journal {'hypothesis' if half else 'edit'} from "
              f"{os.path.basename(CONTEXT)} — INCLUDE an 'In flight:' line naming the "
              "experiments above")
        print(f"#  2. python pipeline/propose.py gate {a.claim} <edit_file>")
        print(f"#  3. apply via a reviewed Edit (claim → JOURNAL.md §1; RE row → EVIDENCE.md §2)")
        print(f"#  4. python pipeline/propose.py round {a.claim} --finish   "
              "← DON'T SKIP: this submits the experiments")
        if half:
            print("#\n# (half-round: stays a digest REMINDER until step 4 — does not "
                  "block ingest --accept, since new code may take a while.)")
        else:
            print(f"#\n# ingest.py --accept will REFUSE to advance until step 4 is done.")


if __name__ == "__main__":
    main()
