"""Layer B (part 2) — the HARD FACT GATE.

Because an LLM writes the narrative, it must not invent or drift numbers. This
verifies every numeric token in a generated presentation against the journal's
canonical number set (`facts.journal_numbers`). A presentation with an unsourced
substantive number FAILS and does not ship (fail-closed).

Matching rules (a generated number `g` is sourced iff any holds):
  - exact: some journal value == g
  - rounding: some journal value rounds to g at g's stated precision
              (lets the prose write 3.787 for the journal's 3.7874)
  - derived: g equals a simple function (sqrt / sum / diff) of journal values
              (e.g. effective bits 4.4 = sqrt(19.3); gap +0.024 = 3.968-3.944)
  - structural: small integers / years / layer counts on an allow-list
              (§ numbers, 6L/512, 2026, σ multiples) — not factual claims

Unsourced numbers are returned for review; `ok` is False if any remain.
"""
import os
import sys
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from smh.pipeline.facts import journal_numbers, NUM_RE, number_matches, to_decimal
from smh.config import resolve_root, load_config

_ROOT = resolve_root(__file__.rsplit("/", 3)[0])

# The IRREDUCIBLE universal structural core — integers non-factual in ANY project
# (single digits used as counts / § numbers / σ multiples, and the current year).
# Everything project- or arch-specific (layer widths, arxiv-id digits, past years)
# is NOT hardcoded here: it lives in smh.toml as a top-level `structural` list
# (shared holes) + optional `[projects.<name>].structural` (per-project holes), so
# adding a structural hole is a VISIBLE, scoped config edit — never a silent global
# grant (R4, SPEC_journal_v2_drift_resistance).
_CORE_STRUCTURAL = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2026}


@lru_cache(maxsize=None)
def structural_for(project=None):
    """The structural allow-set for `project`: the universal core ∪ smh.toml's
    top-level `structural` (shared) ∪ `[projects.<project>].structural` (scoped).
    Config-absent (a stranger's repo without the key) → the core only."""
    nums = set(_CORE_STRUCTURAL)
    cfg = load_config(_ROOT) or {}
    nums |= {int(n) for n in cfg.get("structural", [])}
    if project:
        pc = (cfg.get("projects") or {}).get(project) or {}
        nums |= {int(n) for n in pc.get("structural", [])}
    return nums


# Back-compat: the default (project-less) structural set. With the arch/year ints
# now declared in smh.toml, this equals the historical hardcoded set byte-for-byte,
# so every existing caller that omits `structural=` is unchanged.
STRUCTURAL = structural_for(None)
_STRUCTURAL_DEC = {Decimal(n) for n in STRUCTURAL}


def _derived(tok, jset, dset):
    """True if `tok` is sqrt of a journal value (effective bits — drawn from the
    BROAD jset) or a sum / |difference| of a pair from `dset` (gaps, penalties).
    Splitting the sets is the teeth: dset is the claim-scoped number set when the
    caller passes one, so a gap can't be MANUFACTURED from two coincidental,
    unrelated journal numbers. dset defaults to jset -> legacy behavior."""
    d = to_decimal(tok)
    if d is None:
        return False
    q = Decimal(1).scaleb(-(-(d.as_tuple().exponent) if d.as_tuple().exponent < 0 else 0))
    js = [x for x in (to_decimal(j) for j in jset) if x is not None]
    with localcontext() as ctx:                       # effective bits: sqrt(jv)
        ctx.prec = 28
        for j in js:
            if j > 0 and j.sqrt().quantize(q, ROUND_HALF_EVEN) == d:
                return True
    ds = [x for x in (to_decimal(j) for j in dset) if x is not None]
    for i, a in enumerate(ds):                         # pairwise sum / |diff|
        for b in ds[i:]:
            if (a + b).quantize(q, ROUND_HALF_EVEN) == d \
                    or abs(a - b).quantize(q, ROUND_HALF_EVEN) == d:
                return True
    return False


