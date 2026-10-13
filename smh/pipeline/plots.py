"""Phase 3 of result ingestion (docs/HANDOFF_result_ingestion.md): the plot side
of the loop. Two halves, split by the same compute-vs-judgment seam as Phases 1/2:

  refresh    — KNOWN-family runs are already globbed by figdata the moment their
               log + betas land, so the only action is to re-render. Plots are
               regenerable derivatives → this MAY run unattended (unlike the
               journal, which is curated prose and never auto-commits).

  orphans /  — an ORPHAN run (a tag matching no plotted family) needs a figdata
  add-family   group tuple (name, color, symbol, glob, decoder). decoder comes
               from figdata.decode_for(tag) automatically; the color is the first
               free slot in a palette; only name/marker/glob are taste. The
               helper PROPOSES the tuple; `add-family --write` appends it to
               figdata._honest_groups()/_llama_groups() at a sentinel anchor.
               This is the one irreducibly manual (taste) step, kept deliberately
               small — the human confirms, the helper does the mechanical edit.

CLI:
  plots.py orphans                       propose a figdata tuple per digest orphan
  plots.py add-family <tag> [opts]       show the proposed tuple (dry-run)
  plots.py add-family <tag> --write      append it to figdata.py
  plots.py refresh [--report]            re-render known-family plots (PNG+Plotly)
"""
import argparse
import json
import os
import re
import subprocess
import sys

from smh.config import resolve_root, state_path
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REPORTS = os.path.join(ROOT, "Reports")

DIGEST_JSON = state_path(ROOT, "ingest_digest.json")
FIGDATA = os.path.join(REPORTS, "figdata.py")

# Free colors for orphan families — chosen to NOT collide with the tab10-ish
# palette already burned into _groups/_honest_groups/_llama_groups. The helper
# picks the first one not already in use; the human can override with --color.
PALETTE = ["#393b79", "#637939", "#8c6d31", "#843c39", "#7b4173", "#00a0b0",
           "#cc333f", "#eb6841", "#5254a3", "#a55194", "#bd9e39", "#6b6ecf"]
# Default markers for orphans (valid Plotly symbols; pareto_honest_png MK maps a
# subset to matplotlib and falls back to "o" for the rest — color disambiguates).
SYMBOLS = ["hexagon", "hexagon2", "star-triangle-up", "diamond-tall",
           "pentagon", "triangle-down"]


# ── instance figure-family maps (LDP: Reports/figdata.py) ────────────────────────
def _figdata():
    """Lazily import the instance's figure-family module (LDP: `Reports/figdata.py`).
    It is instance-specific AND lives OUTSIDE the `smh/` package (`Reports/` is
    gitignored), so it is imported on demand — never at module load — so
    `import smh.pipeline.plots` keeps working in a packaged/subtree-split checkout
    that ships no figdata. Only the orphan-classification paths (`used_colors`,
    `propose_tuple`) touch it; LDP's in-repo behavior is unchanged (figdata resolves
    off `Reports/` exactly as before). A stranger's SMH repo puts its own figdata on
    `sys.path`."""
    if REPORTS not in sys.path:
        sys.path.insert(0, REPORTS)
    import figdata
    return figdata


def __getattr__(name):
    """Expose `figdata` as a LAZY module attribute (PEP 562): `plots.figdata`
    resolves on first access via `_figdata()` without importing it at module load,
    so external callers/tests keep referencing `plots.figdata` while a subtree-split
    checkout still imports this module cleanly. Internal code uses `_figdata()`
    directly (a bare global name never triggers module `__getattr__`)."""
    if name == "figdata":
        return _figdata()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── orphan → proposed figdata tuple ─────────────────────────────────────────────
def used_colors():
    """Every color hex currently burned into a figdata group function, so the
    orphan helper can hand out a non-colliding one."""
    figdata = _figdata()
    cols = set()
    for fn in (figdata._groups, figdata._honest_groups, figdata._llama_groups):
        try:
            for tup in fn():
                cols.add(tup[1])
        except Exception:                       # a half-built figdata mustn't crash us
            pass
    return cols


