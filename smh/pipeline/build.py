"""Layer C — the build runner.

  story-prompt + journal facts ─► narrative (md) ─► FACT GATE ─► HTML (+ figures)

Pipeline (HANDOFF §2b / §4a Milestone 1):
  1. Load the story-spec prompt (presentations/<name>/v<N>.prompt.md) and the
     journal's structured facts. These are the LLM's inputs; written to a
     build-context file so the generation step is reproducible + inspectable.
  2. Obtain the narrative markdown. The generation step is `LLM(prompt + facts)`.
     The LLM seam: pass a freshly-generated narrative via --narrative; with no
     flag the runner uses the v1 baseline (Reports/report_v2_draft.md) that the
     prompt was distilled from — the degenerate "use exactly this prose" case,
     which is what Milestone 1 reproduces.
  3. FACT GATE (pipeline/gate.py) over the narrative — fail-closed on any
     unsourced number. A presentation that fails does not ship.
  4. Inject figures at section ends: interactive Plotly (inline, zoomable) for
     the Pareto / §3 / §5; base64 PNGs for β-dynamics.
  5. Emit a self-contained HTML file.

Usage:
  python pipeline/build.py                       # build technical-report v1
  python pipeline/build.py --narrative draft.md  # gate+render a generated narrative
  python pipeline/build.py --no-gate             # render even if the gate fails (debug)
"""
import argparse
import base64
import json
import os
import re
import sys

import markdown

from smh.config import resolve_root
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

from smh.pipeline.facts import structured_facts, journal_numbers  # noqa: E402
from smh.pipeline.gate import verify, report as gate_report  # noqa: E402
# NOTE: no `from ldp.figures import ...` here. Core resolves the instance's
# FigureCatalog through `_default_figure_catalog` / `_resolve_project` (smh.toml),
# so it imports no instance figure module by name — the Adapter-3 import-direction
# fix. LdpFigureCatalog is the lazy fallback inside those resolvers.

DIAG = os.path.join(ROOT, "Reports", "diag")
PRESENTATIONS = os.path.join(ROOT, "presentations")


def _default_figure_catalog(root=None):
    """The instance `FigureCatalog`, resolved exactly like
    `facts._default_catalog` resolves the FactsCatalog. A stranger's repo names
    its catalog in `smh.toml` (`figure_catalog = "module:Class"`); absent that key
    (the LDP repo, or a co-located repo that keeps LDP the default) fall back to
    `LdpFigureCatalog` via a lazy import — so core stays decoupled from any
    instance figure module and LDP output is byte-unchanged."""
    from smh import config
    cfg = config.load_config(root or ROOT)
    if cfg and cfg.get("figure_catalog"):
        return config.instantiate(cfg["figure_catalog"])
    from ldp.figures import LdpFigureCatalog
    return LdpFigureCatalog()


def _resolve_project(project, root=ROOT):
    """Resolve a co-located instance to `(facts_catalog, figure_catalog,
    presentations_dir, label)`.

    `project=None` = the *default* instance: the top-level smh.toml catalogs, else
    the LDP fallbacks + repo-root `presentations/`. Every existing call takes this
    branch and renders byte-identically. A *named* project reads its catalogs +
    presentations dir from smh.toml's `[projects.<name>]` table (a per-key omission
    falls back to the default), so a BitDistiller deck resolves against the
    BitDistiller catalogs without core knowing either by name."""
    from smh.pipeline.facts import _default_catalog
    if project is None:
        return _default_catalog(root), _default_figure_catalog(root), PRESENTATIONS, "LDP"
    from smh import config
    pc = config.project_config(root, project)
    if not pc:
        sys.exit(f"smh.toml has no [projects.{project}] table (project={project!r})")
    facts_cat = (config.instantiate(pc["facts_catalog"]) if pc.get("facts_catalog")
                 else _default_catalog(root))
    fig_cat = (config.instantiate(pc["figure_catalog"]) if pc.get("figure_catalog")
               else _default_figure_catalog(root))
    pres = (os.path.join(root, pc["presentations"]) if pc.get("presentations")
            else PRESENTATIONS)
    return facts_cat, fig_cat, pres, pc.get("label", project)