def verify(text, jset=None, derived=True, derived_set=None, structural=None,
           artifact_set=None):
    """Return {ok, matched, unsourced:[(token, line_excerpt)]}. A number is sourced
    iff it is structural, exact/round-matches `jset` (broad), is a `derived` fact
    (sqrt / gap) whose pairwise operands come from `derived_set` (pass a NARROW,
    claim-scoped set to stop a gap being assembled from unrelated journal pairs;
    defaults to jset), OR exact/round-matches `artifact_set` — numbers MACHINE-
    RESOLVED from run artifacts (MeasurementCatalog.value), which is a STRONGER
    anchor than journal-text membership (R4). `structural` overrides the default
    core∪config set for a project-scoped gate (see structural_for)."""
    jset = jset if jset is not None else journal_numbers()
    dset = derived_set if derived_set is not None else jset
    struct = _STRUCTURAL_DEC if structural is None else {Decimal(n) for n in structural}
    aset = list(artifact_set or [])
    unsourced, matched = [], 0
    for m in NUM_RE.finditer(text):
        tok = m.group(1)
        d = to_decimal(tok)
        if d is None:
            continue
        if d in struct or any(number_matches(tok, jv) for jv in jset) \
                or any(number_matches(tok, a) for a in aset):
            matched += 1
            continue
        if derived and _derived(tok, jset, dset):
            matched += 1
            continue
        ln = text[:m.start()].count("\n") + 1
        excerpt = text.splitlines()[ln - 1].strip()[:80] if ln <= text.count("\n") + 1 else ""
        unsourced.append((tok, ln, excerpt))
    return {"ok": not unsourced, "matched": matched, "unsourced": unsourced}


def report(result):
    lines = [f"FACT GATE: {result['matched']} numbers sourced, "
             f"{len(result['unsourced'])} unsourced -> "
             f"{'PASS' if result['ok'] else 'FAIL'}"]
    for tok, ln, ex in result["unsourced"]:
        lines.append(f"  L{ln}: {tok!r:>10}  | {ex}")
    return "\n".join(lines)


# ── CONFIG PROVENANCE GATE (adapter-5) ─────────────────────────────────────────
# The teeth that stop an invalid-config result winning a headline (Q12/RE-21). The
# CONFIG SIGNATURES a claim's results ran are machine-derived from the job records
# (`config_sig.signature`), never the prose; this checks the drafted RE text
# against them. Pure string logic here (SMH-general) — the caller supplies the
# facts. A config token is written `**⚙ <signature>**` at the head of a Description.
import re as _re
_CFG_TOKEN_RE = _re.compile(r"\*\*\s*⚙\s*(.+?)\s*\*\*")


def config_check(text, configs):
    """Verify a drafted journal edit's config provenance against `configs` — the
    machine-derived signatures of the claim's results, a list of
    `{"sig": str, "invalid": str|None}`.

    Fail-closed on the two integrity failures:
      * **drift** — a `**⚙ X**` token in the prose whose X is NOT a real signature
        of any of the claim's runs (an invented / mislabeled config).
      * **undisclosed invalid** — a run whose config is known-invalid/collapse
        (`invalid` set) but whose signature never appears in the text, so a reader
        can't see a non-deployable cell is in the results.
    A `missing` token (a valid-config claim with no ⚙ token at all) is a WARNING,
    not a failure — it doesn't corrupt the record, and forcing a token onto every
    legacy row edit would false-FAIL. `ok` is False only for drift/undisclosed."""
    real = {c["sig"] for c in configs if c.get("sig")}
    if not real:                                  # config-less claim (nothing to check)
        return {"ok": True, "tokens": [], "drift": [], "undisclosed": [],
                "missing": False, "warn": []}
    tokens = [t.strip() for t in _CFG_TOKEN_RE.findall(text)]
    drift = sorted({t for t in tokens if t not in real})
    invalid = [c for c in configs if c.get("invalid")]
    undisclosed = sorted({c["sig"] for c in invalid if c["sig"] not in text})
    missing = not tokens
    ok = not drift and not undisclosed
    warn = []
    if missing:
        warn.append("no `**⚙ config**` token — every result row should carry its "
                    "machine-derived config prominently")
    return {"ok": ok, "tokens": tokens, "drift": drift,
            "undisclosed": undisclosed, "missing": missing, "warn": warn}