def free_color(used=None):
    used = used_colors() if used is None else used
    for c in PALETTE:
        if c not in used:
            return c
    return PALETTE[0]                           # palette exhausted — reuse (rare)


def infer_model(tag):
    """owt vs llama, the figdata function the tuple lands in. `llama` substring is
    the strong signal; everything else is owt. Taste — overridable with --model."""
    return "llama" if "llama" in tag else "owt"


def propose_glob(tag):
    """A _tags() glob that generalizes a single orphan tag into a family: drop the
    trailing seed (`_s5`), widen the alpha arm (`_a2` → `_a*`), ensure a trailing
    `*`. A first guess — the human tightens it with --glob if it over/under-matches."""
    g = re.sub(r"_s\d+$", "", tag)              # the seed is per-point, not per-family
    g = re.sub(r"_a\d+(?:\.\d+)?", "_a*", g)    # the alpha arm spans the family
    if not g.endswith("*"):
        g += "*"
    return g


def propose_tuple(tag, model=None, name=None, color=None, symbol=None, glob=None,
                  kind=None, baseline=False, used=None):
    """Propose a figdata group tuple for an orphan tag. Returns a dict with every
    resolved field + the exact source line to insert + the decoder (informational).
    Only name/color/symbol/glob are taste; decoder is figdata.decode_for(tag)."""
    model = model or infer_model(tag)
    glob = glob or propose_glob(tag)
    color = color or free_color(used)
    symbol = symbol or SYMBOLS[0]
    decoder = _figdata().decode_for(tag).__name__  # the honest β→bits decoder, automatic
    # owt _honest_groups 5th element = a routing flag: "uni" reads eff-bits straight
    # from the tag (static-uniform, no betas), "betas" decodes the trajectory. A
    # learned/random allocation run is "betas" regardless of which decoder it uses.
    kind = kind or "betas"
    if name is None:
        stem = glob.replace("*", "").rstrip("_") or tag
        name = f"{stem} (ingested)"
    line = family_line(model, name, color, symbol, glob, kind, baseline)
    return {"tag": tag, "model": model, "name": name, "color": color,
            "symbol": symbol, "glob": glob, "kind": kind, "baseline": baseline,
            "decoder": decoder, "line": line}


def family_line(model, name, color, symbol, glob, kind, baseline):
    """The single source line for a group tuple, matching each function's arity:
    owt/honest 5th = a "uni"/"betas" routing string; llama 5th = a baseline bool."""
    head = f'        ("{name}", "{color}", "{symbol}", _tags("{glob}"), '
    return head + (f"{bool(baseline)})," if model == "llama" else f'"{kind}"),')


def orphan_proposals(digest=None):
    """One proposed tuple per orphan tag in the digest, with distinct colors +
    rotating markers so a batch of orphans is visually separable out of the box."""
    d = digest or load_digest()
    used = used_colors()
    props = []
    for i, tag in enumerate(sorted(set(d.get("orphans", [])))):
        col = free_color(used)
        used.add(col)                           # don't hand the same color out twice
        p = propose_tuple(tag, color=col, symbol=SYMBOLS[i % len(SYMBOLS)], used=used)
        props.append(p)
    return props


def load_digest():
    if not os.path.exists(DIGEST_JSON):
        sys.exit("error: no ingest_digest.json — run pipeline/ingest.py first")
    return json.load(open(DIGEST_JSON))


# ── append a tuple to figdata (the mechanical half of the taste step) ───────────
def insert_family(src, model, line):
    """Insert a group-tuple source line directly under the model's sentinel anchor
    in figdata.py. Anchor-based (not bracket-matching) so the edit is deterministic
    and order-stable: newest family lands right under the anchor, above the `]`."""
    anchor = f"# PLOTS-ORPHAN-ANCHOR {model}"
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if anchor in ln:
            lines.insert(i + 1, line)
            return "\n".join(lines) + ("\n" if src.endswith("\n") else "")
    raise ValueError(f"no '{anchor}' sentinel in figdata.py — "
                     "Phase 3 anchors missing or model name wrong")


