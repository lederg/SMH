"""Render JOURNAL.md + EVIDENCE.md -> Reports/journal_graph.{json,html} (spec
`docs/journal_graph_spec.md`). The journal stays the single source of truth; this
is a generated downstream view, exactly like Reports/journal.html. Read-only on
the .md.

Two artifacts:
  - Reports/journal_graph.json — the canonical graph (nodes + edges + warnings).
  - Reports/journal_graph.html — a self-contained interactive view (precomputed
    force layout baked to SVG, vanilla-JS click-to-focus + filters, no network,
    no CDN — same offline discipline as render_journal.py).

Optional Reports/vault/ Obsidian export behind --vault (spec §7).

Usage:
  python journal_graph.py            # writes Reports/journal_graph.{json,html}
  python journal_graph.py --vault    # also writes Reports/vault/ (Obsidian)
  python journal_graph.py --check    # parse only; nonzero exit if warnings (CI)
"""

import json
import math
import os
import re
import sys

from smh import journal_xref as jx
from smh.pipeline import queue_overlay

from smh.config import resolve_root
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "Reports")
OUT_JSON = os.path.join(OUT_DIR, "journal_graph.json")
OUT_HTML = os.path.join(OUT_DIR, "journal_graph.html")
VAULT_DIR = os.path.join(OUT_DIR, "vault")

# Colors are CSS-var friendly; light/dark handled by the palette below.
TYPE_COLOR = {
    "hypothesis": "#d08770",   # warm
    "conclusion": "#5e81ac",   # blue
    "evidence": "#a3be8c",     # green
    "question": "#b48ead",     # purple
    "mechanism": "#a05c84",    # magenta — a diagnosed cause (graduated question)
    "experiment": "#ebcb8b",   # amber
    "decision": "#4fa1a1",     # teal — a chosen course (no result, no open question)
    "section": "#7f8c9b",      # neutral spine
}
EDGE_COLOR = {
    "supports": "#a3be8c",
    "contradicts": "#bf616a",
    "supersedes": "#d08770",
    "refines": "#88c0d0",      # cyan — lifecycle "narrows/extends" (Phase D)
    "tested_by": "#5e81ac",
    "produces": "#b48ead",
    "ties": "#8a9bb5",         # steel — a declared sibling relation
    "contrasts": "#d8a48f",    # dusty warm — a declared opposition (soft contra)
    "underpins": "#4fa1a1",    # teal — a decision is a premise of the claim it points to
    "deprioritizes": "#9aa3ad",  # grey — curatorial demotion (focus, not science)
    "ref": "#8888",
    "contains": "#4445",
    # T4b cross-paper relation overlay (rendered dashed via `.edge-cross`).
    "generalizes": "#ebcb8b",        # amber
    "reproduces": "#8fbcbb",         # teal
    "tests_survival_of": "#bf616a",  # red — a survival test (can refute)
    "conditions": "#b48ead",         # purple — a scoping/conditioning relation
    "echoes": "#a3be8c",             # green — re-tests/echoes a prior finding
    "bridges": "#d08770",            # warm — a deployability bridge
}

W, H, MARGIN = 1280, 860, 70

# T5a: per-project node ring colors for the co-located multi-project graph.
# Assigned by SORTED project order so any project set is deterministic (not
# hardcoded to ldp+fracbits) — a fresh instance with N projects gets N distinct
# rings. Ring (stroke) not fill, so type-color (fill) and status (override) survive.
PROJECT_PALETTE = ["#5e81ac", "#d08770", "#b48ead", "#a3be8c", "#ebcb8b", "#88c0d0"]


def _project_colors(projects):
    """{project: ring_color} keyed on the sorted project list (deterministic)."""
    return {p: PROJECT_PALETTE[i % len(PROJECT_PALETTE)]
            for i, p in enumerate(sorted(projects))}


def _nkey(n):
    """T4b graph-view identity: `project:id`. Two projects share short ids (both
    define H1), so keying the view on the bare id collides; nodeKey disambiguates
    while `id` stays the short display label."""
    return f"{n.get('project', 'ldp')}:{n['id']}"


def _ekeys(e):
    """(src_key, dst_key) in nodeKey space. Cross edges (T4b) already carry
    project-qualified endpoints; within-segment edges are prefixed with their own
    segment's project."""
    if e.get("cross"):
        return e["src"], e["dst"]
    p = e.get("project", "ldp")
    return f"{p}:{e['src']}", f"{p}:{e['dst']}"


def plot_map():
    """{RE-id: [filename, ...]} — tier-2 leaf attachment (spec §3.0) reusing the
    curated EVIDENCE_PLOTS mapping in render_journal (the one source of plots)."""
    try:
        from smh.render_journal import EVIDENCE_PLOTS
    except ImportError:
        return {}            # evidence gallery stripped (2026-06-23) — no leaf plots
    out = {}
    for key, fname, _cap in EVIDENCE_PLOTS:
        m = re.match(r"(RE-\d+[a-z]?)", key)
        if m:
            out.setdefault(m.group(1), [])
            if fname not in out[m.group(1)]:
                out[m.group(1)].append(fname)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Static force-directed layout (Fruchterman–Reingold), pure-python so the SVG
# renders with zero runtime JS deps (spec §4 "recommend the static-SVG route").
# ──────────────────────────────────────────────────────────────────────────

