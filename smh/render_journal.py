"""Render JOURNAL.md + EVIDENCE.md -> Reports/journal.html (self-contained, phone-ok).

The markdown is the source of truth (and the live experiment queue). This makes
a read-only HTML *view* — never edits the .md, never adds anything not in the
markdown. Plots live in the reports (pareto-brief / technical-report), not here.
Servable over tailscale like the report (separate file from Reports/report.html).

Usage: python render_journal.py
"""

import os
import re

import markdown

# The journal parser (anchors + cross-reference index) lives in journal_xref so
# render_journal and journal_graph share ONE extraction — see journal_xref.py.
from smh.journal_xref import (
    claim_tokens,
    linkify,
    xref_index,
    load_journal,
    segment_text,
    build_graph,
    focus_banner,
    _inject_anchors,
)

from smh.config import resolve_root
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "Reports", "journal.html")


CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  max-width: 900px; margin: 0 auto; padding: 1.2rem; }
h1,h2,h3 { line-height: 1.25; }
h2 { border-bottom: 1px solid #8884; padding-bottom: .2rem; margin-top: 2rem; }
code { font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: .9em;
  background: #8881; padding: .1em .3em; border-radius: 3px; }
pre { background: #8881; padding: .8rem; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto;
  font-size: .92em; margin: .8rem 0; }
th,td { border: 1px solid #8886; padding: .35rem .5rem; text-align: left; }
th { background: #8882; }
blockquote { border-left: 3px solid #8888; margin: .8rem 0; padding: .2rem 1rem;
  background: #8881; }
a.xref { text-decoration: none; border-bottom: 1px dotted #8888; }
a.xref:hover { text-decoration: underline; }
.xrefindex li { margin: .15rem 0; }
/* focus axis (SPEC_journal_focus): the auto-derived active-front banner replaces
   the hand-written PRIMARY SUBJECT blockquote; historical claims dim (parked, not
   wrong — kept in full). Curatorial only; never recolors by truth. */
.focus-banner { border-left: 3px solid #4fa1a1; background: #4fa1a112;
  margin: .8rem 0 1.4rem; padding: .5rem .9rem; border-radius: 4px; font-size: .92em; }
.focus-banner .focus-active-ids { font-weight: 600; }
.focus-banner .focus-regime { font-family: ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size: .85em; color: #888; margin-top: .25rem; }
.focus-banner .focus-note { color: #888; font-size: .8em; margin-top: .3rem; }
h1.focus-historical, h2.focus-historical, h3.focus-historical,
h4.focus-historical, h5.focus-historical, h6.focus-historical { opacity: .5; }

/* bottom-line-first: an auto-generated claim ledger at the top (the LDP §0.2
   screen, generated so it can't drift) + per-claim progressive disclosure so the
   verdict/latest-result read first and the design/why/history fold away. */
.ledger-wrap { margin: .4rem 0 2rem; }
.ledger-wrap h2 { margin-top: 1rem; }
table.ledger { font-size: .9em; }
table.ledger td:first-child { white-space: nowrap; font-family: ui-monospace,
  SFMono-Regular,Menlo,monospace; font-weight: 600; }
table.ledger tr.hist { opacity: .55; }
.badge { display: inline-block; padding: .05em .5em; border-radius: 999px;
  font-size: .82em; font-weight: 600; white-space: nowrap; }
.s-supported,.s-settled { background: #2e7d3222; color: #2e7d32; }
.s-contradicted,.s-refuted,.s-retracted { background: #c6282822; color: #c62828; }
.s-testing { background: #ef6c0022; color: #ef6c00; }
.s-open { background: #8883; color: #888; }
.s-accepted { background: #1565c022; color: #1565c0; }
.s-superseded { background: #8882; color: #999; text-decoration: line-through; }
details.claim-more { margin: .3rem 0 .2rem; }
details.claim-more > summary { cursor: pointer; color: #888; font-size: .88em;
  list-style: none; padding: .1rem 0; }
details.claim-more > summary::-webkit-details-marker { display: none; }
details.claim-more > summary::before { content: "▸ "; }
details.claim-more[open] > summary::before { content: "▾ "; }
details.claim-more > summary:hover { color: #4fa1a1; }
"""


# --- bottom-line-first presentation (view only; never touches the markdown) ----
#
# Two transforms, both derived from the SAME markdown the graph reads:
#   1. an auto claim-ledger table (id · status · one-line bottom line) at the top,
#   2. per-claim <details> that fold the pre-registration/history so the current
#      verdict reads first.
# The .md stays the single source of truth; this only reorders what the eye hits.

_CLAIM_HEAD = re.compile(r"^#{1,6}\s+(C\d+[a-z]?|H[1-9][0-9]?|D\d+)\b(.*)")
_CLAIM_Q = re.compile(r"^[-*]\s*\*\*(Q\d+)\b(.*)")
_STATUSES = {"open", "testing", "supported", "contradicted", "settled",
             "retracted", "accepted", "superseded", "refuted", "answered"}


def _plain(s):
    """Strip inline markdown/HTML noise to a flat one-liner."""
    s = re.sub(r"<[^>]+>", "", s)                       # any stray tags
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)      # md links -> text
    return re.sub(r"\s+", " ", s).strip()


def _clip(s, n=200):
    s = _plain(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _result_headline(line):
    """Bottom line carried by a `- **result (RE-NN) — HEADLINE:** …` bullet: the
    emphasized label minus the `result (RE-..)` prefix, falling back to the body."""
    m = re.search(r"\*\*\s*result\b(.*?)\*\*", line, re.S)
    head = _plain(m.group(1)) if m else ""
    head = re.sub(r"^\s*\([^)]*\)", "", head)           # drop the (RE-NN) id
    head = head.lstrip(" ,:—–-").strip()
    if len(head) < 12:                                  # bare "result (RE-13)"
        tail = line.split("**", 2)[-1] if line.count("**") >= 2 else line
        head = (head + " — " + _plain(tail)).strip(" —")
    return _clip(head)


def _claim_rows(md):
    """One ledger row per H/Q/C/D claim, in source order: the current bottom line
    is the last result's headline, else the verdict/prediction (a prediction),
    else the claim title."""
    rows, cur = [], None
    for line in md.splitlines():
        s = line.strip()
        mh, mq = _CLAIM_HEAD.match(s), _CLAIM_Q.match(s)
        if mh or mq:
            if cur:
                rows.append(cur)
            tok = (mh or mq).group(1)
            rest = (mh or mq).group(2)
            title = _clip(re.sub(r"^\**|\**$", "", rest).lstrip(":) ").strip("*"), 160)
            cur = {"id": tok, "status": "open", "focus": "secondary",
                   "results": [], "predict": "", "title": title}
            continue
        if not cur:
            continue
        if re.match(r"#{1,6}\s", s) and not _CLAIM_HEAD.match(s):
            rows.append(cur); cur = None; continue      # left the claim block
        st = re.search(r"\*\*status:\*\*\s*([a-z]+)", s)
        if st and st.group(1) in _STATUSES:
            cur["status"] = st.group(1)
        fo = re.search(r"\*\*focus:\*\*\s*([a-z]+)", s)
        if fo:
            cur["focus"] = fo.group(1)
        if re.search(r"\*\*\s*result\b", s):
            cur["results"].append(_result_headline(s))  # order isn't reliable
        vp = re.search(r"\*\*(?:verdict|prediction|choice):\*\*\s*(.+)", s)
        if vp and not cur["predict"]:
            cur["predict"] = _clip(re.sub(r"^(Predict that|Yes[—,-]*)\s*",
                                          "", _plain(vp.group(1)), flags=re.I))
    if cur:
        rows.append(cur)
    return rows


_DECISIVE = re.compile(
    r"\b(supported|refuted|contradicted|resolved|settled|confirmed|clean|"
    r"final|withheld|partial|blocked)\b", re.I)


def _best_result(results):
    """The most-conclusive result headline — journals order results
    inconsistently (some newest-first, some oldest-first), so pick by decisive-
    keyword count, tie-broken toward the later-listed one."""
    if not results:
        return ""
    best, score = "", -1
    for r in results:
        sc = len(_DECISIVE.findall(r))
        if sc >= score:                                 # >= : later wins ties
            best, score = r, sc
    return best


def _ledger_html(rows, project):
    if not rows:
        return ""
    body = []
    for r in rows:
        lead = "decided: " if r["status"] == "accepted" else "predicted: "
        bl = (_best_result(r["results"])
              or (r["predict"] and lead + r["predict"]) or r["title"])
        cls = " class=\"hist\"" if r["focus"] == "historical" else ""
        badge = f'<span class="badge s-{r["status"]}">{r["status"]}</span>'
        body.append(
            f'<tr{cls}><td><a href="#ref-{r["id"]}" class="xref">{r["id"]}</a>'
            f'</td><td>{badge}</td><td>{bl}</td></tr>')
    return (
        '<section class="ledger-wrap"><h2>Bottom line</h2>'
        '<p style="color:#888;font-size:.85em;margin:.2rem 0 .6rem">'
        'Current state per claim — the finding, not the history. '
        'Click a claim id to jump; open “▸ background” under a claim for its '
        'design / why / earlier results.</p>'
        '<table class="ledger"><thead><tr><th>claim</th><th>status</th>'
        '<th>bottom line</th></tr></thead><tbody>'
        + "".join(body) + "</tbody></table></section>")


def _balanced_list(html, start):
    """End index (exclusive) of the `<ul>`/`<ol>` opening at `start`, matching
    nested lists."""
    depth, i, n = 0, start, len(html)
    for m in re.finditer(r"</?(?:ul|ol)\b[^>]*>", html[start:]):
        tag = m.group(0)
        depth += 1 if tag[1] != "/" else -1
        if depth == 0:
            return start + m.end()
    return n


def _split_top_li(inner):
    """Top-level `<li>…</li>` items of a list body (nesting-aware)."""
    out, li_start, ldepth = [], None, 0
    for m in re.finditer(r"<li\b[^>]*>|</li>|<(?:ul|ol)\b[^>]*>|</(?:ul|ol)>",
                         inner):
        t = m.group(0)
        if t.startswith("<li"):
            if ldepth == 0:
                li_start = m.start()
            ldepth += 1
        elif t == "</li>":
            ldepth -= 1
            if ldepth == 0 and li_start is not None:
                out.append(inner[li_start:m.end()])
                li_start = None
    return out


# labels whose <li> is background (folded); everything else stays open. `result`
# is special-cased (keep the LAST, fold earlier ones).
_FOLD_LABELS = ("verdict", "prediction", "design", "why", "motivating",
                "note", "baseline", "links", "context", "rejected", "rationale",
                "regime", "update", "evidence", "scope", "confirmed",
                "stability fix", "prior", "extensions")


def _li_label(li):
    m = re.search(r"<strong>\s*([^:<]+)", li)
    return (m.group(1).strip().lower() if m else "")


def collapse_claims(body):
    """Fold each claim's background fields into a <details>, keeping the status
    line and the latest result open. Processed right-to-left so indices hold."""
    anchors = [m for m in re.finditer(r'<a id="ref-(H[1-9][0-9]?|Q\d+|C\d+[a-z]?'
                                      r'|D\d+)"></a>', body)]
    for a in reversed(anchors):
        ul = body.find("<ul>", a.end())
        if ul == -1:
            continue
        # the field list must be close to the claim (skip if another anchor
        # intervenes — then this claim has no field list of its own)
        nxt = body.find('<a id="ref-', a.end())
        if nxt != -1 and nxt < ul:
            continue
        end = _balanced_list(body, ul)
        inner = body[ul + 4:end - 5]                    # strip <ul> … </ul>
        lis = _split_top_li(inner)
        if len(lis) < 3:
            continue
        # keep the status line + every RESULT open (results are the findings);
        # fold only the pre-registration/rationale background.
        open_lis, fold_lis = [], []
        for li in lis:
            fold = _li_label(li).startswith(_FOLD_LABELS)
            (fold_lis if fold else open_lis).append(li)
        if not fold_lis:
            continue
        rebuilt = ("<ul>" + "".join(open_lis)
                   + '<li><details class="claim-more"><summary>background — '
                   'design · why · rationale · earlier results</summary><ul>'
                   + "".join(fold_lis) + "</ul></details></li></ul>")
        body = body[:ul] + rebuilt + body[end:]
    return body


def main(project="ldp"):
    # LDP is the default single-project view (byte-identical to the historical
    # path). A named co-located project (e.g. bitdistiller) renders ONLY its own
    # ledger segment — same read-only markdown->HTML view, its own graph namespace
    # for the focus banner, and its own output file. Never edits any .md.
    if project == "ldp":
        md = load_journal(ROOT)
        graph = build_graph(md)                    # str form -> tagged 'ldp'
        title = "LDP Research Journal"
        out = OUT
    else:
        md = segment_text(project, ROOT)
        if md is None:
            raise SystemExit(f"no co-located project {project!r} in the ledger")
        graph = build_graph([(project, md)])       # tag nodes with THIS project
        title = f"{project} Research Journal"
        out = os.path.join(ROOT, "Reports", f"{project}-journal.html")
    body = markdown.markdown(
        md, extensions=["tables", "fenced_code", "sane_lists", "toc"])

    # focus axis (SPEC_journal_focus): build the graph once to read each claim's
    # `focus` band, then (1) dim historical claim headings and (2) auto-derive the
    # active-front banner. A read-only VIEW — never edits the markdown. The banner
    # is "" when the active set is empty (LDP is the reference instance), so this
    # path adds nothing to the current ledger.
    focus_map = {n["id"]: n.get("focus", "secondary")
                 for n in graph["nodes"] if n.get("type") != "section"}
    banner = focus_banner(graph["nodes"], project)

    # Cross-references: anchor each claim definition, then linkify inline
    # mentions to those anchors (membership-filtered so H100 etc. stay plain).
    tokens = claim_tokens(md)
    body, anchors = _inject_anchors(body, tokens, focus_map)
    body = linkify(body, anchors)
    n_links = body.count('class="xref"')

    # bottom-line-first: an auto claim-ledger (id · status · finding) at the top,
    # but only for journals that don't already hand-maintain one (LDP has §0.2's
    # "State-of-belief ledger" / "verdict now" table — richer, don't shadow it).
    has_ledger = bool(re.search(r"State-of-belief ledger|verdict now", md, re.I))
    ledger = "" if has_ledger else _ledger_html(_claim_rows(md), project)
    body = collapse_claims(body)

    index = xref_index(md, anchors)
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style></head><body>
{banner}
{ledger}
{body}
{index}
</body></html>"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out}  ({len(html)//1024} KB, "
          f"{len(anchors)} anchors, {n_links} xref links)")


if __name__ == "__main__":
    import argparse
    from smh.config import current_project
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None,
                    help="co-located ledger project to render "
                         "(default: the current-project default, usually ldp)")
    a = ap.parse_args()
    # An explicit --project always wins; else the session default (== 'ldp' when
    # nothing is set, so the LDP render stays byte-identical).
    main(project=a.project if a.project is not None else current_project(ROOT))