def add_family(prop, write=False):
    """Dry-run (default) or, with write=True, append the proposed tuple to figdata.
    Returns the path written (None on dry-run)."""
    if not write:
        return None
    src = open(FIGDATA).read()
    open(FIGDATA, "w").write(insert_family(src, prop["model"], prop["line"]))
    return FIGDATA


# ── refresh known-family plots (regenerable — may run unattended) ────────────────
def render_steps(report=False):
    """The re-render commands, run from Reports/ so relative `diag/` resolves and
    `import figdata` works off the script dir. Known-family runs are already on
    the plot; this just regenerates the derivative artifacts."""
    py = sys.executable
    steps = [("owt honest Pareto PNG", [py, "pareto_honest_png.py"]),
             ("Llama honest Pareto PNG", [py, "pareto_png.py"]),
             ("interactive Plotly figures", [py, "interactive.py"])]
    if report:                                  # rebuild the published HTML (gated)
        steps.append(("technical-report HTML",
                      [py, os.path.join(ROOT, "smh", "pipeline", "build.py")]))
    return steps


def refresh(report=False):
    results = []
    for label, argv in render_steps(report):
        try:
            r = subprocess.run(argv, cwd=REPORTS, capture_output=True, text=True)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            results.append({"label": label, "ok": r.returncode == 0,
                            "tail": tail[-1] if tail else ""})
        except OSError as e:
            results.append({"label": label, "ok": False, "tail": str(e)})
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────────
def _print_proposal(p, write=False):
    print(f"# orphan `{p['tag']}` → {p['model']} family "
          f"(decoder {p['decoder']}, {'WROTE' if write else 'DRY-RUN'}):\n")
    print(p["line"])
    target = "_llama_groups()" if p["model"] == "llama" else "_honest_groups()"
    if not write:
        print(f"\n# append to figdata.{target} with:")
        print(f"  python pipeline/plots.py add-family {p['tag']} "
              f'--name "{p["name"]}" --color {p["color"]} '
              f'--symbol {p["symbol"]} --glob "{p["glob"]}" --write')


def main():
    ap = argparse.ArgumentParser(description="Phase-3: plot refresh + orphan tuples.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("orphans", help="propose a figdata tuple for each digest orphan")
    af = sub.add_parser("add-family", help="propose/append a figdata tuple for a tag")
    af.add_argument("tag")
    af.add_argument("--model", choices=["owt", "llama"])
    af.add_argument("--name"); af.add_argument("--color"); af.add_argument("--symbol")
    af.add_argument("--glob")
    af.add_argument("--kind", choices=["betas", "uni"],
                    help="owt only: 'uni' = eff-bits from the tag (static), else 'betas'")
    af.add_argument("--baseline", action="store_true",
                    help="mark as a not-learned baseline (drawn hollow/faded)")
    af.add_argument("--write", action="store_true", help="append to figdata.py")
    rf = sub.add_parser("refresh", help="re-render known-family plots")
    rf.add_argument("--report", action="store_true",
                    help="also rebuild the technical-report HTML (gated)")
    a = ap.parse_args()

    if a.cmd == "orphans":
        props = orphan_proposals()
        if not props:
            print("# no orphans in the current digest — nothing to assign.")
            return
        print(f"# {len(props)} orphan(s) need a figdata family tuple "
              "(color/marker/glob are taste; confirm + --write):\n")
        for p in props:
            _print_proposal(p)
            print()
    elif a.cmd == "add-family":
        p = propose_tuple(a.tag, model=a.model, name=a.name, color=a.color,
                          symbol=a.symbol, glob=a.glob, kind=a.kind,
                          baseline=a.baseline)
        path = add_family(p, write=a.write)
        _print_proposal(p, write=bool(path))
        if path:
            print(f"\n[appended to {path} — re-render with: "
                  "python pipeline/plots.py refresh]")
    elif a.cmd == "refresh":
        for r in refresh(report=a.report):
            print(f"  [{'ok ' if r['ok'] else 'FAIL'}] {r['label']}: {r['tail']}")


if __name__ == "__main__":
    main()