# β-dynamics figures stay static PNG (base64-embedded), per the story-prompt.
BETA_PNGS = [
    "01_owt128_pn_a2_bits_by_kind.png", "02_owt128_pn_budget_vs_alpha.png",
    "03_hinge_vs_pressure_budget.png", "04_depth_wi_hinge_vs_pressure.png",
    "05_owt_seed_neighbors_a2_reproducibility.png",
]


def _b64img(fname):
    p = os.path.join(DIAG, fname)
    if not os.path.exists(p):
        return f"<p><i>missing figure: {fname}</i></p>"
    data = base64.b64encode(open(p, "rb").read()).decode()
    return (f'<figure><img src="data:image/png;base64,{data}" alt="{fname}">'
            f'<figcaption>{fname}</figcaption></figure>')


def build_context(prompt_path, facts_catalog=None):
    """Assemble the LLM input (prompt + structured facts) and persist it. The
    named facts come from the active instance's `facts_catalog` (default = LDP's,
    so a zero-arg call is byte-unchanged)."""
    prompt = open(prompt_path).read()
    facts = structured_facts(catalog=facts_catalog)
    ctx = (prompt + "\n\n---\n\n## Injected journal facts (structured)\n\n```json\n"
           + json.dumps(facts, indent=1) + "\n```\n")
    out = os.path.join(os.path.dirname(prompt_path), "build_context.md")
    open(out, "w").write(ctx)
    return ctx, facts, out


# β-dynamics plot aliases — stable ids a narrative can reference by token.
BETA_ALIASES = {
    "beta_bits_by_kind": "01_owt128_pn_a2_bits_by_kind.png",
    "beta_budget_vs_alpha": "02_owt128_pn_budget_vs_alpha.png",
    "beta_hinge_vs_pressure": "03_hinge_vs_pressure_budget.png",
    "beta_depth": "04_depth_wi_hinge_vs_pressure.png",
    "beta_reproducibility": "05_owt_seed_neighbors_a2_reproducibility.png",
}


def _fig_html(fig_id, fig_divs):
    """Resolve a figure id -> HTML: interactive Plotly div, a single β PNG,
    or the full β gallery."""
    if fig_id in fig_divs:
        # interactive Plotly: add a fullscreen toggle (handler + styles are
        # injected once per page — see FS_JS / CSS).
        btn = ('<button class="fs-btn" type="button" onclick="ldpFs(this)" '
               'title="Toggle fullscreen">⛶ Fullscreen</button>')
        return f'<div class="fig">{btn}{fig_divs[fig_id]}</div>'
    if fig_id == "beta_gallery":
        # prefer the interactive β figures; fall back to static PNGs if absent
        ids = ["beta_bits_by_kind", "beta_continuous", "beta_budget_vs_alpha",
               "beta_hinge_vs_pressure", "beta_depth", "beta_reproducibility"]
        if all(i in fig_divs for i in ids):
            return "".join(_fig_html(i, fig_divs) for i in ids)
        return ('<div class="gallery">'
                + "\n".join(_b64img(f) for f in BETA_PNGS) + "</div>")
    fname = BETA_ALIASES.get(fig_id, fig_id)
    if fname.endswith(".png"):
        return f'<div class="gallery">{_b64img(fname)}</div>'
    return f"<p><i>unknown figure id: {fig_id}</i></p>"


def inject_figures(md, fig_divs, placement=None):
    """Replace explicit `{{fig:<id>}}` tokens anywhere in the narrative —
    structure is free, figures go where the prompt places them. Falls back to
    the technical-report section-end injection (the catalog's `placement` map,
    section-marker → figure-id) when a narrative carries no tokens (backward
    compat)."""
    import re
    tokens = set(re.findall(r"\{\{fig:([a-z0-9_.]+)\}\}", md))
    if tokens:
        for t in tokens:
            md = md.replace("{{fig:%s}}" % t, _fig_html(t, fig_divs))
        return md

    # --- legacy: section-end injection, ordered by the placement map ---
    for marker, fig_id in (placement or {}).items():
        if marker in md:
            md = md.replace(marker, _fig_html(fig_id, fig_divs) + "\n\n" + marker, 1)
    return md


CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  max-width: 880px; margin: 0 auto; padding: 1.2rem; }
h1,h2,h3 { line-height: 1.25; } h2 { border-bottom: 2px solid #8884;
  padding-bottom: .2rem; margin-top: 2.2rem; }
code { font-family: ui-monospace,Menlo,monospace; font-size: .9em; background:#8881;
  padding:.1em .3em; border-radius:3px; }
pre { background:#8881; padding:.8rem; border-radius:6px; overflow-x:auto; }
table { border-collapse: collapse; width: 100%; display:block; overflow-x:auto;
  font-size:.92em; margin:.8rem 0; }
th,td { border:1px solid #8886; padding:.35rem .5rem; text-align:left; } th{background:#8882;}
blockquote { border-left:3px solid #8888; margin:.8rem 0; padding:.2rem 1rem; background:#8881; }
.fig { margin: 1.5rem 0; position: relative; }
.fs-btn { position:absolute; top:8px; left:8px; z-index:6; font:inherit; font-size:.78em;
  padding:.2em .55em; border:1px solid #8886; border-radius:4px; background:#fffd;
  color:#333; cursor:pointer; opacity:.65; }
.fs-btn:hover { opacity:1; }
.fig:fullscreen { background:#fff; padding:0; margin:0; overflow:hidden; }
.fig:fullscreen .plotly-graph-div { width:100vw !important; height:100vh !important;
  height:100dvh !important; }  /* dvh: exclude mobile browser chrome so x-ticks aren't clipped */
.gallery figure { margin:0 0 2rem; } .gallery img { width:100%; border:1px solid #8884; border-radius:6px; }
.gallery figcaption { font-size:.85em; color:#888; }
.banner { background:#e8f0ff22; border:1px solid #88a; border-radius:6px; padding:.6rem .9rem;
  font-size:.9em; margin:.5rem 0; }
"""

# Fullscreen toggle for interactive figures. Requests fullscreen on the .fig box
# and resizes the Plotly plot to fill it (Plotly honors the DOM node's size, so
# the :fullscreen CSS sizing + a resize() on fullscreenchange is enough).
FS_JS = """<script>
function ldpFs(btn){
  var box = btn.parentElement;
  if (document.fullscreenElement === box) { document.exitFullscreen(); }
  else if (box.requestFullscreen) { box.requestFullscreen(); }
}
document.addEventListener('fullscreenchange', function(){
  function resizeAll(){
    document.querySelectorAll('.fig').forEach(function(box){
      var p = box.querySelector('.plotly-graph-div');
      if (p && window.Plotly) { Plotly.Plots.resize(p); }
    });
  }
  // once now, once after the fullscreen layout/viewport settles
  requestAnimationFrame(resizeAll);
  setTimeout(resizeAll, 120);
});
</script>"""


# ── reveal.js slide target ───────────────────────────────────────────────────
# A second render target on the SAME facts→gate→figures pipeline: the narrative
# is sliced into slides (split on a lone `---`), each rendered to a <section>.
# Steps 1–4 (context, gate, figure injection) are shape-agnostic and reused; only
# the final HTML wrapper differs from the scrolling report.
REVEAL_V = "5.1.0"
REVEAL_CDN = f"https://cdn.jsdelivr.net/npm/reveal.js@{REVEAL_V}"

# Slide-specific styling (layered over reveal's white theme): the loop diagram,
# the automate/judge table, and the prose-emphasis helpers the narrative uses.
SLIDES_CSS = """
.reveal { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.reveal h1 { font-size: 1.9em; } .reveal h2 { font-size: 1.25em; color:#1a2a4a; }
.reveal h3 { color:#3a4a6a; font-weight:500; }
.reveal section { text-align: left; }
.reveal section h1, .reveal section h2 { text-align: left; }
.reveal p { font-size:.8em; line-height:1.4; }
.reveal p.sub { font-size:.62em; color:#555; line-height:1.5; }
.reveal p.cap { font-size:.5em; color:#555; line-height:1.4; margin-top:.2em; }
.reveal p.cap b { color:#c0392b; } .reveal p.cap em { font-style:italic; color:#333; }
.reveal p.big { font-size:.88em; margin-top:.5em; padding:.35em .6em; border-left:4px solid #c44;
  background:#fbeaea; border-radius:4px; }
.reveal p.big em { color:#c0392b; font-style:normal; font-weight:600; }
.reveal ul, .reveal ol { font-size:.78em; } .reveal li { margin:.26em 0; line-height:1.34; }
/* scientific-loop diagram */
.reveal .loop { display:flex; flex-wrap:wrap; align-items:stretch; gap:.4em; justify-content:center;
  margin:.6em 0 .4em; }
.reveal .loop .node { flex:1 1 0; min-width:0; border-radius:8px; padding:.55em .4em; text-align:center;
  font-size:.62em; font-weight:600; line-height:1.2; display:flex; flex-direction:column; justify-content:center; }
.reveal .loop .node span { display:block; font-weight:400; font-size:.82em; opacity:.7; margin-top:.25em;
  text-transform:uppercase; letter-spacing:.04em; }
.reveal .loop .node.auto  { background:#e8f0fe; color:#1a4480; border:1px solid #aac4f0; }
.reveal .loop .node.human { background:#fdeede; color:#8a4b10; border:1px solid #e8c48a; }
.reveal .loop .arr { align-self:center; color:#888; font-size:.9em; flex:0 0 auto; }
/* automate / judge table */
.reveal table.divide { width:100%; border-collapse:collapse; font-size:.6em; }
.reveal table.divide th { background:#1a2a4a; color:#fff; padding:.5em .6em; text-align:left; width:50%; }
.reveal table.divide th:last-child { background:#7a3a10; }
.reveal table.divide td { border:1px solid #ccc; padding:.45em .6em; vertical-align:top; }
.reveal table.divide tr td:first-child { background:#f2f6fd; }
.reveal table.divide tr td:last-child  { background:#fdf4ea; }
.reveal table.divide b { color:#a02; }
/* interactive figures inside a slide */
.reveal .fig { margin:.3em 0; }
/* cap slide figures so a tall plot + caption can't overflow the 820px stage;
   resize() honors it */
.reveal .fig .plotly-graph-div { max-width:100%; margin:0 auto; height:340px !important; }
.reveal .fs-btn, .reveal .banner { display:none; }
/* static PNG galleries on a slide: cap height to the 820px stage so the image +
   caption can't overflow the bottom; hide the auto figcaption (filename) — the
   narrative supplies its own <p class="cap">. */
.reveal .gallery { margin:.3em 0; }
.reveal .gallery figure { margin:0; }
.reveal .gallery img { width:auto; max-width:100%; max-height:500px;
  display:block; margin:0 auto; }
.reveal .gallery figcaption { display:none; }
/* journal row anatomy box */
.reveal .anat { font-size:.5em; line-height:1.5; background:#f6f8fa; border:1px solid #d4dae2;
  border-left:4px solid #1a4480; border-radius:5px; padding:.55em .8em; margin:.6em 0; }
.reveal .anat code { font-size:.92em; background:#e6ebf2; padding:.05em .3em; border-radius:3px; }
.reveal .anat b { color:#1a4480; }
"""

# Plotly plots are display:none until their slide is shown, so they render at
# zero size — resize them whenever a slide becomes current (and once on ready).
SLIDES_JS = f"""
<script src="{REVEAL_CDN}/dist/reveal.js"></script>
<script src="{REVEAL_CDN}/plugin/notes/notes.js"></script>
<script>
function ldpResizePlots(){{
  if(!window.Plotly) return;
  document.querySelectorAll('.reveal .slides section.present .plotly-graph-div')
    .forEach(function(p){{ Plotly.Plots.resize(p); }});
}}
Reveal.initialize({{ hash:true, slideNumber:'c/t', width:1100, height:820,
  margin:0.04, minScale:0.2, maxScale:1.5, controls:true, progress:true,
  plugins:[RevealNotes] }});
Reveal.on('ready', ldpResizePlots);
Reveal.on('slidechanged', function(){{ requestAnimationFrame(ldpResizePlots);
  setTimeout(ldpResizePlots, 120); }});
</script>"""


# --- in-place editor (edit variant only) --------------------------------------
# The `-edit.html` variant wraps each editable markdown BLOCK in a
# <div class="md-block" data-md-lo data-md-hi data-md-src> so the browser editor
# can round-trip an edit back to the exact source line range in narrative.md.
# These anchors + the editor CSS/JS live ONLY in the edit variant — the normal
# slides output (editable=False) stays byte-identical (source of truth ⟂ HTML).

_FENCE_RE = re.compile(r"^\s*```")
_HTML_OPEN_RE = re.compile(r"^\s*<([a-zA-Z][\w-]*)")
_FIG_TOKEN_RE = re.compile(r"^\{\{fig:[^}]+\}\}$")


def _split_slides_with_lines(md):
    """Split the narrative into slides, tracking each slide's source line span.
    Yields (start_lineno, lines) — start_lineno is the 1-based file line of the
    slide's first line. Separator is a line that is exactly `---` (matches the
    normal path's `md.split("\\n---\\n")` for well-formed narratives)."""
    slides, cur, cur_start = [], [], 1
    for idx, ln in enumerate(md.split("\n"), start=1):
        if ln == "---":
            slides.append((cur_start, cur))
            cur, cur_start = [], idx + 1
        else:
            cur.append(ln)
    slides.append((cur_start, cur))
    return slides


def _split_blocks(lines, base_lineno):
    """Split one slide's lines into markdown blocks, each with its 1-based
    inclusive file line range. Blocks are blank-line separated, EXCEPT fenced
    code and multi-line raw-HTML blocks (`<aside>…</aside>`, `<div…>…</div>`,
    `<table…>…</table>`) are kept intact even across internal blank lines.
    Returns list of (lo, hi, src)."""
    blocks, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].strip() == "":
            i += 1
            continue
        start = i
        if _FENCE_RE.match(lines[i]):
            i += 1
            while i < n and not _FENCE_RE.match(lines[i]):
                i += 1
            if i < n:                       # include the closing fence
                i += 1
        else:
            m = _HTML_OPEN_RE.match(lines[i])
            close = f"</{m.group(1)}>" if m else None
            if close and close not in lines[i]:
                i += 1                      # multi-line HTML: consume to close tag
                while i < n and close not in lines[i - 1]:
                    i += 1
            else:
                i += 1                      # ordinary block: consume to blank line
                while i < n and lines[i].strip() != "":
                    i += 1
        blocks.append((base_lineno + start, base_lineno + i - 1,
                       "\n".join(lines[start:i])))
    return blocks


def _render_slides_editable_sections(md, fig_divs, placement):
    """Render slides for the edit variant: one <section> per slide, each block
    wrapped with its source-line anchors. Figure-token blocks are LOCKED (the
    token is literal in narrative.md but not free-text); all other blocks are
    editable as their raw markdown source (base64 in data-md-src)."""
    exts = ["tables", "fenced_code", "sane_lists"]
    sections = []
    for start_line, slide_lines in _split_slides_with_lines(md):
        parts = []
        for lo, hi, src in _split_blocks(slide_lines, start_line):
            if _FIG_TOKEN_RE.match(src.strip()):
                fig_html = inject_figures(src, fig_divs, placement)
                parts.append(
                    f'<div class="md-block locked" data-locked '
                    f'data-md-lo="{lo}" data-md-hi="{hi}">{fig_html}</div>')
            else:
                html = markdown.markdown(src, extensions=exts)
                b64 = base64.b64encode(src.encode()).decode()
                parts.append(
                    f'<div class="md-block" data-md-lo="{lo}" data-md-hi="{hi}" '
                    f'data-md-src="{b64}">{html}</div>')
        sections.append("<section>\n" + "\n".join(parts) + "\n</section>")
    return sections


SLIDES_EDIT_CSS = """
/* in-place editor affordances (edit variant only) */
.reveal .md-block { position:relative; border-radius:4px; transition:background .1s; }
.reveal .md-block:not(.locked):hover { background:#fff7d6; cursor:text;
  outline:1px dashed #d0a000; outline-offset:2px; }
.reveal .md-block.editing { background:#fffef5; outline:2px solid #1a4480; }
.reveal .md-block.locked { opacity:1; }
.reveal .md-edit-box { width:100%; box-sizing:border-box; }
.reveal .md-edit-box textarea { width:100%; min-height:6em; font:13px/1.5 ui-monospace,
  monospace; padding:8px; box-sizing:border-box; border:1px solid #1a4480; border-radius:4px; }
.reveal .md-edit-bar { display:flex; gap:8px; align-items:center; margin-top:5px; }
.reveal .md-edit-bar button { font:12px/1 sans-serif; padding:5px 14px; cursor:pointer; }
.reveal .md-edit-msg { font:11px/1.3 ui-monospace,monospace; white-space:pre-wrap;
  color:#c00; max-height:9em; overflow:auto; flex:1; }
#md-edit-banner { position:fixed; top:0; left:0; right:0; z-index:60; background:#1a4480;
  color:#fff; font:12px/1 sans-serif; padding:6px 12px; text-align:center; }
#md-edit-banner b { color:#ffd24a; }
"""

# __DECK_NAME__ is replaced with a JSON string literal at render time.
SLIDES_EDIT_JS = """
<div id="md-edit-banner">EDIT MODE — click any text block to edit its markdown ·
edits round-trip into <b>narrative.md</b> through the fact gate (fail-closed)</div>
<script>
const DECK_NAME = __DECK_NAME__;
function b64decode(b){ return decodeURIComponent(Array.prototype.map.call(
  atob(b), c => '%' + ('00'+c.charCodeAt(0).toString(16)).slice(-2)).join('')); }
let mdEditing = null;
function mdCloseEditor(reload){
  if(reload){ location.reload(); return; }
  if(!mdEditing) return;
  const b = mdEditing; mdEditing = null;
  b.classList.remove('editing');
  b.innerHTML = b.dataset._html; delete b.dataset._html;
  if(window.Reveal) Reveal.configure({keyboard:true});
}
function mdOpenEditor(b){
  if(mdEditing || b.classList.contains('locked') || !b.dataset.mdSrc) return;
  mdEditing = b;
  if(window.Reveal) Reveal.configure({keyboard:false});
  b.dataset._html = b.innerHTML;
  b.classList.add('editing');
  const src = b64decode(b.dataset.mdSrc);
  const box = document.createElement('div'); box.className = 'md-edit-box';
  const ta = document.createElement('textarea'); ta.value = src;
  ta.rows = Math.max(3, src.split('\\n').length + 1);
  const bar = document.createElement('div'); bar.className = 'md-edit-bar';
  const save = document.createElement('button'); save.textContent = 'Save';
  const cancel = document.createElement('button'); cancel.textContent = 'Cancel';
  const msg = document.createElement('span'); msg.className = 'md-edit-msg';
  bar.append(save, cancel, msg);
  box.append(ta, bar);
  b.innerHTML = ''; b.append(box); ta.focus();
  ['keydown','keypress','keyup'].forEach(ev =>
    ta.addEventListener(ev, e => e.stopPropagation()));
  cancel.onclick = e => { e.stopPropagation(); mdCloseEditor(false); };
  save.onclick = async e => {
    e.stopPropagation();
    save.disabled = cancel.disabled = true; msg.style.color = '#888';
    msg.textContent = 'building + gating…';
    try {
      const r = await fetch('/save', { method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ name: DECK_NAME, lo:+b.dataset.mdLo,
          hi:+b.dataset.mdHi, markdown: ta.value }) });
      const d = await r.json();
      if(d.ok){ mdCloseEditor(true); }
      else { msg.style.color='#c00';
        msg.textContent = 'gate/build FAILED — narrative.md unchanged:\\n' + d.log;
        save.disabled = cancel.disabled = false; }
    } catch(err){ msg.style.color='#c00'; msg.textContent = 'request error: '+err;
      save.disabled = cancel.disabled = false; }
  };
}
document.addEventListener('click', e => {
  const b = e.target.closest('.md-block');
  if(b && !mdEditing) mdOpenEditor(b);
});
</script>"""


def _render_slides(name, version, md, fig_divs, plotlyjs, placement=None, label="LDP",
                   editable=False):
    """Slice the narrative into reveal.js <section>s, figures injected per slide.
    editable=True emits the in-place edit variant (block anchors + editor JS);
    editable=False is byte-identical to the shipped deck."""
    if editable:
        sections = _render_slides_editable_sections(md, fig_divs, placement)
        extra_css = SLIDES_EDIT_CSS
        extra_js = SLIDES_EDIT_JS.replace("__DECK_NAME__", json.dumps(name))
    else:
        sections = []
        for chunk in md.split("\n---\n"):
            chunk = inject_figures(chunk, fig_divs, placement)
            html = markdown.markdown(chunk, extensions=["tables", "fenced_code", "sane_lists"])
            sections.append(f"<section>\n{html}\n</section>")
        extra_css = extra_js = ""
    slides = "\n".join(sections)
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{label} — {name} v{version}</title>"
        f'<link rel=stylesheet href="{REVEAL_CDN}/dist/reveal.css">'
        f'<link rel=stylesheet href="{REVEAL_CDN}/dist/theme/white.css">'
        f"<style>{SLIDES_CSS}{extra_css}</style>{plotlyjs}</head>"
        f'<body><div class="reveal"><div class="slides">{slides}</div></div>'
        f"{SLIDES_JS}{extra_js}</body></html>")


def _render_report(name, version, md, fig_divs, plotlyjs, res, placement=None, label="LDP"):
    """The scrolling self-contained HTML report (original target)."""
    md = inject_figures(md, fig_divs, placement)
    body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
    n_unsourced = len(res["unsourced"])
    banner = (f'<div class="banner"><b>{name} v{version}</b> · generated via the '
              f'journal→facts→gate pipeline · fact gate: <b>{res["matched"]} numbers '
              f'sourced, {n_unsourced} unsourced</b> · interactive plots (zoom/pan/'
              f'box-select) inline.</div>')
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{label} — {name} v{version}</title><style>{CSS}</style>"
            f"{plotlyjs}</head><body>{banner}{body}{FS_JS}</body></html>")


def build(name="technical-report", version=1, narrative=None, gate=True, target="report",
          catalog=None, cross_project=False, project=None, editable=False):
    # --- step 0: resolve the active instance's catalogs + presentations dir.
    # project=None -> the LDP default (byte-unchanged); a named project resolves
    # via smh.toml [projects.<name>]. `catalog` still overrides the figure seam.
    facts_cat, fig_cat, presentations_dir, label = _resolve_project(project)
    if catalog is not None:
        fig_cat = catalog
    prompt_path = os.path.join(presentations_dir, name, f"v{version}.prompt.md")
    if not os.path.exists(prompt_path):
        sys.exit(f"no story-prompt at {prompt_path}")
    _, facts, ctx_path = build_context(prompt_path, facts_cat)

    # --- step 2: obtain narrative (LLM seam) ---
    if narrative:
        narrative_path = narrative
    else:
        # default: the presentation's own narrative.md, else the v1 baseline draft
        cand = os.path.join(presentations_dir, name, "narrative.md")
        narrative_path = cand if os.path.exists(cand) else \
            os.path.join(ROOT, "Reports", "report_v2_draft.md")
    md = open(narrative_path).read()

    # --- step 3: FACT GATE (fail-closed) ---
    # A cross-project deck (e.g. meta-workflow, which spans the LDP + FracBits +
    # BitDistiller ledgers) gates against the UNION of every project segment's
    # numbers. A named single project gates against ITS resolved ledger's numbers
    # (a BitDistiller deck fails closed on a number BitDistiller doesn't assert).
    # The default (LDP, project=None) keeps the LDP-scoped allow-list, byte-
    # unchanged — the narrow case is the default so existing decks are untouched.
    if cross_project:
        from smh.pipeline.facts import all_project_numbers
        res = verify(md, jset=all_project_numbers())
    elif project is not None:
        res = verify(md, jset=journal_numbers(text=facts_cat.journal_text()))
    else:
        res = verify(md)
    print(gate_report(res))
    if not res["ok"] and gate:
        sys.exit("\nFACT GATE FAILED — presentation does not ship. "
                 "Fix the narrative or migrate the missing fact into the journal "
                 "(consolidation pass), then rebuild. Use --no-gate to override.")

    # --- step 3b: REPRO GUARD (fail-closed) — don't ship while the journal is
    # known (or untested) to drift from its evidence. LDP-only: the guard is driven
    # by `pipeline/authored_facts.json`, the LDP coverage store. A non-default
    # instance has no such store, so skip it (with a note) rather than block —
    # wiring per-project authored_facts is separate owed work.
    if project is None:
        from smh.pipeline import repro_check
        repro_green, repro_reason = repro_check.build_status()
        print(f"REPRO GUARD: {repro_reason}")
        if not repro_green and gate:
            sys.exit("\nREPRO GUARD FAILED — presentation does not ship. Run "
                     "`python pipeline/repro_check.py`, resolve the drift, then rebuild. "
                     "Use --no-gate to override.")
    else:
        print(f"REPRO GUARD: skipped (project={project!r} has no authored_facts store)")

    # --- step 4: figures (interactive inline + β PNGs) — via the FigureCatalog
    # seam; core no longer imports the instance figure module by name.
    plotlyjs, fig_divs = fig_cat.figures_html()
    placement = fig_cat.placement()

    # --- step 5: emit (render target = report | slides) ---
    if target == "slides":
        html = _render_slides(name, version, md, fig_divs, plotlyjs, placement, label)
    else:
        html = _render_report(name, version, md, fig_divs, plotlyjs, res, placement, label)

    out = os.path.join(ROOT, "Reports", f"{name}.html")
    open(out, "w").write(html)
    # also drop into runs/ — the dir scripts/reports-serve.sh publishes over the
    # tailnet (http://ldp-reports.<tailnet>.ts.net/<name>.html)
    served = os.path.join(ROOT, "runs", f"{name}.html")
    os.makedirs(os.path.dirname(served), exist_ok=True)
    open(served, "w").write(html)
    print(f"\nwrote {out}  ({len(html)//1024} KB, target={target})")
    print(f"served copy: {served}  (reports-serve.sh up -> .../{name}.html)")
    # in-place edit variant (opt-in): same gated facts, block-anchored so the
    # browser editor can round-trip an edit into narrative.md. Written ONLY to
    # Reports/ (NOT runs/) — it's a writable-editor surface, not for the
    # read-only tailscale publish path. The normal outputs above are unchanged.
    if editable and target == "slides":
        edit_html = _render_slides(name, version, md, fig_divs, plotlyjs, placement,
                                   label, editable=True)
        edit_out = os.path.join(ROOT, "Reports", f"{name}-edit.html")
        open(edit_out, "w").write(edit_html)
        print(f"wrote {edit_out}  ({len(edit_html)//1024} KB, edit variant)")
    print(f"context: {ctx_path}  ({sum(v is not None for v in facts.values())}"
          f"/{len(facts)} facts resolved)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="technical-report")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--narrative", help="path to a generated narrative md (LLM output)")
    ap.add_argument("--no-gate", action="store_true", help="render even if gate fails")
    ap.add_argument("--target", default="report", choices=["report", "slides"],
                    help="render target: scrolling report (default) or reveal.js slides")
    ap.add_argument("--cross-project", action="store_true",
                    help="gate against the UNION of all co-located project ledgers "
                         "(meta-workflow deck spans LDP + FracBits + BitDistiller)")
    ap.add_argument("--project", default=None,
                    help="render a co-located non-default instance named in smh.toml's "
                         "[projects.<name>] table (e.g. bitdistiller); default = LDP")
    ap.add_argument("--editable", action="store_true",
                    help="also emit Reports/<name>-edit.html — the in-place browser "
                         "edit variant (slides target only); normal outputs unchanged")
    a = ap.parse_args()
    # An explicit --project always wins; else the current-project default. build()'s
    # default sentinel is None (the byte-identical LDP branch), so map the resolved
    # 'ldp' back to None — a session default of 'ldp' must render byte-identically.
    from smh.config import current_project, DEFAULT_PROJECT
    proj = a.project if a.project is not None else current_project(ROOT)
    if proj == DEFAULT_PROJECT:
        proj = None
    build(a.name, a.version, a.narrative, gate=not a.no_gate, target=a.target,
          cross_project=a.cross_project, project=proj, editable=a.editable)