def layout(nodes, edges, iters=320):
    ids = [_nkey(n) for n in nodes]          # T4b: key on project:id, not bare id
    idx = {i: k for k, i in enumerate(ids)}
    n = len(ids)
    if n == 0:
        return {}
    # Deterministic golden-angle spiral init — no RNG, reproducible across runs.
    ga = math.pi * (3 - math.sqrt(5))
    pos = {}
    for k, i in enumerate(ids):
        r = 0.45 * math.sqrt((k + 0.5) / n)
        pos[i] = [0.5 + r * math.cos(k * ga), 0.5 + r * math.sin(k * ga)]

    links = [k for k in (_ekeys(e) for e in edges)
             if k[0] in idx and k[1] in idx]
    k = math.sqrt(1.0 / n)          # ideal edge length in unit square
    temp = 0.10
    for step in range(iters):
        disp = {i: [0.0, 0.0] for i in ids}
        # Repulsion (all pairs).
        for a in range(n):
            ia = ids[a]
            for b in range(a + 1, n):
                ib = ids[b]
                dx = pos[ia][0] - pos[ib][0]
                dy = pos[ia][1] - pos[ib][1]
                d2 = dx * dx + dy * dy + 1e-9
                d = math.sqrt(d2)
                f = (k * k) / d2
                ux, uy = dx / d, dy / d
                disp[ia][0] += ux * f
                disp[ia][1] += uy * f
                disp[ib][0] -= ux * f
                disp[ib][1] -= uy * f
        # Attraction (edges).
        for a, b in links:
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            d = math.sqrt(dx * dx + dy * dy) + 1e-9
            f = (d * d) / k
            ux, uy = dx / d, dy / d
            disp[a][0] -= ux * f
            disp[a][1] -= uy * f
            disp[b][0] += ux * f
            disp[b][1] += uy * f
        # Apply, capped by temperature, keep inside the unit box.
        for i in ids:
            dx, dy = disp[i]
            dl = math.sqrt(dx * dx + dy * dy) + 1e-9
            pos[i][0] += (dx / dl) * min(dl, temp)
            pos[i][1] += (dy / dl) * min(dl, temp)
            pos[i][0] = min(0.98, max(0.02, pos[i][0]))
            pos[i][1] = min(0.98, max(0.02, pos[i][1]))
        temp = max(0.005, temp * 0.985)

    # Scale unit coords to the viewport.
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = (W - 2 * MARGIN) / (x1 - x0 + 1e-9)
    sy = (H - 2 * MARGIN) / (y1 - y0 + 1e-9)
    return {i: (MARGIN + (pos[i][0] - x0) * sx,
                MARGIN + (pos[i][1] - y0) * sy) for i in ids}


def _node_radius(n, indeg):
    """Base size from in-degree (importance), bumped by stated confidence. The
    JS sim reads this baked `r` back off each circle — no separate data needed."""
    if n["type"] == "section":
        return 13.0
    conf = {"high": 3, "med-high": 2, "medium": 1, "low": 0}.get(
        n.get("confidence"), 0)
    return 5 + 2.2 * math.sqrt(indeg.get(_nkey(n), 0)) + conf


def _svg(nodes, edges, coords, indeg, proj_colors=None):
    """Initial paint: positions are a warm-start from the Python force layout, so
    the file renders correctly with JS off. With JS on, the in-browser sim takes
    over from these same coords (drag / settle / zoom-pan).

    `proj_colors` (T5a) is set only for a MULTI-project graph; when present each
    node gets a `proj-<project>` class driving its ring color + filter. Empty for a
    single-project graph, so that output is byte-for-byte unchanged."""
    parts = []
    # Edges first (under the nodes). Endpoints route on nodeKey (T4b); cross edges
    # get a `edge-cross` class so the view can style the relation overlay.
    for e in edges:
        s, d = _ekeys(e)
        if s not in coords or d not in coords:
            continue
        x1, y1 = coords[s]
        x2, y2 = coords[d]
        col = EDGE_COLOR.get(e["kind"], "#8888")
        cross = " edge-cross" if e.get("cross") else ""
        parts.append(
            f'<line class="edge edge-{e["kind"]}{cross}" data-src="{s}" data-dst="{d}" '
            f'x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{col}"/>')
    # Nodes + labels. Identity (data-id / coords) is the nodeKey; the visible label
    # stays the short id.
    for n in nodes:
        key = _nkey(n)
        x, y = coords[key]
        r = _node_radius(n, indeg)
        col = TYPE_COLOR.get(n["type"], "#888")
        cls = (f'node type-{n["type"]} status-{n.get("status", "active")} '
               f'focus-{n.get("focus", "secondary")}')
        if n.get("subtype") in ("infra", "methodology"):
            cls += f' subtype-{n["subtype"]}'
        if proj_colors and n.get("project", "ldp") in proj_colors:
            cls += f' proj-{n.get("project", "ldp")}'
        parts.append(
            f'<g class="{cls}" data-id="{key}">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{col}"/>'
            f'<text class="lbl" x="{x:.1f}" y="{y - r - 3:.1f}">{n["id"]}</text></g>')
    return (f'<svg id="graph" viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="xMidYMid meet">\n' + "\n".join(parts) +
            "\n</svg>")


CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  margin: 0; }
header { padding: .7rem 1rem; border-bottom: 1px solid #8884; }
header h1 { font-size: 1.1rem; margin: 0; }
header p { margin: .25rem 0 0; color: #888; font-size: .82rem; }
#wrap { display: flex; flex-wrap: wrap; }
#left { flex: 1 1 540px; min-width: 0; }
#controls { padding: .5rem 1rem; font-size: .82rem; border-bottom: 1px solid #8884; }
#controls label { margin-right: .8rem; white-space: nowrap; cursor: pointer; }
#controls .swatch { display: inline-block; width: .7em; height: .7em; border-radius: 50%;
  margin-right: .25em; vertical-align: middle; }
svg { width: 100%; height: 78vh; display: block; cursor: grab;
  touch-action: none; user-select: none; }
svg.grabbing { cursor: grabbing; }
.edge { stroke-width: 1; opacity: .5; }
.edge-contains { stroke-dasharray: 2 3; opacity: .35; }
.edge-cross { stroke-width: 1.6; opacity: .7; stroke-dasharray: 5 3; }
.node text.lbl { font-size: 9px; fill: currentColor; text-anchor: middle;
  pointer-events: none; opacity: .75; }
.node circle { stroke: #0004; stroke-width: .5; cursor: pointer; }
.node.type-section circle { opacity: .5; }
.node.subtype-infra circle, .node.subtype-methodology circle { opacity: .5; }
/* decision glyph: a dashed teal ring marks a chosen course apart from claims. */
.node.type-decision circle { stroke: #4fa1a1; stroke-width: 1.8; stroke-dasharray: 2 1.5; }
#toolbar { padding: .4rem 1rem; font-size: .82rem; border-bottom: 1px solid #8884;
  display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
#toolbar input[type=search] { padding: .2rem .5rem; border: 1px solid #8886;
  border-radius: .4rem; background: transparent; color: inherit; min-width: 11rem; }
#toolbar button { padding: .2rem .6rem; border: 1px solid #8886; border-radius: .4rem;
  background: transparent; color: inherit; cursor: pointer; font: inherit; }
#toolbar .legend { color: #888; font-size: .75rem; }
#toolbar .legend i { display: inline-block; width: 1.4em; height: 0; border-top-width: 2px;
  border-top-style: solid; vertical-align: middle; margin: 0 .2em 0 .5em; }
#tip { position: fixed; z-index: 9; display: none; max-width: 22rem; pointer-events: none;
  background: #2e3440f2; color: #e5e9f0; border: 1px solid #4c566a; border-radius: .4rem;
  padding: .35rem .55rem; font-size: .78rem; box-shadow: 0 2px 8px #0006; }