def config_report(res):
    status = "PASS" if res["ok"] else "FAIL"
    lines = [f"CONFIG GATE: {len(res['tokens'])} config token(s) -> {status}"]
    for x in res["drift"]:
        lines.append(f"  DRIFT: `⚙ {x}` is not a real signature of this claim's runs")
    for x in res["undisclosed"]:
        lines.append(f"  UNDISCLOSED INVALID: `{x}` ran but is not shown on the row "
                     f"(a known-collapse/non-deployable config must be disclosed)")
    for w in res["warn"]:
        lines.append(f"  warn: {w}")
    return "\n".join(lines)


def main(argv=None):
    """`smh gate <file> [--project P] [--all-projects]` — fact-gate a drafted
    presentation/edit. The allow-list is PROJECT-SCOPED: default = the resolved
    current project's segment numbers (five-tier resolver, same chain as the mint
    verbs), `--project` overrides, `--all-projects` unions every co-located
    segment (a cross-project deck). Structural holes scope with the project
    (`structural_for`). Echoes `[project: X (tier)]` to stderr when the project
    came from a default tier, like propose/mint — a mis-pin is visible."""
    import argparse
    from smh import journal_xref as jx
    from smh.config import resolve_current_project

    ap = argparse.ArgumentParser(
        prog="smh gate",
        description="Fact-gate a file's numbers against the journal allow-list.")
    ap.add_argument("file", nargs="?", default="Reports/report_v2_draft.md",
                    help="markdown file to gate")
    ap.add_argument("--project", default=None,
                    help="gate against this project's segment (default: current project)")
    ap.add_argument("--all-projects", action="store_true",
                    help="gate against the union of every co-located project's numbers")
    args = ap.parse_args(argv)

    if args.all_projects:
        from smh.pipeline.facts import all_project_numbers
        jset = all_project_numbers(_ROOT)
        struct = set()
        for proj, _files in jx.resolved_ledger(_ROOT):
            struct |= structural_for(proj)
        scope = "all-projects"
    else:
        project, source = resolve_current_project(_ROOT, explicit=args.project)
        seg = jx.segment_text(project, _ROOT)
        if seg is None:
            sys.stderr.write(f"gate: unknown project {project!r} — no ledger segment\n")
            return 2
        if source not in ("explicit", "repo default"):
            sys.stderr.write(f"[project: {project} ({source})]\n")
        jset = journal_numbers(text=seg)
        struct = structural_for(project)
        scope = project
    text = open(args.file).read()
    res = verify(text, jset, structural=struct)
    print(f"[{args.file}] (scope: {scope})")
    print(report(res))
    # metric-provenance lint (ADVISORY, single-project scope only): label→basis
    # consistency via the project's MeasurementCatalog. No-op when the catalog
    # has no METRIC_LEXICON (LDP/FracBits) or the scope is all-projects. Never
    # affects the exit code — the fail-closed flip is a pending user decision
    # (HANDOFF_metric_provenance_gate Phase 3).
    if not args.all_projects:
        try:
            from smh.pipeline import metric_lint
            from smh.pipeline.measure import measurement_for
            mres = metric_lint.metric_check(text, measurement_for(scope))
            if mres["enabled"]:
                print(metric_lint.metric_report(mres))
        except ModuleNotFoundError as e:
            # A project with NO MeasurementCatalog registered (a fresh `smh init`
            # scaffold, a non-numeric domain) has nothing to lint — silent, not
            # a leak of the reference-repo fallback module name onto stderr.
            if os.environ.get("SMH_DEBUG"):
                sys.stderr.write(f"metric lint skipped: {e}\n")
        except Exception as e:
            sys.stderr.write(f"metric lint skipped: {e}\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