.node.status-retracted circle, .node.status-contradicted circle {
  stroke: #bf616a !important; stroke-width: 2 !important; stroke-dasharray: 2 2; }
/* focus (SPEC_journal_focus): curatorial dim for parked history. `active` floats
   forward (subtle accent), `secondary` is the unstyled baseline. Dim is a fade,
   NOT a status ring — focus is orthogonal to truth, so it never recolors. */
.node.focus-historical { opacity: .4; }
.node.focus-active text.lbl { opacity: 1; font-weight: 600; }
.hidden { display: none; }
.dim { opacity: .12; }
.node.sel circle { stroke: #ebcb8b; stroke-width: 3; }
#panel { flex: 1 1 320px; max-width: 420px; padding: 1rem; border-left: 1px solid #8884; }
#panel h2 { font-size: 1rem; margin: 0 0 .3rem; }
#panel .badge { display: inline-block; font-size: .7rem; padding: .1em .5em;
  border-radius: 1em; background: #8883; margin-bottom: .5rem; }
#panel .summary { font-size: .9rem; color: #ccc8; }
#panel h3 { font-size: .8rem; margin: 1rem 0 .3rem; color: #888;
  text-transform: uppercase; letter-spacing: .04em; }
#panel a { cursor: pointer; color: #5e81ac; text-decoration: none; }
#panel a:hover { text-decoration: underline; }
#panel .deep { display: inline-block; margin-top: .8rem; font-size: .85rem; }
#panel .plots { font-size: .8rem; color: #888; }
.muted { color: #888; }
ul.edgelist { margin: .2rem 0; padding-left: 1.1rem; }
ul.edgelist li { margin: .1rem 0; font-size: .85rem; }
#live { padding: .4rem 1rem .8rem; border-bottom: 1px solid #3b4252; }
#live h2 { font-size: .95rem; margin: .3rem 0; }
#live a { color: #5e81ac; text-decoration: none; }
#live a:hover { text-decoration: underline; }
.live-row { font-size: .85rem; margin: .25rem 0; }
.live-tags { color: #777; font-size: .75rem; margin-left: 1rem; }
.live-run, .live-q, .live-done, .live-x {
  display: inline-block; font-size: .72rem; padding: 0 .4rem;
  border-radius: .6rem; margin-left: .3rem; }
.live-run { background: #a3be8c33; color: #a3be8c; }
.live-q { background: #ebcb8b33; color: #ebcb8b; }
.live-done { background: #5e81ac33; color: #88a4c8; }
.live-x { background: #bf616a33; color: #bf616a; }
"""

JS = r"""
const G = window.__GRAPH__;
const byId = {}; G.nodes.forEach(n => byId[n.key] = n);  // T4b: keyed on project:id
const inEdges = {}, outEdges = {};
G.edges.forEach(e => {
  (outEdges[e.src] = outEdges[e.src] || []).push(e);
  (inEdges[e.dst] = inEdges[e.dst] || []).push(e);
});
const svg = document.getElementById('graph');
const groups = {};
svg.querySelectorAll('g.node').forEach(g => groups[g.dataset.id] = g);

function neighbors(id) {
  const s = new Set([id]);
  (outEdges[id] || []).forEach(e => s.add(e.dst));
  (inEdges[id]  || []).forEach(e => s.add(e.src));
  return s;
}

let selected = null;
function focus(id) {
  selected = id;
  const keep = neighbors(id);
  Object.entries(groups).forEach(([nid, g]) => {
    g.classList.toggle('dim', !keep.has(nid));
    g.classList.toggle('sel', nid === id);
  });
  svg.querySelectorAll('line.edge').forEach(l => {
    const on = l.dataset.src === id || l.dataset.dst === id;
    l.classList.toggle('dim', !on);
  });
  renderPanel(id);
}
function clearFocus() {
  selected = null;
  Object.values(groups).forEach(g => g.classList.remove('dim', 'sel'));
  svg.querySelectorAll('line.edge').forEach(l => l.classList.remove('dim'));
}

function edgeLine(e, self) {
  const other = e.src === self ? e.dst : e.src;
  const arrow = e.src === self ? '→' : '←';
  const lbl = (byId[other] && byId[other].id) || other;   // short label, route on key
  return `<li>${arrow} <a data-go="${other}">${lbl}</a>`
       + ` <span class="muted">${e.kind}</span></li>`;
}
function renderPanel(id) {
  const n = byId[id];
  const out = (outEdges[id] || []), inc = (inEdges[id] || []);
  let h = `<h2>${n.id}</h2><div class="badge">${n.type} · ${n.status}</div>`;
  h += `<div class="summary">${n.title || ''}</div>`;
  if (n.evidence_plots && n.evidence_plots.length)
    h += `<h3>backed by</h3><div class="plots">${n.evidence_plots.join(', ')}</div>`;
  if (out.length) h += `<h3>references →</h3><ul class="edgelist">`
    + out.map(e => edgeLine(e, id)).join('') + `</ul>`;
  if (inc.length) h += `<h3>← referenced by</h3><ul class="edgelist">`
    + inc.map(e => edgeLine(e, id)).join('') + `</ul>`;
  if (n.anchor)
    h += `<a class="deep" href="journal.html#${n.anchor}">open in journal ↗</a>`;
  const p = document.getElementById('panel');
  p.innerHTML = h;
  p.querySelectorAll('a[data-go]').forEach(a =>
    a.onclick = () => focus(a.dataset.go));
}

// ── Live force layout — a zero-dep port of the Python Fruchterman–Reingold in
// layout(). Warm-starts from the baked coords, then keeps relaxing in-browser so
// nodes can be dragged and the graph re-settles. No d3, no CDN. ───────────────
const tip = document.getElementById('tip');
const vb0 = svg.getAttribute('viewBox').split(' ').map(Number);
const W = vb0[2], H = vb0[3];
const ids = G.nodes.map(n => n.key);     // T4b: nodeKey identity
const links = G.edges.filter(e => byId[e.src] && byId[e.dst])
  .map(e => [e.src, e.dst]);
const lines = [...svg.querySelectorAll('line.edge')];
const pos = {}, pinned = {};
ids.forEach(id => {                          // warm-start from the baked layout
  const c = groups[id].querySelector('circle');
  pos[id] = [+c.getAttribute('cx'), +c.getAttribute('cy')];
});
const K = Math.sqrt(W * H / Math.max(1, ids.length)) * 0.55;
let alpha = 0.35;

function step() {
  const disp = {}; ids.forEach(i => disp[i] = [0, 0]);
  for (let a = 0; a < ids.length; a++) {            // repulsion (all pairs)
    for (let b = a + 1; b < ids.length; b++) {
      const ia = ids[a], ib = ids[b];
      let dx = pos[ia][0] - pos[ib][0], dy = pos[ia][1] - pos[ib][1];
      const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2), f = K * K / d2;
      const ux = dx / d, uy = dy / d;
      disp[ia][0] += ux * f; disp[ia][1] += uy * f;
      disp[ib][0] -= ux * f; disp[ib][1] -= uy * f;
    }
  }
  for (const [a, b] of links) {                      // attraction (edges)
    let dx = pos[a][0] - pos[b][0], dy = pos[a][1] - pos[b][1];
    const d = Math.hypot(dx, dy) + 0.01, f = d * d / K;
    const ux = dx / d, uy = dy / d;
    disp[a][0] -= ux * f; disp[a][1] -= uy * f;
    disp[b][0] += ux * f; disp[b][1] += uy * f;
  }
  const cap = 16 * alpha;
  ids.forEach(i => {
    if (pinned[i]) return;
    const dx = disp[i][0], dy = disp[i][1], dl = Math.hypot(dx, dy) + 1e-9;
    const mv = Math.min(dl, cap);
    pos[i][0] += dx / dl * mv; pos[i][1] += dy / dl * mv;
  });
  alpha *= 0.985;
}
function paint() {
  ids.forEach(i => {
    const g = groups[i], c = g.querySelector('circle'), t = g.querySelector('text');
    const [x, y] = pos[i];
    c.setAttribute('cx', x.toFixed(1)); c.setAttribute('cy', y.toFixed(1));
    t.setAttribute('x', x.toFixed(1));
    t.setAttribute('y', (y - (+c.getAttribute('r')) - 3).toFixed(1));
  });
  lines.forEach(l => {
    const s = pos[l.dataset.src], d = pos[l.dataset.dst];
    if (!s || !d) return;
    l.setAttribute('x1', s[0].toFixed(1)); l.setAttribute('y1', s[1].toFixed(1));
    l.setAttribute('x2', d[0].toFixed(1)); l.setAttribute('y2', d[1].toFixed(1));
  });
}
const reheat = a => { alpha = Math.max(alpha, a); };
(function loop() { if (alpha > 0.004) step(); paint(); requestAnimationFrame(loop); })();

// ── Zoom + pan via the viewBox ───────────────────────────────────────────────
let vb = [...vb0];
const setVB = () => svg.setAttribute('viewBox', vb.join(' '));
function toSvg(e) {
  const r = svg.getBoundingClientRect();
  return [vb[0] + (e.clientX - r.left) / r.width * vb[2],
          vb[1] + (e.clientY - r.top) / r.height * vb[3]];
}
svg.addEventListener('wheel', e => {
  e.preventDefault();
  const [mx, my] = toSvg(e), r = svg.getBoundingClientRect();
  const k = e.deltaY > 0 ? 1.1 : 0.9;
  vb[2] *= k; vb[3] *= k;
  vb[0] = mx - (e.clientX - r.left) / r.width * vb[2];
  vb[1] = my - (e.clientY - r.top) / r.height * vb[3];
  setVB();
}, { passive: false });

// ── Pointer interaction (mouse + touch). One pointer = drag a node (pinned
// while held) or pan the background; TWO pointers = pinch-zoom. `touch-action:
// none` on the SVG stops Safari/iOS from hijacking the gesture as page scroll. ─
const ptrs = new Map();           // active pointers: id -> {x, y}
let drag = null;                  // {id, moved} — node being dragged
let pan = null;                   // {sx, sy, moved, vb} — background pan
let pinch = null;                 // {d, m, vb} — two-finger zoom anchor
const _dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const _mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

svg.addEventListener('pointerdown', e => {
  svg.setPointerCapture(e.pointerId);
  ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (ptrs.size === 2) {                       // second finger -> start pinch
    if (drag) { pinned[drag.id] = false; drag = null; }
    pan = null;
    const [p, q] = [...ptrs.values()];
    pinch = { d: _dist(p, q) || 1, m: _mid(p, q), vb: [...vb] };
    return;
  }
  const g = e.target.closest && e.target.closest('g.node');
  if (g) { drag = { id: g.dataset.id, moved: false }; pinned[g.dataset.id] = true; }
  else { pan = { sx: e.clientX, sy: e.clientY, moved: false, vb: [...vb] }; }
  svg.classList.add('grabbing');
});
svg.addEventListener('pointermove', e => {
  if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pinch && ptrs.size >= 2) {
    const [p, q] = [...ptrs.values()];
    const k = pinch.d / (_dist(p, q) || 1), r = svg.getBoundingClientRect();
    const ax = pinch.vb[0] + (pinch.m.x - r.left) / r.width * pinch.vb[2];
    const ay = pinch.vb[1] + (pinch.m.y - r.top) / r.height * pinch.vb[3];
    vb[2] = pinch.vb[2] * k; vb[3] = pinch.vb[3] * k;
    vb[0] = ax - (pinch.m.x - r.left) / r.width * vb[2];
    vb[1] = ay - (pinch.m.y - r.top) / r.height * vb[3];
    setVB();
    return;
  }
  if (drag) { drag.moved = true; pos[drag.id] = toSvg(e); reheat(0.25); return; }
  if (pan) {
    if (Math.abs(e.clientX - pan.sx) + Math.abs(e.clientY - pan.sy) > 3)
      pan.moved = true;
    const r = svg.getBoundingClientRect();
    vb[0] = pan.vb[0] - (e.clientX - pan.sx) / r.width * vb[2];
    vb[1] = pan.vb[1] - (e.clientY - pan.sy) / r.height * vb[3];
    setVB();
    return;
  }
  const g = e.target.closest && e.target.closest('g.node');   // hover tooltip
  if (g && tip) {
    const n = byId[g.dataset.id];
    tip.innerHTML = `<b>${n.id}</b> · ${n.type} / ${n.status}`
      + (n.confidence ? ` · ${n.confidence}` : '')
      + `<br>${n.summary || n.title || ''}`;
    tip.style.display = 'block';
    tip.style.left = (e.clientX + 12) + 'px';
    tip.style.top = (e.clientY + 12) + 'px';
  } else if (tip) { tip.style.display = 'none'; }
});
function endPtr(e) {
  ptrs.delete(e.pointerId);
  svg.classList.remove('grabbing');
  if (pinch) { if (ptrs.size < 2) pinch = null; return; }
  if (drag) { pinned[drag.id] = false; if (!drag.moved) focus(drag.id); drag = null; }
  else if (pan) { if (!pan.moved) clearFocus(); pan = null; }
}
svg.addEventListener('pointerup', endPtr);
svg.addEventListener('pointercancel', endPtr);
svg.addEventListener('pointerleave', () => { if (tip) tip.style.display = 'none'; });

// ── Search + fit-to-view ─────────────────────────────────────────────────────
const search = document.getElementById('search');
if (search) search.oninput = () => {
  const q = search.value.trim().toLowerCase();
  if (!q) { clearFocus(); return; }
  ids.forEach(i => {
    const n = byId[i];
    const hit = i.toLowerCase().includes(q)
      || (n.title || '').toLowerCase().includes(q);
    groups[i].classList.toggle('dim', !hit);
  });
  lines.forEach(l => l.classList.add('dim'));
};
const fitBtn = document.getElementById('fit');
if (fitBtn) fitBtn.onclick = () => { vb = [...vb0]; setVB(); reheat(0.3); };

function applyFilters() {
  const types = new Set([...document.querySelectorAll('.f-type:checked')]
    .map(c => c.value));
  const stat = new Set([...document.querySelectorAll('.f-status:checked')]
    .map(c => c.value));
  const projBoxes = document.querySelectorAll('.f-project');   // T5a (multi-project only)
  const projs = new Set([...document.querySelectorAll('.f-project:checked')]
    .map(c => c.value));
  const focusBoxes = document.querySelectorAll('.f-focus');    // SPEC_journal_focus
  const foci = new Set([...document.querySelectorAll('.f-focus:checked')]
    .map(c => c.value));
  const vis = {};
  G.nodes.forEach(n => {
    const projOn = projBoxes.length === 0 || projs.has(n.project || 'ldp');
    // focus is curatorial, not a science filter — sections always pass, and a band
    // with no checkbox in the toolbar passes (so single-band graphs are unaffected).
    const fb = n.focus || 'secondary';
    const focusOn = n.type === 'section' || focusBoxes.length === 0 || foci.has(fb)
      || !document.querySelector('.f-focus[value="' + fb + '"]');
    const on = projOn && focusOn && types.has(n.type)
      && (n.type === 'section' || stat.has(n.status)
        || !document.querySelector('.f-status[value="' + n.status + '"]'));
    vis[n.key] = on;
    groups[n.key].classList.toggle('hidden', !on);
  });
  svg.querySelectorAll('line.edge').forEach(l =>
    l.classList.toggle('hidden', !(vis[l.dataset.src] && vis[l.dataset.dst])));
}
document.querySelectorAll('.f-type,.f-status,.f-project,.f-focus').forEach(c =>
  c.onchange = applyFilters);
"""


def _project_css(proj_colors):
    """Per-project node ring (T5a): a thin stroke in the project's legend color so
    co-located projects read apart at a glance. Status rings (retracted /
    contradicted) override via `!important`. Empty for a single-project graph."""
    return "".join(
        f'.node.proj-{p} circle {{ stroke: {c}; stroke-width: 1.6; }}\n'
        for p, c in (proj_colors or {}).items())


def _controls(nodes, proj_colors=None):
    types = ["hypothesis", "conclusion", "evidence", "question", "mechanism",
             "experiment", "decision", "section"]
    present = {n["type"] for n in nodes}
    statuses = sorted({n["status"] for n in nodes
                       if n["type"] != "section"})
    tparts = []
    for t in types:
        if t not in present:
            continue
        sw = TYPE_COLOR[t]
        tparts.append(
            f'<label><input type="checkbox" class="f-type" value="{t}" checked>'
            f'<span class="swatch" style="background:{sw}"></span>{t}</label>')
    sparts = [
        f'<label><input type="checkbox" class="f-status" value="{s}" checked>'
        f'{s}</label>' for s in statuses]
    # focus filter row (SPEC_journal_focus §Render #3) — curatorial band, shown in
    # canonical order active->secondary->historical (only bands actually present).
    foci_present = {n.get("focus", "secondary") for n in nodes
                    if n["type"] != "section"}
    fparts = [
        f'<label><input type="checkbox" class="f-focus" value="{fb}" checked>'
        f'{fb}</label>' for fb in ("active", "secondary", "historical")
        if fb in foci_present]
    fhtml = (' &nbsp;|&nbsp; <b>focus:</b> ' + " ".join(fparts)) if len(fparts) > 1 else ''
    # T5a: project filter row + legend (the swatch IS the per-project ring color).
    # Only for a multi-project graph — a single-project view stays as it was.
    pparts = [
        f'<label><input type="checkbox" class="f-project" value="{p}" checked>'
        f'<span class="swatch" style="background:{c}"></span>{p}</label>'
        for p, c in (proj_colors or {}).items()]
    phtml = (' &nbsp;|&nbsp; <b>project:</b> ' + " ".join(pparts)) if pparts else ''
    return ('<div id="controls"><b>type:</b> ' + " ".join(tparts) +
            ' &nbsp;|&nbsp; <b>status:</b> ' + " ".join(sparts) + fhtml +
            phtml + '</div>')


def _live_panel(overlay, node_ids):
    """Render-time scheduler overlay -> a 'live job status' HTML block. This is the
    ONLY home for queued/running/finished status (EVIDENCE.md §3 is backlog-only).
    Joins jobs->claims via pipeline.queue_overlay; claims that name no real node are
    folded into 'unattributed' so the panel can't invent phantom claims. Returns ""
    when scheduler/ is absent or no jobs exist — the panel renders nothing, never a
    crash. NEVER written into journal_graph.json (build_graph stays pure)."""
    if not overlay or not overlay.get("present"):
        return ""
    claims = {c: dict(b) for c, b in overlay["claims"].items()}
    unattr = {s: list(v) for s, v in overlay["unattributed"].items()}
    # A claim key that resolves to no graph node (e.g. an infra group tag) is not a
    # journal claim — fold it into unattributed rather than show a dead link.
    for c in list(claims):
        if c not in node_ids:
            for s, jobs in claims.pop(c).items():
                unattr[s] += jobs

    live = sum(len(claims[c][s]) for c in claims for s in ("queue", "running"))
    live += len(unattr["queue"]) + len(unattr["running"])
    if not claims and not any(unattr[s] for s in unattr):
        return ""

    def _row(label, anchor, b):
        q, r, d, x = (b["queue"], b["running"], b["done"], b["cancelled"])
        if not (q or r or d or x):
            return ""
        name = (f'<a href="journal.html#{anchor}">{label}</a>' if anchor
                else f"<span>{label}</span>")
        chips = []
        if r:
            chips.append(f'<span class="live-run">{len(r)} running</span>')
        if q:
            chips.append(f'<span class="live-q">{len(q)} queued</span>')
        if d:
            chips.append(f'<span class="live-done">{len(d)} done</span>')
        if x:
            chips.append(f'<span class="live-x">{len(x)} cancelled</span>')
        tags = ", ".join(
            j["tag"] for j in (r + q) if j.get("tag"))[:160]
        sub = f'<div class="live-tags">{tags}</div>' if tags else ""
        return f'<div class="live-row">{name} {" ".join(chips)}{sub}</div>'

    rows = [_row(c, c, claims[c]) for c in sorted(claims)]
    rows.append(_row("unattributed (legacy / no goal)", None, unattr))
    rows = [r for r in rows if r]
    c = overlay["counts"]
    return f"""<section id="live">
  <h2>Live job status <span class="muted">— {live} active ·
  generated from the scheduler, not the markdown</span></h2>
  <p class="muted">{c['queue']} queued · {c['running']} running ·
  {c['done']} done · {c['cancelled']} cancelled. Source:
  <code>scheduler/{{queue,running,done}}</code> via
  <code>pipeline/queue_overlay.py</code> — never duplicated into EVIDENCE.md §3
  (backlog only). Read-only.</p>
  {"".join(rows)}
</section>"""


def _toolbar(edges):
    """Search box, fit button, gesture hint, and an edge-kind legend (only the
    kinds actually present). Lives above the SVG in the left column."""
    present = [k for k in ("supports", "contradicts", "refines", "tested_by",
                           "supersedes", "produces", "ties", "contrasts",
                           "underpins", "deprioritizes", "generalizes", "reproduces",
                           "tests_survival_of", "conditions", "echoes", "bridges")
               if any(e["kind"] == k for e in edges)]
    leg = "".join(
        f'<span><i style="border-top-color:{EDGE_COLOR[k]}"></i>{k}</span>'
        for k in present)
    return (
        '<div id="toolbar">'
        '<input type="search" id="search" placeholder="search id / title…">'
        '<button id="fit">fit</button>'
        '<span class="muted">drag node · scroll = zoom · drag background = pan</span>'
        f'<span class="legend">{leg}</span>'
        '</div>')


def render_html(graph, overlay=None):
    nodes, edges = graph["nodes"], graph["edges"]
    indeg = {}
    for e in edges:
        if e["kind"] != "contains":
            dk = _ekeys(e)[1]
            indeg[dk] = indeg.get(dk, 0) + 1
    coords = layout(nodes, edges)
    # T5a: per-project rings + filter only when >1 project is co-located.
    projects = {n.get("project", "ldp") for n in nodes}
    proj_colors = _project_colors(projects) if len(projects) > 1 else {}
    svg = _svg(nodes, edges, coords, indeg, proj_colors)
    # Trim the JSON we embed for the panel (drop layout-only noise). `key` is the
    # nodeKey the JS routes on (T4b); `id` stays the short display label.
    slim = {
        "nodes": [dict({k: n.get(k) for k in
                        ("id", "type", "status", "title", "summary", "anchor",
                         "evidence_plots", "confidence", "subtype", "project")},
                       key=_nkey(n), focus=n.get("focus", "secondary"))
                  for n in nodes],
        "edges": [dict(zip(("src", "dst"), _ekeys(e)),
                       kind=e["kind"], cross=bool(e.get("cross")))
                  for e in edges],
    }
    nwarn = len(graph["warnings"])
    warn = (f' · <span style="color:#bf616a">{nwarn} parser warning(s)</span>'
            if nwarn else " · 0 warnings")
    live_html = _live_panel(overlay, {n["id"] for n in nodes})
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LDP Journal — graph</title>
<style>{CSS}{_project_css(proj_colors)}</style></head><body>
<header>
  <h1>LDP Research Journal — claim graph</h1>
  <p>{len(nodes)} nodes · {len(edges)} edges{warn}. Generated from
  <code>JOURNAL.md</code> + <code>EVIDENCE.md</code> by <code>journal_graph.py</code> — the
  Markdown is the source of truth. Drag nodes, scroll to zoom, drag the
  background to pan; click a node to focus, empty space to reset.</p>
</header>
{live_html}
<div id="wrap">
  <div id="left">
    {_controls(nodes, proj_colors)}
    {_toolbar(edges)}
    {svg}
  </div>
  <div id="panel"><p class="muted">Click a node to inspect its claim, status,
  backlinks, and a deep link into the prose journal.</p></div>
</div>
<div id="tip"></div>
<script>window.__GRAPH__ = {json.dumps(slim)};</script>
<script>{JS}</script>
</body></html>"""


def write_vault(graph):
    """Obsidian-compatible export: one .md per node with [[wikilinks]] (spec §7).
    Generated, disposable, gitignored."""
    os.makedirs(VAULT_DIR, exist_ok=True)
    out_by = {}
    in_by = {}
    for e in graph["edges"]:
        out_by.setdefault(e["src"], []).append((e["dst"], e["kind"]))
        in_by.setdefault(e["dst"], []).append((e["src"], e["kind"]))
    written = 0
    for n in graph["nodes"]:
        if n["type"] == "section":
            continue
        fn = os.path.join(VAULT_DIR, n["id"].replace("/", "_") + ".md")
        lines = [
            "---", f"id: {n['id']}", f"type: {n['type']}",
            f"status: {n['status']}", "---", "",
            f"# {n['id']} — {n.get('title', '')}", "",
            n.get("summary", ""), "",
        ]
        outs = out_by.get(n["id"], [])
        ins = in_by.get(n["id"], [])
        if outs:
            lines.append("## References")
            lines += [f"- [[{d}]] ({k})" for d, k in outs]
            lines.append("")
        if ins:
            lines.append("## Referenced by")
            lines += [f"- [[{s}]] ({k})" for s, k in ins]
        with open(fn, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        written += 1
    return written


def main(argv):
    if "--schema" in argv:                 # R6: print the grammar, generated from
        sys.stdout.write(jx.schema_text())  # the same tables the parser reads
        return 0

    if "--edge-audit" in argv:             # R1: triage prose-minted edges (read-only)
        rows = jx.edge_audit(ROOT, plot_map())
        by_proj = {}
        for r in rows:
            by_proj.setdefault(r["project"], []).append(r)
        for proj in sorted(by_proj):
            print(f"\n## {proj} — {len(by_proj[proj])} prose-minted edge(s) to triage")
            for r in by_proj[proj]:
                print(f"  L{r['line']}: {r['src']} --{r['kind']}--> {r['dst']}   "
                      f"[promote to links:/evidence: OR drop]")
        print(f"\nTOTAL prose-minted edges: {len(rows)} across "
              f"{len(by_proj)} project(s). "
              f"Declared edges are unaffected by the R1 flip.")
        return 0

    segs = jx.load_segments(ROOT)          # co-located ledger: ldp + fracbits (T3b₁)
    graph = jx.build_graph(segs, plot_map())

    if "--belief-ledger" in argv:          # R5: preview / diff the generated §0.2
        i = argv.index("--belief-ledger")
        project = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") \
            else None
        seg_md = dict(segs).get(project, "") if project else ""
        if "--diff" in argv:               # transition check vs the hand table
            drift = jx.belief_ledger_diff(seg_md, graph["nodes"], graph["edges"], project)
            for d in drift:
                print("DRIFT:", d)
            print(f"belief-ledger diff for {project or '(all)'}: "
                  f"{len(drift)} mismatch(es)")
            return 1 if drift else 0
        if "--write" in argv:              # rewrite the §0.2 region in-place (idempotent)
            if not project:
                print("--belief-ledger --write needs a project (e.g. `ldp`)")
                return 2
            path, changed = jx.belief_ledger_write_file(
                project, graph["nodes"], graph["edges"], ROOT)
            print(f"{'rewrote' if changed else 'no change'} §0.2 in {path}")
            return 0
        table = jx.belief_ledger_table(graph["nodes"], graph["edges"], project)
        print(table or f"(no claim in {project or '(all)'} has a **bottom-line:** "
              f"field yet — nothing to generate)")
        return 0

    if "--check" in argv:
        for w in graph["warnings"]:
            print("WARN:", w)
        for a in graph.get("advisories", []):       # non-gating: visibility only
            print("ADVISORY:", a)
        n = len(graph["warnings"])
        print(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges, "
              f"{n} warnings, {len(graph.get('advisories', []))} advisories")
        return 1 if n else 0

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    # The live overlay is a render-time join only — it touches the HTML, never the
    # canonical graph JSON above (build_graph stays a pure function of the markdown).
    overlay = queue_overlay.build_overlay()
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(graph, overlay))

    msg = (f"wrote {OUT_JSON} and {OUT_HTML}  "
           f"({len(graph['nodes'])} nodes, {len(graph['edges'])} edges, "
           f"{len(graph['warnings'])} warnings)")
    if "--vault" in argv:
        n = write_vault(graph)
        msg += f"; vault {VAULT_DIR} ({n} notes)"
    print(msg)
    for w in graph["warnings"]:
        print("WARN:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
