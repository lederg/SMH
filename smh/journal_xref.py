"""Shared journal parser: JOURNAL.md + EVIDENCE.md -> anchors, backlinks, graph.

This is the single extraction consumed by BOTH:
  - render_journal.py  (claim anchors + cross-reference index — the "cheap things")
  - journal_graph.py   (the document graph: nodes + edges + warnings)

Lifted out of render_journal.py (commit 91d821b) so there is ONE parser, not two
that drift. The first half of this file (TOKEN_RE … xref_index) is that lift,
byte-for-byte; render_journal imports it back, behavior unchanged. The second
half (extract_*_nodes, build_graph) adds the typed/hierarchy/provenance edges on
top of the same reference-edge extraction (spec §8.1, §3.2).

The journal markdown is the single source of truth. Nothing here writes it.

Phase F (spec §F): the journal lives in TWO physical files — JOURNAL.md
(status+ledger+invariants+claims+conventions) and EVIDENCE.md (RE table +
queue + changelog). `load_journal()` is the single contract point: every reader
reads the *concatenation* through it; nothing else opens the files. The cut is
physical only — no regex/anchor spans the boundary (claims live in JOURNAL,
RE rows in EVIDENCE; none crosses).
"""

import os
import re

# Order = concatenation order. Blank-line join so a heading never abuts text.
JOURNAL_FILES = ("JOURNAL.md", "EVIDENCE.md")

# T4a: co-located multi-project ledger. Each entry is (project, [files]); the
# loader stamps every node with the project of the file it came from, so two
# projects can share one graph without their short IDs (H1, RE-23) colliding —
# the namespaced key is the pair (project, id). The default keeps LDP the sole
# project, so every existing caller is byte-for-byte unchanged.
DEFAULT_LEDGER = (("ldp", JOURNAL_FILES),
                  ("fracbits", ("FRACBITS_JOURNAL.md", "FRACBITS_EVIDENCE.md")),
                  ("bitdistiller", ("BITDISTILLER_JOURNAL.md",
                                    "BITDISTILLER_EVIDENCE.md")),
                  # the meta-instance: the harness studying itself (VISION pillar
                  # 4). Process-history claims, no GPU runs / no MeasurementCatalog.
                  ("smh", ("SMH_JOURNAL.md", "SMH_EVIDENCE.md")))


def _read_files(root, files):
    """Blank-line join so a `## ` heading never abuts prior text at a file seam."""
    return "\n".join(open(os.path.join(root, f), encoding="utf-8").read()
                     for f in files)


def resolved_ledger(root=None):
    """The co-located ledger config — the (project, files) map — resolved for
    `root`. This is the ONE home for the project→journal-files map (T4a);
    `load_segments`/`ledger_files`, and the scheduler's claim gate, all source it
    here instead of re-hardcoding the list.

    Resolution order (R7 — `smh.toml [projects]` is the single registration seam):
      1. an explicit top-level `ledger` key (a stranger repo naming its ledger);
      2. else the `[projects.<name>].journal` file lists, in table order — the
         canonical per-project registration for a co-located repo like this one;
      3. else `DEFAULT_LEDGER` — the file-absent (or projects-absent) fallback, so
         removing `smh.toml` entirely still yields the same four-project map.
    `.get` not `[...]` throughout: a partial smh.toml never KeyErrors."""
    from smh import config
    root = root or config.resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = config.load_config(root)                # None for a repo with no smh.toml
    if cfg:
        if cfg.get("ledger"):
            return cfg["ledger"]
        built = [(name, tuple(p["journal"]))
                 for name, p in (cfg.get("projects") or {}).items()
                 if p.get("journal")]
        if built:
            return built
    return DEFAULT_LEDGER


def load_segments(root=None, ledger=None):
    """Read a co-located ledger into ordered (project, markdown) segments — the
    provenance-preserving loader (T4a, Option A). Each project's files are read
    and concatenated on their own, so the project of every claim is the project
    of the segment it parses from.

    `ledger` resolution (when not passed explicitly): a generated `smh.toml` at
    `root` names a stranger's ledger; absent it (the LDP repo), fall back to
    `DEFAULT_LEDGER`. The lookup is runtime + root-aware on purpose — it cannot
    live on the module constant, which has no `root` at import time. LDP ships no
    `smh.toml`, so this branch is byte-for-byte the old `DEFAULT_LEDGER` path.

    build_graph() consumes this list directly; node IDs are deduped/looked-up on
    (project, id), so a 2nd project's `H1` is distinct from LDP's `H1`."""
    from smh import config
    root = root or config.resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if ledger is None:
        ledger = resolved_ledger(root)
    return [(project, _read_files(root, files)) for project, files in ledger]


def load_journal(root=None):
    """Read the split journal and return the combined markdown — the single
    contract point for the LDP project. JOURNAL.md (claims+conventions) ++
    EVIDENCE.md (evidence+queue+changelog). Kept as the back-compatible single-
    project string view; multi-project callers use load_segments()."""
    from smh import config
    root = root or config.resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return _read_files(root, JOURNAL_FILES)


def ledger_files(project, root=None):
    """The ledger file list for one co-located project (from `smh.toml` if present,
    else `DEFAULT_LEDGER`), or None. Lets the round name a project's actual sink
    files (FracBits writes FRACBITS_*, LDP writes JOURNAL/EVIDENCE)."""
    for proj, files in resolved_ledger(root):
        if proj == project:
            return tuple(files)
    return None


def segment_text(project, root=None):
    """The concatenated markdown of ONE co-located project's segment (its ledger
    files joined), or None if no such project. The project-scoped analogue of
    `load_journal` — the namespace-aware round resolves and gates a claim against
    ITS project's numbers, not the whole ledger, so a FracBits edit can't cite an
    LDP number and vice-versa."""
    for proj, txt in load_segments(root):
        if proj == project:
            return txt
    return None

# ──────────────────────────────────────────────────────────────────────────
# LIFTED from render_journal.py — the "two cheap things" (anchors + backlinks).
# Preserve the hard-won edge cases (spec §8.1): pipe-paragraph evidence rows,
# C4b-vs-C4 boundary, slashed/comma compound refs. Pinned by the golden test.
# ──────────────────────────────────────────────────────────────────────────

# Claim-ID token grammar. Order matters: RE- and C4b-style suffixes before the
# bare forms so the longest match wins. \b guards stop H1 matching inside H100
# and C4 inside C40 (the trailing lookahead forbids a following digit/letter).
# H[1-9][0-9]? (Phase D widened H[123]→H[1-9] for H4-H8; Phase G widened it again
# to H[1-9][0-9]? for two-digit H10-H99, since harvest mints H9 then H10+) is
# collision-safe against the GPU string "H100": greedy it matches "H10", the
# (?![\w-]) lookahead then sees the trailing "0" (a word char) and fails; it
# backtracks to "H1", same failure — so the whole token fails to match. NOT H\d+ —
# that would swallow "H100". The other grammar sites use \b/$ for the same effect.
TOKEN_RE = re.compile(
    r"\b(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)(?![\w-])")


def _id_type_key(tok):
    """Sort key: group by type (H, C, RE, Q) then numeric, then suffix."""
    order = {"H": 0, "C": 1, "RE": 2, "Q": 3, "D": 4}
    m = re.match(r"(RE|H|C|Q|D)-?(\d+)([a-z]?)", tok)
    typ, num, suf = m.group(1), int(m.group(2)), m.group(3)
    return (order[typ], num, suf)


def claim_tokens(md):
    """Tokens that DEFINE a claim, scanned from the markdown source.

    H/C/H3 are defined by `### TOKEN: ...` headings; RE-NN by the leading cell
    of an evidence-log table row (`| RE-01 | ...`); Q by `**Qn ...:**` bullets.
    A token only becomes a link target if it shows up here — that membership
    test is what keeps H100 (a GPU, never a heading) from ever linkifying.
    """
    toks = set()
    for line in md.splitlines():
        s = line.strip()
        # heading definition: ### H1: ... / ### C4b [...] / #### D1: ... / ### RE-17 ...
        m = re.match(r"#{1,6}\s+(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)\b", s)
        if m:
            toks.add(m.group(1))
        # evidence-log table row: | RE-01 | ...
        m = re.match(r"\|\s*(RE-\d+[a-z]?)\s*\|", s)
        if m:
            toks.add(m.group(1))
        # open-question bullet: - **Q5 (...):** ...  /  - **Q1:** ...
        m = re.match(r"[-*]\s*\*\*(Q\d+)\b", s)
        if m:
            toks.add(m.group(1))
    return toks


def _inject_anchors(html, tokens, focus_map=None):
    """Give each claim definition a deterministic id=ref-TOKEN anchor, and
    return (html, set_of_anchored_tokens). Each token is anchored at most once,
    at its FIRST definition site (heading / table cell / bold bullet).

    `focus_map` (SPEC_journal_focus §Render #1, optional) maps token -> focus band;
    when supplied, a claim whose band is `historical` gets a `focus-historical`
    class on its heading container so the view CSS can dim it. When omitted (or a
    band is the default `secondary`/`active`) the output is byte-for-byte the
    pre-focus markup — so every existing caller is unchanged."""
    anchored = set()
    focus_map = focus_map or {}

    def heading_sub(m):
        full, tok = m.group(0), m.group(2)
        if tok in tokens and tok not in anchored:
            anchored.add(tok)
            tag = m.group(1)
            if focus_map.get(tok) == "historical":
                tag = re.sub(r"^(<h[1-6])\b", r'\1 class="focus-historical"', tag)
            return f'{tag}<a id="ref-{tok}"></a>{m.group(2)}'
        return full

    # headings: <h3 id="...">C4: ...  -> insert anchor right after the >
    html = re.sub(
        r'(<h[1-6][^>]*>)(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)\b',
        heading_sub, html)

    def cell_sub(m):
        tok = m.group(1)
        if tok in tokens and tok not in anchored:
            anchored.add(tok)
            return f'<td><a id="ref-{tok}"></a>{tok}</td>'
        return m.group(0)

    # evidence-log table cells: <td>RE-01</td>
    html = re.sub(r'<td>(RE-\d+[a-z]?)</td>', cell_sub, html)

    def pipe_sub(m):
        tok = m.group(2)
        if tok in tokens and tok not in anchored:
            anchored.add(tok)
            return f'{m.group(1)}| <a id="ref-{tok}"></a>{tok} |'
        return m.group(0)

    # Some evidence rows are separated by blank lines in the source, so the
    # markdown table breaks and they render as literal pipe paragraphs/rows:
    # `<p>| RE-17 | ...` or a following `\n| RE-19 | ...` row line. Anchor the
    # leading `| TOKEN |` of each such row.
    html = re.sub(r'(<p>|\n)\|\s*(RE-\d+[a-z]?)\s*\|', pipe_sub, html)

    def strong_sub(m):
        tok = m.group(1)
        if tok in tokens and tok not in anchored:
            anchored.add(tok)
            return f'<strong><a id="ref-{tok}"></a>{m.group(0)[8:]}'
        return m.group(0)

    # open-question bullets: <strong>Q5 ...:</strong>
    html = re.sub(r'<strong>(Q\d+)[^<]*</strong>', strong_sub, html)
    return html, anchored


# Spans we must NOT linkify inside: tags, code, pre, and our own anchors.
_SKIP_RE = re.compile(
    r"<(code|pre|a)\b[^>]*>.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)


_SELF_ANCHOR_RE = re.compile(r'<a id="ref-(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)"></a>$')


def _linkify_text(text, anchors, skip_first=None):
    """Linkify claim tokens inside a single run of plain text. `skip_first` is
    the token of the definition anchor immediately preceding this run — its
    leading occurrence is left unlinked so a heading/row never self-links."""
    def repl(m):
        tok = m.group(1)
        if tok == skip_first and m.start() == 0:
            return m.group(0)  # don't self-link the definition itself
        # T4b: the bare half of a `project:id` cross token (`fracbits:H3`) must not
        # linkify to a same-id LOCAL claim — it names another project's node, which
        # this per-project prose view doesn't anchor. Leave it as plain text.
        if m.start() >= 2 and text[m.start() - 1] == ":" \
                and text[m.start() - 2].isalnum():
            return m.group(0)
        if tok in anchors:
            return f'<a href="#ref-{tok}" class="xref">{tok}</a>'
        return m.group(0)
    return TOKEN_RE.sub(repl, text)


def linkify(html, anchors):
    """Wrap inline claim-ID mentions in #ref- links, leaving tags, code/pre
    blocks and existing anchors untouched. A token directly following its own
    `<a id="ref-TOKEN">` definition anchor is NOT linked (no heading self-link).
    Compound refs like RE-03/RE-05 linkify each valid part; the slash stays a
    literal separator, so nothing breaks across it."""
    out, last, prev_self = [], 0, None
    for m in _SKIP_RE.finditer(html):
        out.append(_linkify_text(html[last:m.start()], anchors, prev_self))
        out.append(m.group(0))  # protected span, copied verbatim
        sm = _SELF_ANCHOR_RE.search(m.group(0))
        prev_self = sm.group(1) if sm else None
        last = m.end()
    out.append(_linkify_text(html[last:], anchors, prev_self))
    return "".join(out)


def backlinks(md, anchors):
    """token -> sorted list of referring tokens. Scan each definition section
    of the source for which OTHER claim tokens it mentions, then invert."""
    # Split the source into sections, each owned by the claim it defines.
    sections = []  # (owner_token_or_None, text)
    cur_owner, cur_lines = None, []

    def flush():
        if cur_lines:
            sections.append((cur_owner, "\n".join(cur_lines)))

    for line in md.splitlines():
        s = line.strip()
        owner = None
        m = re.match(r"#{1,6}\s+(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|Q\d+)\b", s)
        if m and m.group(1) in anchors:
            owner = m.group(1)
        m2 = re.match(r"[-*]\s*\*\*(Q\d+)\b", s)
        if m2 and m2.group(1) in anchors:
            owner = m2.group(1)
        if owner is not None:
            flush()
            cur_owner, cur_lines = owner, [line]
        elif s.startswith("---") or re.match(r"#{1,6}\s", s):
            # A `---` rule or a non-claim heading (`## 5. Conventions`) ends the
            # current owner span — mirrors `_claim_block`'s terminators so prose
            # below the last claim can't be swallowed into it. Claim headings /
            # Q bullets already re-open a span via `owner` above.
            flush()
            cur_owner, cur_lines = None, []
        else:
            cur_lines.append(line)
            cur_owner = cur_owner  # noqa
    flush()

    refs = {t: set() for t in anchors}
    # Evidence-log rows: each row is its own RE-NN section (one line).
    for line in md.splitlines():
        rm = re.match(r"\|\s*(RE-\d+[a-z]?)\s*\|", line.strip())
        if rm and rm.group(1) in anchors:
            owner = rm.group(1)
            for t in set(TOKEN_RE.findall(line)):
                if t in anchors and t != owner:
                    refs[t].add(owner)
    # Heading/bullet sections from the split above.
    for owner, text in sections:
        if owner is None:
            continue
        for t in set(TOKEN_RE.findall(_strip_cross(text))):   # T4b: skip cross tokens
            if t in anchors and t != owner:
                refs[t].add(owner)

    return {t: sorted(v, key=_id_type_key) for t, v in refs.items() if v}


def xref_index(md, anchors):
    bl = backlinks(md, anchors)
    if not bl:
        return ""
    rows = []
    for tok in sorted(bl, key=_id_type_key):
        referrers = " ".join(
            f'<a href="#ref-{r}" class="xref">{r}</a>' for r in bl[tok])
        rows.append(
            f'<li><a href="#ref-{tok}" class="xref"><b>{tok}</b></a> '
            f'— referenced by: {referrers}</li>')
    return ('<h2 id="cross-reference-index">Cross-reference index</h2>'
            '<p class="meta">Backlinks between claims, computed from '
            '<code>JOURNAL.md</code> + <code>EVIDENCE.md</code>. Each token links to its '
            'definition.</p>\n'
            '<ul class="xrefindex">' + "\n".join(rows) + "</ul>")


# ──────────────────────────────────────────────────────────────────────────
# GRAPH extraction (spec §3) — tier-1 nodes + typed/hierarchy/provenance edges.
# Built on the SAME reference scan as backlinks above (spec §3.2 source #1).
# ──────────────────────────────────────────────────────────────────────────

# Node types keyed by token prefix. `evidence` = RE rows, `question` = Q bullets.
TYPE_OF = {"H": "hypothesis", "C": "conclusion", "RE": "evidence", "Q": "question",
           "D": "decision"}

# §-section heading: `## 1. Claims`.
SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)")


# ── Canonical claim schema (Phase A–F) ──────────────────────────────────────
# Every claim body opens with `- **kind:** X · **status:** Y`, optionally followed
# by **verdict:** and **Confidence:** lines. These are AUTHORITATIVE: they replace
# the id-prefix type guess and the heading-`[bracket]` status the parser used
# pre-restructure (which only ever yielded a coarse active/done/demoted set).
# NB: match the lowercase `**status:**` on the schema line — NOT the legacy
# capital `**Status:** Active —` prose line some claims still carry.
_KINDS = {"hypothesis", "conclusion", "question", "mechanism", "decision"}
# `accepted`/`superseded` are the decision lifecycle (SPEC_journal_decision_node):
# a decision enters the ledger already chosen (`accepted`) and only leaves it when
# a later decision replaces it (`superseded`) — no proposed/open rung.
_STATUSES = {"open", "testing", "supported", "contradicted", "settled",
             "retracted", "accepted", "superseded", "answered"}
# `answered` = a question-resolved synonym for `settled` (authors reach for it on
# Q nodes). Kept in-vocab so it validates rather than silently defaulting to
# `open` — the root cause of the §4 status-drift the stuck-as-question audit found
# (docs/AUDIT_stuck_as_question.md). It is a RESOLVED status: the graduation and
# focus advisories fire only on live open/testing, so `answered` is excluded.
# `focus` (SPEC_journal_focus) — a FOURTH curatorial axis orthogonal to
# kind/status/confidence. It answers "is this where we are spending attention?"
# (active = the live front · secondary = relevant-but-not-headline, the default ·
# historical = ran/correct/parked), never "is this true?". It MUST NEVER gate
# science (principle 3) — only orders attention in the render/graph. A missing
# token defaults to `secondary`, exactly as a missing status defaults to `open`.
_FOCI = {"active", "secondary", "historical"}

# ── R6: ONE field-table-driven grammar ──────────────────────────────────────
# Every canonical claim-header field is a row here — its value-capture regex body
# and, for --schema, its value domain. The parser (_claim_fields), the field
# linter (_lint_fields), and `python -m smh.journal_graph --schema` ALL read this
# table, so "what the regex wants" is written down once and cannot drift from the
# code that enforces it (spec R6).
#
# Field LABELS are matched CASE-INSENSITIVELY: a `**Kind:**` / `**Status:**` /
# `**Confidence:**` miscasing (and, symmetrically, a lowercase `**confidence:**`
# that the old capital-only regex silently DROPPED) now parse. Values of the
# vocab-validated fields (kind/status/focus) are lower-cased and matched against
# their set; crucially the parser takes the FIRST match whose value is IN-VOCAB,
# so a legacy prose line like `**Status:** Active —` (value not in _STATUSES) is
# skipped in favour of the canonical `**status:** settled` regardless of order.
_FIELD_GRAMMAR = {
    # name          value-capture   value domain (for --schema)
    "kind":       (r"([A-Za-z]+)", lambda: "/".join(sorted(_KINDS))),
    "status":     (r"([A-Za-z]+)", lambda: "/".join(sorted(_STATUSES))),
    "focus":      (r"([A-Za-z]+)", lambda: "/".join(sorted(_FOCI))),
    "confidence": (r"(.+)", lambda: "low / med / med-high / high (+ optional date)"),
    "verdict":    (r"(.+)", lambda: "free prose — the claim's one-line finding"),
    "bottom-line": (r"(.+)", lambda: "free prose — the claim's §0.2 belief-ledger "
                    "one-liner (source for the generated 'verdict now' cell, R5)"),
    "rationale":  (r"(.*)", lambda: "free prose — why the choice (decisions; §Gate)"),
    "regime":     (r"(.+)", lambda: "free prose — the focus-decision regime predicate"),
    "current":    (r"(.+)", lambda: "free prose — the claim's current state"),
}


def _field_re(name):
    """Case-insensitive matcher for one header field, built from _FIELD_GRAMMAR."""
    return re.compile(r"\*\*" + name + r":\*\*\s*" + _FIELD_GRAMMAR[name][0],
                      re.IGNORECASE)


_KIND_RE = _field_re("kind")
_STATUS_RE = _field_re("status")
_VERDICT_RE = _field_re("verdict")
_BOTTOMLINE_RE = _field_re("bottom-line")
_CONF_RE = _field_re("confidence")
_FOCUS_RE = _field_re("focus")
_REGIME_RE = _field_re("regime")
_RATIONALE_RE = re.compile(r"\*\*rationale:\*\*", re.IGNORECASE)   # decision §Gate
# A question carries a directional bet when its block says `prediction`/`Predict`
# (the bet may live in a `**verdict:** Predict …` field or an inline
# `(prediction: …)`) or literally "the bet". Feeds the Q→H graduation advisory
# (a prediction + a run experiment = a hypothesis, not an open question). The
# EXP half of that test is an evidence edge, checked at graph-build time; this
# regex alone over-matches ("prediction: none asserted"), so it never gates.
_PREDICTION_RE = re.compile(r"(?i)\b(?:the bet|predict(?:ion|ed|s)?)\b")
# …but a question may explicitly DISCLAIM a bet ("**prediction:** none asserted —
# a scope flag"). That is a genuine open question, not a stuck hypothesis, so the
# disclaimer un-sets has_prediction and the graduation advisory skips it.
_NO_PREDICTION_RE = re.compile(
    r"(?i)prediction[^.\n]{0,15}\bnone\b|\bno\s+(?:committed\s+)?prediction\b")

# Labels legal in a claim block beyond the scalar header fields: `links:` and the
# evidence/relation bullets mint edges (they are edge sources, not scalars). The
# linter treats these as recognized so it never flags them. `bottom-line` is
# reserved for R5's generated §0.2.
_RECOGNIZED_FIELDS = set(_FIELD_GRAMMAR) | {
    "links", "evidence", "supports", "contradicts", "undermines", "bottom-line"}

_SUBTYPE_RE = re.compile(r"^\s*\(([^)]*)\)")          # `### C5 (methodology): …`


def _first_valid(rx, block, vocab):
    """First value captured by `rx` in `block` whose lower-case form is in `vocab`,
    else None. Skips legacy/prose lines carrying an out-of-vocab value so the
    canonical schema line wins irrespective of order or casing (R6)."""
    for m in rx.finditer(block):
        v = m.group(1).lower()
        if v in vocab:
            return v
    return None


def _lint_fields(block):
    """R6 field linter: bold bullet labels in a claim block that are NOT a
    recognized field but ARE a close match to one — i.e. a typo/miscasing that
    would silently drop the field (`**Confidenc:**`, `**statuss:**`). Returns
    [(bad_label, suggestion)]. A genuinely freeform bullet (`**design:**`,
    `**why:**`, `**history:**`) is far from every field name and never flagged."""
    import difflib
    out = []
    for line in block.splitlines():
        m = re.match(r"\s*[-*]\s*\*\*([A-Za-z][A-Za-z /-]*?):?\*\*", line)
        if not m:
            continue
        label = re.sub(r"\s+", " ", m.group(1).strip().lower())
        if label in _RECOGNIZED_FIELDS:
            continue
        near = difflib.get_close_matches(label, sorted(_RECOGNIZED_FIELDS),
                                         n=1, cutoff=0.82)
        if near:
            out.append((m.group(1).strip(), near[0]))
    return out


def schema_text():
    """The full claim grammar, generated from the SAME tables the parser reads
    (R6): field table, id grammar, link/relation verbs, edge sources, and the
    status/kind/focus vocab. `--schema` prints this — the one written-down spec
    that cannot drift from the code because it IS the code."""
    L = ["JOURNAL CLAIM GRAMMAR (generated from smh.journal_xref)", ""]
    L.append("Header fields (labels matched case-insensitively):")
    for name, (_body, dom) in _FIELD_GRAMMAR.items():
        L.append(f"  **{name}:** <{dom()}>")
    L += ["", "Edge-source fields (mint graph edges; everything else is "
          "mention-only):",
          "  **links:** <verb target[, target…]>   typed claim/decision edges",
          "  **evidence:** / **Supports:** <RE-id>  evidence → claim",
          "  **Contradicts:** / **Undermines:** <id> contradiction edge",
          "  §2 RE table 'Linked' column            RE → claim",
          "  §3 queue 'DONE → RE-NN' marker         experiment → RE"]
    L += ["", "Link verbs (**links:** line):"]
    for verb, (kind, rev) in sorted(_LINK_KIND.items()):
        arrow = "target→owner" if rev else "owner→target"
        L.append(f"  {verb:<20} {kind} ({arrow})")
    L += ["", "Cross-project target syntax: `project:id` (e.g. `ldp:H8`)."]
    L += ["", "ID grammar: " + TOKEN_RE.pattern]
    L += ["", "Vocab:",
          "  kind   = " + "/".join(sorted(_KINDS)),
          "  status = " + "/".join(sorted(_STATUSES)),
          "  focus  = " + "/".join(sorted(_FOCI))]
    return "\n".join(L) + "\n"


def _claim_block(lines, defline):
    """Body text of the claim defined at 1-based source line `defline` — every
    line until the next claim definition / markdown heading / `---` rule. The
    canonical kind/status/verdict/Confidence fields all live inside this span."""
    out = []
    for j in range(defline, len(lines)):     # lines[defline] = first body line
        s = lines[j].strip()
        if (re.match(r"#{1,6}\s", s) or s.startswith("---")
                or re.match(r"[-*]\s*\*\*Q\d+\b", s)
                or re.match(r"\|\s*RE-\d+[a-z]?\s*\|", s)):
            break
        out.append(lines[j])
    return "\n".join(out)


def _norm_conf(text):
    """Leading confidence token, normalized: 'Medium-High (06-17…)' -> 'med-high'."""
    tok = re.split(r"[\s(—–]", text.strip(), maxsplit=1)[0].lower()
    return {"medium-high": "med-high"}.get(tok, tok) or None


def _claim_subtype(text):
    """Science vs infra/methodology, from a conclusion heading parenthetical."""
    t = text.lower()
    if "infra" in t:
        return "infra"
    if "method" in t:
        return "methodology"
    return "science"


def _claim_fields(block, fallback_type):
    """(type, status, verdict, confidence, focus) parsed from a claim body block.
    type falls back to the id-prefix guess, status to 'open', and focus to
    'secondary' when the relevant schema token is absent (RE rows have no block;
    harvested stubs may lag). `focus` is validated against _FOCI; a malformed
    value normalizes to the 'secondary' default here — the raw token is re-read
    at the node so the gate can flag malformed markup (SPEC_journal_focus §Gate)."""
    vm = _VERDICT_RE.search(block)
    cm = _CONF_RE.search(block)
    ntype = _first_valid(_KIND_RE, block, _KINDS) or fallback_type
    status = _first_valid(_STATUS_RE, block, _STATUSES) or "open"
    verdict = _clean_title(vm.group(1)) if vm else ""
    conf = _norm_conf(cm.group(1)) if cm else None
    focus = _first_valid(_FOCUS_RE, block, _FOCI) or "secondary"
    return ntype, status, verdict, conf, focus


def _bottom_line(block):
    """The claim's `**bottom-line:**` field — the §0.2 belief-ledger one-liner (R5),
    or None. Distinct from `**verdict:**` (the §1 finding); this is the single source
    for the generated 'verdict now' cell, so §0.2 stops being hand-synced."""
    m = _BOTTOMLINE_RE.search(block)
    return m.group(1).strip() if m else None


def _stamp_focus_malformed(node, block):
    """Record a malformed `**focus:**` value on the node so the gate can flag it
    (SPEC_journal_focus §Gate: a value outside _FOCI is bad markup, same class as
    an unknown kind → a counted, gating warning). A `**focus:**` label present but
    with NO in-vocab value among its occurrences is the malformed case; the first
    raw token is recorded for the message."""
    ms = list(_FOCUS_RE.finditer(block))
    if ms and not any(m.group(1).lower() in _FOCI for m in ms):
        node["focus_malformed"] = ms[0].group(1)


def _clean_title(rest):
    """Heading/bullet text after the claim token, minus status brackets and
    leading separators/backticks."""
    t = re.sub(r"\[[^\]]*\]", " ", rest)
    t = t.strip().lstrip(":").strip().lstrip("-").strip().strip("`").strip()
    return re.sub(r"\s+", " ", t)[:240]


def _summary(title):
    """First sentence / clause of a title (spec §3.1 summary)."""
    return re.split(r"\s[—–-]\s|\.\s", title)[0][:200]


def _type_of(tok):
    pre = re.match(r"(RE|H|C|Q|D)", tok).group(1)
    return TYPE_OF[pre]


def extract_claim_nodes(md, project="ldp"):
    """One node per claim (H/C/RE/Q), harvested from the journal's existing ID
    scheme. Type/status/summary/confidence come from each claim's canonical
    schema block (spec §5); RE evidence rows carry no schema block. First
    definition site wins. `project` (T4a) stamps the source-file provenance onto
    every node; the namespaced key downstream is (project, id)."""
    nodes = {}
    tokens = claim_tokens(md)
    lines = md.splitlines()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        # H/C/D/Q heading (RE is a table row — handled below). Q also has a legacy
        # `- **Qn (...):**` bullet form (LDP/FracBits) parsed further down; the
        # SPEC_journal_layout §2 heading form `### Qn:` is parsed here, whichever
        # defines the token first winning. D = decision (SPEC_journal_decision_node):
        # a `#### D1:` heading parsed by the SAME claim-body reader, kind/status from
        # the canonical schema line.
        m = re.match(r"#{1,6}\s+(C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)\b(.*)", s)
        if m and m.group(1) in tokens and m.group(1) not in nodes:
            tok, rest = m.group(1), m.group(2)
            block = _claim_block(lines, i)
            ntype, status, verdict, conf, focus = _claim_fields(block, _type_of(tok))
            title = _clean_title(rest)
            node = {
                "id": tok, "project": project, "type": ntype, "title": title,
                "summary": _summary(verdict or title), "status": status,
                "focus": focus, "anchor": f"ref-{tok}", "section_line": i,
                "evidence_plots": [],
            }
            _stamp_focus_malformed(node, block)
            node["field_lint"] = _lint_fields(block)
            if conf:
                node["confidence"] = conf
            bl = _bottom_line(block)
            if bl:
                node["bottom_line"] = bl
            if ntype == "decision":       # gate needs a rationale line (§Gate)
                node["has_rationale"] = bool(_RATIONALE_RE.search(block))
                rm = _REGIME_RE.search(block)   # focus decision's regime predicate
                if rm:
                    node["regime"] = rm.group(1).strip()
            if ntype == "conclusion":     # science vs infra/methodology
                sm = _SUBTYPE_RE.match(rest)
                node["subtype"] = _claim_subtype(sm.group(1)) if sm else "science"
            if ntype == "question":       # Q→H graduation advisory (does it bet?)
                node["has_prediction"] = bool(_PREDICTION_RE.search(block)) \
                    and not _NO_PREDICTION_RE.search(block)
            nodes[tok] = node
            continue
        # evidence-log row: | RE-01 | date | type | description | source | linked |
        rm = re.match(r"\|\s*(RE-\d+[a-z]?)\s*\|", s)
        if rm and rm.group(1) in tokens and rm.group(1) not in nodes:
            tok = rm.group(1)
            cells = [c.strip() for c in s.replace(r"\|", "\x00").split("|")]
            subtype = cells[3] if len(cells) > 3 else ""
            desc = cells[4].replace("\x00", "|") if len(cells) > 4 else ""
            title = _clean_title(desc.replace("**", ""))
            # R1 (SPEC_journal_v2_drift_resistance): the row's trailing `Linked`
            # cell is a DECLARED edge field (like a `**links:**` line) — bare tokens
            # (`C1,C2`) or verb-typed trailers (`tests bitdistiller:H1`). Pipe-split
            # ends in a trailing '' from the closing `|`, so the field is cells[-2].
            linked = (cells[-1] or (cells[-2] if len(cells) > 2 else "")) \
                .replace("\x00", "|")
            nodes[tok] = {
                "id": tok, "project": project, "type": "evidence",
                "subtype": subtype, "title": title, "summary": _summary(title),
                "status": "done", "anchor": f"ref-{tok}",
                "section_line": i, "evidence_plots": [], "linked": linked,
            }
            continue
        # open-question bullet: - **Q5 (...):** ...  (kind may be 'mechanism')
        qm = re.match(r"[-*]\s*\*\*(Q\d+)\b(.*)", s)
        if qm and qm.group(1) in tokens and qm.group(1) not in nodes:
            tok, rest = qm.group(1), qm.group(2)
            qblock = _claim_block(lines, i)
            ntype, status, verdict, conf, focus = _claim_fields(qblock, "question")
            # Title from the bold `**Qn:**` / `**Qn (label):**` header (R5 — the old
            # `.replace("(", …)` left the `label):` fragment in the title, leaking
            # into the generated §0.2 claim column). Form B: the parenthetical is the
            # author's short name — use it, minus any trailing "— OPEN, needs …"
            # status cruft. Form A (or a one-word label like "(paradox)"): fall back
            # to the question prose after `:**`.
            _h, _sep, _tail = rest.partition(":**")
            lm = re.match(r"\s*\((.*?)\)\s*$", _h) if _sep else None
            label = re.split(r"\s+[—–-]\s+", lm.group(1))[0].strip() if lm else ""
            prose = _clean_title((_tail if _sep else rest).replace("**", ""))
            title = label if len(label.split()) >= 2 else (prose or label)
            node = {
                "id": tok, "project": project, "type": ntype, "title": title,
                "summary": _summary(verdict or title), "status": status,
                "focus": focus, "anchor": f"ref-{tok}", "section_line": i,
                "evidence_plots": [],
            }
            _stamp_focus_malformed(node, qblock)
            node["field_lint"] = _lint_fields(qblock)
            if conf:
                node["confidence"] = conf
            bl = _bottom_line(qblock)
            if bl:
                node["bottom_line"] = bl
            if ntype == "question":       # Q→H graduation advisory (does it bet?)
                node["has_prediction"] = bool(_PREDICTION_RE.search(qblock)) \
                    and not _NO_PREDICTION_RE.search(qblock)
            nodes[tok] = node
    return nodes


def _section_index(md):
    """[(line, id, title)] for every `## N.` section heading."""
    out = []
    for i, line in enumerate(md.splitlines(), 1):
        m = SECTION_RE.match(line)
        if m:
            out.append((i, f"§{m.group(1)}", m.group(2).strip()))
    return out


def _section_of(line, sections):
    """(id, title, heading_line) of the §-section containing `line`."""
    hit = None
    for sl, sid, st in sections:
        if sl <= line:
            hit = (sid, st, sl)
        else:
            break
    return hit if hit else (None, None, None)


def _exp_status(bracket):
    b = bracket.upper()
    for k in ("DONE", "IN PROGRESS", "BLOCKED", "QUEUED", "IDEA", "TODO"):
        if k in b:
            return k.lower().replace(" ", "_")
    return "queued"


def _clean_exp_title(text):
    t = text.strip().lstrip("`").strip()
    cuts = [t.find(sep) for sep in ("**", "—", "–", ". ")]
    cuts = [c for c in cuts if c > 0]
    if cuts:
        t = t[:min(cuts)]
    return t.strip(" .`*—–:")[:120]


def extract_experiment_nodes(md, nodes_seen, project="ldp"):
    """Live experiment-queue entries in §3 become `experiment` nodes (spec §3.1).
    Both the `### \\`[STATUS]\\` Title` blocks and the `- **\\`[STATUS]\\` Title**`
    queue bullets. IDs are synthetic EXP-N in document order. `produces` holds the
    RE/Q tokens parsed from a `DONE → RE-NN` marker (spec §3.2 provenance)."""
    lines = md.splitlines()
    # Bound to the experiment-queue section (§3 after the Phase E renumber; was §5).
    start = end = None
    for i, line in enumerate(lines, 1):
        if re.match(r"##\s+3\.\s", line):   # §3 by number (retitled to backlog, task B)
            start = i
        elif start and i > start and re.match(r"##\s+\d+\.", line):
            end = i
            break
    if start is None:
        return {}
    end = end or len(lines) + 1

    nodes = {}
    idx = 0
    for i in range(start, end):
        line = lines[i - 1]
        s = line.strip()
        is_head = s.startswith("###")
        is_bullet = s.startswith("- **")
        if not (is_head or is_bullet):
            continue
        bm = re.search(r"\[([^\]]*)\]", s)
        if not bm:
            continue  # e.g. `### Queue (run in order ...)` — not an experiment
        bracket = bm.group(1)
        title = _clean_exp_title(s[bm.end():])
        if not title:
            continue
        idx += 1
        eid = f"EXP-{idx}"
        produces = re.findall(r"RE-\d+[a-z]?|Q\d+", bracket)
        nodes[eid] = {
            "id": eid, "project": project, "type": "experiment",
            "title": title, "summary": _summary(title),
            "status": _exp_status(bracket), "anchor": None,
            "section_line": i, "evidence_plots": [],
            "produces": [p for p in produces if p in nodes_seen],
        }
    return nodes


def _owner_lines(md, anchors):
    """owner-token -> [(line, text)]. Mirrors `backlinks`' section model so the
    reference edges match the cheap-thing backlink index exactly: claim headings
    / Q bullets open an owner span; evidence rows are their own one-line owner."""
    out = {}
    cur = None
    for i, line in enumerate(md.splitlines(), 1):
        s = line.strip()
        rm = re.match(r"\|\s*(RE-\d+[a-z]?)\s*\|", s)
        if rm and rm.group(1) in anchors:
            out.setdefault(rm.group(1), []).append((i, line))
            continue
        m = re.match(r"#{1,6}\s+(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)\b", s)
        m2 = re.match(r"[-*]\s*\*\*(Q\d+)\b", s)
        owner = None
        if m and m.group(1) in anchors:
            owner = m.group(1)
        elif m2 and m2.group(1) in anchors:
            owner = m2.group(1)
        if owner:
            cur = owner
            out.setdefault(cur, []).append((i, line))
        elif s.startswith("---") or re.match(r"#{1,6}\s", s):
            # End the owner span at a `---` rule or a non-claim heading,
            # mirroring `_claim_block` (and `backlinks`) so Conventions/§7 prose
            # below the last §1.3 claim is not absorbed into it and minted as
            # false edges. Claim headings / Q bullets re-open a span via `owner`.
            cur = None
        elif cur:
            out.setdefault(cur, []).append((i, line))
    return out


def _bullet_label(text):
    """If a line is a labeled provenance bullet, return its kind. Recognizes
    **Supports:**/**Evidence:** -> supports, **Contradicts:** -> contradicts,
    **Undermines:** -> undermines (reverse-direction contradicts)."""
    m = re.match(r"[-*]?\s*\*\*([A-Za-z][A-Za-z /]*?):?\*\*", text.strip())
    if not m:
        return None
    lab = m.group(1).strip().lower()
    if lab in ("supports", "evidence"):
        return "supports"
    if lab in ("contradicts", "contradict"):
        return "contradicts"
    if lab == "undermines":
        return "undermines"
    return None


# Lifecycle links (Phase A–F): a `- **links:** refined-by H4, tested-by RE-21,
# ties C2, contrasts H7` bullet carries typed claim→claim edges inline. We draw
# these directly from the declared link verbs (the journal's own lifecycle
# vocabulary) — NOT from numeric heuristics. Each verb maps to an edge kind and a
# direction; the `-by` verbs reverse it (`<owner> refined-by H4` ⇒ edge H4→owner).
# T4b: cross-paper verbs. The within-segment lifecycle vocab (refines/.../contrasts)
# describes one project's internal claim flow; these describe how a claim in one
# project RELATES TO a claim in another (the cross-paper relation vocabulary for
# every SMH instance — ratified with the user). A cross link is distinguished not
# by its verb but by a `project:` prefix on its target (D1); the same verb may be
# used within a segment or across (e.g. `tested-by fracbits:H3` mirrors a cross
# edge with the existing within-segment verb).
_CROSS_VERBS = ("generalizes", "reproduces", "tests-survival-of",
                "conditions", "echoes", "bridges")

# The optional `([a-z][a-z0-9_]*):` prefix is folded INTO the target group so
# `_TYPED_LINK_RE.findall(...)` keeps returning (verb, target) 2-tuples (the bare
# id case is unchanged, e.g. ("refines","H10")); a cross target reads as
# ("reproduces","ldp:H8"). The build does NOT use this regex (see _VERB_RE +
# _CROSS_TOKEN_RE) — it documents/pins the grammar.
_TYPED_LINK_RE = re.compile(
    r"\b(refined-by|refines|superseded-by|supersedes|tested-by|ties|contrasts|"
    r"underpins|informed-by|deprioritizes|generalizes|reproduces|tests-survival-of|"
    r"conditions|echoes|bridges)\s+"
    r"((?:[a-z][a-z0-9_]*:)?(?:RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+))(?![\w-])",
    re.IGNORECASE)

# verb -> (edge kind, reverse?). reverse=True makes the edge tok->owner.
_LINK_KIND = {
    "refines": ("refines", False),
    "refined-by": ("refines", True),
    "supersedes": ("supersedes", False),
    "superseded-by": ("supersedes", True),
    "tested-by": ("tested_by", False),
    "ties": ("ties", False),
    "contrasts": ("contrasts", False),
    # decision vocab (SPEC_journal_decision_node): a decision `underpins` the
    # claim(s) it is a premise of (forward: D --underpins--> claim) and
    # `supersedes` a prior decision (reuses the existing supersedes kind above).
    "underpins": ("underpins", False),
    # R2 (SPEC_journal_v2_drift_resistance): a decision may be `informed-by` any
    # node — including an RE row (forward: D --informed_by--> node). Semantics: "this
    # measured fact SHAPED the choice", never "this decision is TESTED by it". The
    # decision-gate allows it (it is not a result edge) so a session can finally
    # cite the evidence behind a choice instead of burying the number in the body.
    "informed-by": ("informed_by", False),
    # focus vocab (SPEC_journal_focus): the focus decision `deprioritizes` each
    # claim it demotes to historical/secondary (forward: D --deprioritizes--> claim).
    # Curatorial, NOT a result edge — it never counts as evidence (see _decision_gate).
    "deprioritizes": ("deprioritizes", False),
    # T4b cross-paper kinds (all forward: owner --verb--> target).
    "generalizes": ("generalizes", False),
    "reproduces": ("reproduces", False),
    "tests-survival-of": ("tests_survival_of", False),
    "conditions": ("conditions", False),
    "echoes": ("echoes", False),
    "bridges": ("bridges", False),
    # R1 (SPEC_journal_v2_drift_resistance): verbs used in the RE-row trailing
    # `Linked` field (an evidence row declaring its own provenance). Direction is
    # from the RE owner: `RE-01 tests H1` => the hypothesis is tested_by the row
    # (rev), `RE-08 extends RE-07` => RE-08 builds on RE-07 (fwd). `supports`
    # joins as a links verb so a row may state a positive verdict inline.
    "tests": ("tested_by", True),
    "extends": ("extends", False),
    "sharpens": ("sharpens", False),
    "seeds": ("seeds", False),
    "relates": ("ties", False),
    "supports": ("supports", False),
}

# Just the verb alternation. We segment a `**links:**` line at each verb, so a
# comma-list under one verb (`ties C2, Q2`) attaches to ALL its tokens, and we
# scan ONLY the declared links line — never prose (else "ties"/"contrasts" as
# plain English words would mint phantom edges). Includes the cross verbs so a
# cross `**links:**` line segments the same way.
# NOTE: multi-word verbs (`tests-survival-of`, `tested-by`) MUST precede the bare
# `tests` alternative — regex alternation is leftmost-wins, and `\btests\b` would
# otherwise match the `tests` prefix of `tests-survival-of` and shadow it.
_VERB_RE = re.compile(
    r"\b(refined-by|refines|superseded-by|supersedes|tested-by|ties|contrasts|"
    r"underpins|informed-by|deprioritizes|generalizes|reproduces|tests-survival-of|"
    r"conditions|echoes|bridges|extends|sharpens|seeds|relates|supports|tests)\b",
    re.IGNORECASE)

# T4b: a cross-project link target carries a `project:` prefix (`ldp:H8`). The
# bare-id grammar (TOKEN_RE) would still match the `H8` half and try to resolve it
# WITHIN the local segment — minting a spurious local edge/backlink/warning when
# the bare id happens to name a local node (e.g. fracbits also defines H2). So
# every within-segment scan strips cross tokens first (_strip_cross); cross refs
# are collected in _build_segment and resolved against the global (project, id)
# keyspace in build_graph's 2nd pass. A space (no colon-space) is required before
# the id, so prose `note: H1` never matches — only the authored `project:id`.
_CROSS_TOKEN_RE = re.compile(
    r"\b([a-z][a-z0-9_]*):(RE-\d+[a-z]?|C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)(?![\w-])")


def _strip_cross(text):
    """Blank out `project:id` cross tokens so a within-segment TOKEN_RE scan can't
    pick up the bare id half (T4b)."""
    return _CROSS_TOKEN_RE.sub(" ", text)


# Keyword promotion of a plain reference to a typed provenance edge (spec §3.2:
# "keep heuristics few and documented; unmatched stays ref"). Recall-expanded for
# loose contradictions a drafter states without a formal **Contradicts:** bullet.
_CONTRA_KW = ("contradict", "refut", "undermin", "fails to", "no effect",
              "does not", "weakens", "tension with", "retract", "null",
              "no consistent winner")
# Explicit support language — used only to decide whether a node-type-inferred
# `supports` edge is grounded (it stays a support either way); see _kw_signal.
_SUPPORT_KW = ("support", "confirm", "consistent with", "as predicted",
               "in line with", "corroborat")

# Sentence split: an RE row is ONE line that mixes support for one claim with
# retraction of another, so a line-wide keyword scan cross-talks (e.g. RE-17's
# "retracted"/"null" target C3/C4b, not the C8 it supports). Scope every keyword
# to the clause that actually holds the token.
_SENT_SPLIT = re.compile(r"(?<=[.;])\s+")


def _kw_signal(owner, tok, owner_lines):
    """Sentence-scoped keyword signal for the edge owner->tok: returns
    ('contra'|'support'|None). Only clauses that mention `tok` are inspected, so a
    contra/support word stays bound to its own claim on a multi-claim line."""
    contra = support = False
    for _, text in owner_lines.get(owner, []):
        for sent in _SENT_SPLIT.split(text):
            if tok not in TOKEN_RE.findall(sent):
                continue
            low = sent.lower()
            if any(w in low for w in _CONTRA_KW):
                contra = True
            if any(w in low for w in _SUPPORT_KW):
                support = True
    if contra:
        return "contra"
    if support:
        return "support"
    return None


def _classify_ref(owner, tok, nodes, owner_lines):
    """Type a reference edge owner->tok. A sentence-scoped contradict keyword
    wins; else by node types (evidence→claim = supports, hypothesis→evidence =
    tested_by); else plain ref."""
    if _kw_signal(owner, tok, owner_lines) == "contra":
        return "contradicts"
    rt, tt = nodes[owner]["type"], nodes[tok]["type"]
    if rt == "evidence" and tt in ("conclusion", "hypothesis"):
        return "supports"
    if rt == "hypothesis" and tt == "evidence":
        return "tested_by"
    if rt == "experiment" and tt == "evidence":
        return "produces"
    return "ref"


_REF_PRIORITY = {"supports": 0, "contradicts": 0, "tested_by": 2,
                 "produces": 2, "ref": 5}

# R1 switch (SPEC_journal_v2_drift_resistance). True = pre-R1 behavior (any prose
# co-mention mints a graph edge). False = mention ≠ assertion (only declared
# links:/evidence/Contradicts bullets + produces + contains mint edges). Held True
# until the one-time `--edge-audit` triage is reviewed, then flipped to False in
# the same commit that updates the golden edge counts with the audited diff.
MENTION_MINTS_EDGES = False


def _build_segment(md, project="ldp", plot_map=None, ref_edges=None):
    """Parse ONE project's markdown into its piece of the graph (T4a). Returns
    {nodes, sec_nodes, edges, backlinks, warnings, advisories} where `nodes`/
    `sec_nodes` are dicts keyed by short id (within a single project IDs are
    unique); build_graph() re-keys them on (project, id) at merge time. Every
    node and edge is stamped with `project`. Token references resolve within this
    segment, so cross-project refs are simply absent here (that is T4b).

    `ref_edges` (R1): None → the module default `MENTION_MINTS_EDGES`; True/False
    force it (the audit tool passes both)."""
    if ref_edges is None:
        ref_edges = MENTION_MINTS_EDGES
    claims = extract_claim_nodes(md, project)
    exps = extract_experiment_nodes(md, claims, project)
    nodes = {**claims, **exps}
    if plot_map:
        for rid, files in plot_map.items():
            if rid in nodes:
                nodes[rid]["evidence_plots"] = list(files)

    anchors = set(claims)
    sections = _section_index(md)
    owner_lines = _owner_lines(md, anchors)

    edges = []
    seen = set()  # undirected claim/exp pairs already represented by an edge
    # T4b: project-qualified link targets, collected here, resolved post-merge in
    # build_graph (the only point where every project's nodes exist). Each entry:
    # {owner, project, kind, rev, target_proj, target_id, line}.
    cross_refs = []

    def add(src, dst, kind, line):
        key = frozenset((src, dst))
        if key in seen:
            return False
        seen.add(key)
        edges.append({"src": src, "dst": dst, "kind": kind, "line": line,
                      "project": project})
        return True

    # (2) Hierarchy edges — section containment (spec §3.2 #2). Section nodes are
    # lightweight containers giving the layout its spine; not counted in tier-1.
    sec_nodes = {}
    for nid, n in nodes.items():
        sid, stitle, sline = _section_of(n["section_line"], sections)
        if sid:
            n["section"] = sid
            if sid not in sec_nodes:
                sec_nodes[sid] = {
                    "id": sid, "project": project, "type": "section",
                    "title": stitle, "summary": stitle, "status": "active",
                    "anchor": None, "section_line": sline, "evidence_plots": [],
                }
            edges.append({"src": sid, "dst": nid, "kind": "contains",
                          "line": n["section_line"], "project": project})

    # (3a) Provenance: supersedes/refines/ties now come ONLY from the declared
    # `**links:**` lifecycle vocabulary (handled in 3b). The old numeric heuristic
    # (Cn DEMOTED ⇒ C(n+1) supersedes Cn; CnX ⇒ supersedes Cn) is gone — it
    # fabricated edges the journal never asserts.

    # (3b) Provenance: explicit **Supports:/Evidence:/Contradicts:** bullets, plus
    # the typed **links:** lifecycle edges (refines/supersedes — added FIRST so the
    # undirected `add()` dedup gives them priority over a later plain ref).
    for owner, ls in owner_lines.items():
        if owner not in nodes:
            continue
        for lineno, text in ls:
            if "**links:**" not in text:        # declared links field only
                continue
            verbs = list(_VERB_RE.finditer(text))
            for vi, vm in enumerate(verbs):
                kind, rev = _LINK_KIND[vm.group(1).lower()]
                end = verbs[vi + 1].start() if vi + 1 < len(verbs) else len(text)
                span = text[vm.end():end]
                # cross-project targets (`project:id`) defer to the 2nd pass.
                for cm in _CROSS_TOKEN_RE.finditer(span):
                    cross_refs.append({
                        "owner": owner, "project": project, "kind": kind,
                        "rev": rev, "target_proj": cm.group(1),
                        "target_id": cm.group(2), "line": lineno})
                # within-segment bare tokens resolve here; strip cross first so the
                # bare half of `ldp:H8` can't mint a spurious local edge.
                for tok in TOKEN_RE.findall(_strip_cross(span)):
                    if tok not in nodes or tok == owner:
                        continue
                    add(tok, owner, kind, lineno) if rev else \
                        add(owner, tok, kind, lineno)
        for lineno, text in ls:
            lab = _bullet_label(text)
            if not lab:
                continue
            for tok in set(TOKEN_RE.findall(text)):
                if tok not in nodes or tok == owner:
                    continue
                if lab == "supports":
                    add(tok, owner, "supports", lineno)
                elif lab == "contradicts":
                    add(tok, owner, "contradicts", lineno)
                elif lab == "undermines":
                    add(owner, tok, "contradicts", lineno)

    # (3c) Provenance: experiment --produces--> evidence (from `DONE → RE-NN`).
    for nid, n in nodes.items():
        if n["type"] == "experiment":
            for rid in n.get("produces", []):
                if rid in nodes:
                    add(nid, rid, "produces", n["section_line"])

    # (3d) R1 (SPEC_journal_v2_drift_resistance): the RE row's trailing `Linked`
    # cell is a DECLARED provenance field, on par with a `**links:**` line — NOT
    # prose. A verb prefix types the edge from the RE owner (`tests H1` → the
    # hypothesis is tested_by the row; `extends RE-07` → this row builds on that
    # one; cross tokens `bitdistiller:H4` defer to the 2nd pass). A bare token
    # list (ldp `C1,C2`) carries no verb, so each token is typed by node inference
    # (_classify_ref: evidence→claim = supports; a sentence-scoped contradict
    # keyword in the row's own description = contradicts) — the SAME typing the
    # pre-R1 prose scan gave, now sourced from the declared column so description
    # co-mentions (RE-14's "(C5)"/"(Q5)") no longer leak in. Runs AFTER the
    # links/bullet block so an explicit **Contradicts:** bullet keeps priority
    # over a node-type-inferred support for the same pair.
    for owner, n in nodes.items():
        if n["type"] != "evidence":
            continue
        cell = n.get("linked", "") or ""
        if not cell.strip() or cell.strip() in ("—", "-"):
            continue
        lineno = n["section_line"]
        verbs = list(_VERB_RE.finditer(cell))
        for vi, vm in enumerate(verbs):
            kind, rev = _LINK_KIND[vm.group(1).lower()]
            end = verbs[vi + 1].start() if vi + 1 < len(verbs) else len(cell)
            span = cell[vm.end():end]
            for cm in _CROSS_TOKEN_RE.finditer(span):
                # A trailer names its own project explicitly (`tests
                # bitdistiller:H1` on a bitdistiller row). A self-project ref is a
                # WITHIN-segment edge, not cross — routing it through the cross
                # resolver would mint a phantom same-project "cross" edge that
                # duplicates any bare declaration (e.g. smh's H claims already
                # `**links:** tested-by RE-01`). Only a genuinely other-project
                # target (`ldp:Q6`) defers to the 2nd pass.
                if cm.group(1) == project:
                    tid = cm.group(2)
                    if tid in nodes and tid != owner:
                        add(tid, owner, kind, lineno) if rev else \
                            add(owner, tid, kind, lineno)
                    continue
                cross_refs.append({
                    "owner": owner, "project": project, "kind": kind, "rev": rev,
                    "target_proj": cm.group(1), "target_id": cm.group(2),
                    "line": lineno})
            for tok in TOKEN_RE.findall(_strip_cross(span)):
                if tok not in nodes or tok == owner:
                    continue
                add(tok, owner, kind, lineno) if rev else \
                    add(owner, tok, kind, lineno)
        # bare tokens outside any verb clause (the ldp `Linked` column): a leading
        # verb-free segment. Type each by node inference against the row's prose.
        pre = cell if not verbs else cell[:verbs[0].start()]
        for tok in TOKEN_RE.findall(_strip_cross(pre)):
            if tok not in nodes or tok == owner:
                continue
            add(owner, tok, _classify_ref(owner, tok, nodes, owner_lines), lineno)

    # (1) Reference edges — the mention scan that also feeds the backlink index.
    # R1 (SPEC_journal_v2_drift_resistance): mention ≠ assertion. When `ref_edges`
    # is False (the R1 default once the edge audit has run), prose mentions no
    # longer mint graph edges — only the DECLARED sources above (links:/evidence/
    # Contradicts bullets, produces, contains) do. Prose still renders as
    # hyperlinks via `backlinks()`; the graph just stops inferring relations from
    # co-mention. `ref_edges=True` reproduces the pre-R1 behavior and is used by
    # the `--edge-audit` tool to diff old-vs-new and triage every prose-minted edge.
    advisories = []
    if ref_edges:
        pairs = []
        for owner, ls in owner_lines.items():
            if owner not in nodes:
                continue
            text = _strip_cross(" ".join(t for _, t in ls))   # T4b: drop cross tokens
            firstline = {}
            for ln, t in ls:
                for tok in TOKEN_RE.findall(_strip_cross(t)):
                    firstline.setdefault(tok, ln)
            for tok in set(TOKEN_RE.findall(text)):
                if tok in nodes and tok != owner:
                    kind = _classify_ref(owner, tok, nodes, owner_lines)
                    pairs.append((_REF_PRIORITY[kind], owner, tok, kind,
                                  firstline.get(tok, ls[0][0])))
        pairs.sort(key=lambda p: (p[0], p[1], p[2]))
        for _, src, dst, kind, line in pairs:
            added = add(src, dst, kind, line)
            if added and kind == "supports" \
                    and _kw_signal(src, dst, owner_lines) is None:
                advisories.append(
                    f"L{line}: {src}->{dst} 'supports' inferred from node types only "
                    f"(no support/contradict language near the mention) — verify direction")

    warnings = _collect_warnings(md, nodes)

    # focus axis (SPEC_journal_focus §Gate), per-segment so it is per-project for
    # free. Two severities: a malformed enum is bad markup → a counted, GATING
    # warning; a parked-but-still-running claim (historical + testing/open) means
    # GPU is being spent off the live front → a non-gating ADVISORY (visibility,
    # never blocks the green gate — focus must NEVER gate science, principle 3).
    for nid, n in nodes.items():
        bad = n.get("focus_malformed")
        if bad:
            warnings.append(
                f"line {n['section_line']}: {project}:{nid} has malformed "
                f"**focus:** '{bad}' (not active/secondary/historical). "
                f"fix: use one of active / secondary / historical")
        # R6: a bold bullet label that looks like a typo'd header field (would be
        # silently dropped) is a GATING warning naming the field it meant.
        for badf, sugg in n.get("field_lint", []):
            warnings.append(
                f"line {n['section_line']}: {project}:{nid} has unknown header "
                f"field '**{badf}:**'. fix: known fields are "
                f"{', '.join(sorted(_RECOGNIZED_FIELDS))}; did you mean '{sugg}'?")
        if n.get("focus") == "historical" and n.get("status") in ("testing", "open"):
            advisories.append(
                f"L{n['section_line']}: {project}:{nid} is focus=historical but "
                f"status={n['status']} — spending attention on a deprioritized "
                f"claim; re-activate or stop the run")
        # A LIVE `kind: question` (status open/testing) that carries a directional
        # prediction AND has run/queued experiment evidence (an edge to an RE node)
        # is really a hypothesis that never graduated — the "stuck-as-question"
        # pattern (docs/AUDIT_stuck_as_question.md). Non-gating ADVISORY: prompt the
        # manual Q→H re-kind, never perform it (kind is a human judgment, must not
        # gate science). Only open/testing fire — a settled/supported/superseded Q
        # is resolved, nothing to graduate (same live-status gate as the focus
        # advisory above). NB an out-of-vocab status like a bare "answered" defaults
        # to "open" (_STATUSES), so it reads as live here until re-kinded — that is
        # the §4 status-drift the audit flags, not a second bug.
        if (n.get("type") == "question" and n.get("has_prediction")
                and n.get("status") in ("open", "testing")):
            has_evidence = any(
                (e["src"] == nid and nodes.get(e["dst"], {}).get("type") == "evidence")
                or (e["dst"] == nid and nodes.get(e["src"], {}).get("type") == "evidence")
                for e in edges)
            if has_evidence:
                advisories.append(
                    f"L{n['section_line']}: {project}:{nid} is kind=question but "
                    f"carries a prediction and has experiment evidence — graduation "
                    f"candidate, re-kind Q→H (see bitdistiller:Q16→H12)")

    return {
        "nodes": nodes,
        "sec_nodes": sec_nodes,
        "edges": edges,
        "cross_refs": cross_refs,
        "backlinks": backlinks(md, anchors),
        "warnings": warnings,
        "advisories": advisories,
    }


def build_graph(md, plot_map=None, ref_edges=None):
    """Parse the journal into {nodes, edges, backlinks, warnings, advisories}
    (spec §2). Accepts either a single markdown string (legacy single-project
    call — treated as the `ldp` project) or a list of (project, markdown)
    segments from load_segments() (T4a co-located ledger).

    Two-tier model (spec §3.0): tier-1 nodes only (claims + live experiments +
    §-section containers). Artifacts stay tier-2 leaves — `evidence_plots` on the
    parent RE node (passed in via `plot_map`: {RE-id: [filename, ...]}).

    Nodes are deduped on (project, id): a 2nd project's `H1` is its own node, so
    co-located projects never overwrite one another. Edges/backlinks are within
    a single project (cross-project edges are T4b); each carries its `project`.
    The returned `nodes`/`edges` shapes are unchanged for the single-project
    case — every node simply gains a `project` field."""
    segments = [("ldp", md)] if isinstance(md, str) else list(md)

    g_nodes = {}    # (project, id) -> node
    g_sec = {}      # (project, sid) -> section node
    edges, warnings, advisories = [], [], []
    cross_refs = []
    backlinks_out = {}
    for project, seg_md in segments:
        seg = _build_segment(seg_md, project, plot_map, ref_edges)
        for nid, n in seg["nodes"].items():
            g_nodes[(project, nid)] = n
        for sid, sn in seg["sec_nodes"].items():
            g_sec[(project, sid)] = sn
        edges.extend(seg["edges"])
        cross_refs.extend(seg["cross_refs"])
        warnings.extend(seg["warnings"])
        advisories.extend(seg["advisories"])
        for tok, refs in seg["backlinks"].items():
            backlinks_out.setdefault(tok, []).extend(refs)

    # (T4b) Second pass: resolve project-qualified cross-refs against the global
    # (project, id) keyspace — the only point where BOTH namespaces' nodes exist.
    # A resolved ref mints a `cross:true` edge with project-qualified endpoints
    # (D4); a dangling one (typo'd id or unknown project) warns loudly, no edge
    # (D6). Endpoints stay qualified strings for display; *_key carry the pair.
    cross_seen = set()
    for cr in cross_refs:
        src_key = (cr["project"], cr["owner"])
        dst_key = (cr["target_proj"], cr["target_id"])
        if dst_key not in g_nodes:
            known = ", ".join(sorted({p for p, _ in g_nodes})) or "(none)"
            warnings.append(
                f"line {cr['line']}: cross-ref {cr['project']}:{cr['owner']} "
                f"-> {cr['target_proj']}:{cr['target_id']} resolves to no node "
                f"(unknown project or undefined id). "
                f"fix: check the `project:` prefix (known projects: {known}) and "
                f"that the target id is defined in that project")
            continue
        if src_key not in g_nodes:        # owner vanished — defensive, shouldn't hit
            continue
        if cr["rev"]:                     # `-by` verb: edge target -> owner
            src_key, dst_key = dst_key, src_key
        sig = (src_key, dst_key, cr["kind"])
        if sig in cross_seen:
            continue
        cross_seen.add(sig)
        edges.append({
            "src": f"{src_key[0]}:{src_key[1]}",
            "dst": f"{dst_key[0]}:{dst_key[1]}",
            "kind": cr["kind"], "line": cr["line"], "cross": True,
            "src_key": src_key, "dst_key": dst_key})

    # Decision-node gate (SPEC_journal_decision_node §Gate). Run here, after the
    # cross-ref pass, because a decision's `underpins`/`supersedes` edges may be
    # cross-project (`bitdistiller:H1`) and only exist once both namespaces resolve.
    warnings.extend(_decision_gate(g_nodes, edges))

    # R5 transition (non-gating): once a project opts claims into the generated §0.2
    # (any node carries a bottom-line), advise on every cell where the generated
    # ledger diverges from the hand table still in the markdown — the one-release
    # warn-before-replace. Silent until the reviewed migration populates the fields,
    # so today it adds nothing (0-warn/advisory count unchanged).
    for project, seg_md in segments:
        proj_nodes = [n for n in g_nodes.values() if n.get("project") == project]
        for msg in belief_ledger_diff(seg_md, proj_nodes, edges, project):
            advisories.append(
                f"belief-ledger[{project}]: {msg}  fix: update the claim's "
                f"**bottom-line:** (or regenerate §0.2) — do not hand-edit the table")

    return {
        "nodes": list(g_nodes.values()) + list(g_sec.values()),
        "edges": edges,
        "backlinks": backlinks_out,
        "warnings": warnings,
        "advisories": advisories,
    }


def edge_audit(root=None, plot_map=None):
    """R1 one-time edge audit (read-only). Diffs the edge set under the OLD rule
    (any prose co-mention mints an edge) vs the NEW rule (declared sources only)
    per project, and returns every PROSE-MINTED edge as a triage row
    `{project, src, kind, dst, line}` — the edges that will DISAPPEAR when
    `MENTION_MINTS_EDGES` flips to False. Each row must then be human-reviewed:
    PROMOTED into the owner's `links:`/`evidence:` line, or DROPPED deliberately
    (it was owner-span co-mention noise). No edge is lost silently."""
    segs = load_segments(root)
    old = build_graph(segs, plot_map, ref_edges=True)["edges"]
    new = build_graph(segs, plot_map, ref_edges=False)["edges"]

    def dkey(e):
        return (e.get("project", "ldp"), e["src"], e["dst"], e["kind"])

    kept = {dkey(e) for e in new}
    rows = []
    for e in old:
        if e.get("cross"):                 # cross edges are already declared-only
            continue
        if dkey(e) not in kept:
            rows.append({"project": e.get("project", "ldp"), "src": e["src"],
                         "kind": e["kind"], "dst": e["dst"], "line": e.get("line")})
    rows.sort(key=lambda r: (r["project"], r["src"], r["dst"]))
    return rows


def focus_banner(nodes, project=None):
    """Auto-derived 'active front' banner for one project segment
    (SPEC_journal_focus §Render #2). Lists the claims tagged `focus = active` and
    the regime predicate from the project's focus decision (the decision node
    carrying a `regime:` line), so the banner can never drift from the tags the way
    a hand-written `PRIMARY SUBJECT` blockquote does.

    Returns "" when the active set is empty — the correct, expected output for a
    project that is the reference instance now, not the live front (e.g. LDP). A
    consumer (render_journal) injects the returned HTML at the top of the segment;
    it is a read-only VIEW, never written back into the markdown.

    `nodes` is a built-graph node list (each carries `focus`, `project`, `type`,
    `id`); pass `project` to scope to one segment, or None for a single-project
    graph."""
    seg = [n for n in nodes
           if n.get("type") != "section"
           and (project is None or n.get("project") == project)]
    active = [n for n in seg if n.get("focus") == "active"]
    if not active:
        return ""
    regime = next((n["regime"] for n in seg
                   if n.get("type") == "decision" and n.get("regime")), None)
    ids = ", ".join(n["id"] for n in sorted(active, key=lambda n: n["id"]))
    reg_html = (f'<div class="focus-regime">regime: {_html_escape(regime)}</div>'
                if regime else "")
    return (
        '<div class="focus-banner">'
        '<b>Active front</b> '
        f'<span class="focus-active-ids">{_html_escape(ids)}</span>'
        f'{reg_html}'
        '<div class="focus-note">auto-derived from <code>**focus:** active</code> '
        'tags — secondary claims are in scope, historical claims are parked '
        '(kept in full, dimmed).</div>'
        '</div>')


# ── R5: generated §0.2 belief ledger (single source; stop hand-syncing) ──────────
# Like the focus banner, the §0.2 "State-of-belief" table is a generated VIEW of §1:
# a claim OPTS IN by carrying a `**bottom-line:**` field, and its row is emitted from
# the claim header (id / status / confidence), the bottom-line (the 'verdict now'
# cell) and its DECLARED evidence edges (the 'key RE' cell) — never hand-copied. The
# block is marker-fenced so `render`/a future migration can replace the hand table by
# construction. This pass ships the generator + a non-gating transition diff; the hand
# table stays authoritative until the reviewed ledger migration populates the fields.
_BL_BEGIN = ("<!-- BEGIN generated:belief-ledger (R5) — do not hand-edit; regenerate "
             "with `python -m smh.journal_graph --belief-ledger <project>` -->")
_BL_END = "<!-- END generated:belief-ledger -->"
_RE_ID_RE = re.compile(r"^RE-\d+[a-z]?$")
_BL_CLAIM_KINDS = ("hypothesis", "conclusion", "question", "decision", "mechanism")


def _clip_words(text, n):
    ws = str(text).split()
    return " ".join(ws[:n]) + ("…" if len(ws) > n else "")


def _esc_cell(text):
    return str(text).replace("|", r"\|").strip()


def _norm_cell(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _key_res(claim_id, project, edges):
    """The RE ids a claim connects to via its DECLARED edges (within its own project)
    — the 'key RE' column, derived from the graph so §0.2 can never cite an RE the
    claim is not actually linked to."""
    res = set()
    for e in edges:
        if e.get("cross") or (e.get("project") or "ldp") != (project or "ldp"):
            continue
        a, b = e.get("src"), e.get("dst")
        if a == claim_id and _RE_ID_RE.match(b or ""):
            res.add(b)
        elif b == claim_id and _RE_ID_RE.match(a or ""):
            res.add(a)
    return sorted(res, key=lambda r: int(re.findall(r"\d+", r)[0]))


def belief_ledger_rows(nodes, edges, project=None):
    """Generated §0.2 rows (dicts) for every claim that opts in with a
    `**bottom-line:**` field, in source order. id/status/confidence from the claim
    header, verdict-now from bottom-line, key RE from declared evidence edges."""
    seg = [n for n in nodes
           if n.get("type") in _BL_CLAIM_KINDS and n.get("bottom_line")
           and (project is None or n.get("project") == project)]
    seg.sort(key=lambda n: n.get("section_line", 0))
    return [{
        "id": n["id"],
        "claim": _clip_words(n.get("title", ""), 12),
        "verdict": n["bottom_line"],
        "conf": n.get("confidence") or "—",
        "key_re": ", ".join(_key_res(n["id"], n.get("project"), edges)) or "—",
        "status": n.get("status", "open"),
    } for n in seg]


def belief_ledger_table(nodes, edges, project=None):
    """The marker-fenced markdown for the generated §0.2 table (a generated VIEW,
    like focus_banner). "" when no claim has opted in."""
    rows = belief_ledger_rows(nodes, edges, project)
    if not rows:
        return ""
    L = [_BL_BEGIN,
         "| id | claim (≤12 words) | verdict now | conf | key RE | status |",
         "|----|---------------------|-------------|------|--------|--------|"]
    for r in rows:
        L.append(f"| {r['id']} | {_esc_cell(r['claim'])} | {_esc_cell(r['verdict'])} "
                 f"| {_esc_cell(r['conf'])} | {_esc_cell(r['key_re'])} | "
                 f"{_esc_cell(r['status'])} |")
    L.append(_BL_END)
    return "\n".join(L)


_HAND_LEDGER_ROW = re.compile(r"^\|\s*(C\d+[a-z]?|H[1-9][0-9]?|D\d+|Q\d+)\s*\|(.*)\|\s*$")


def _parse_hand_ledger(md):
    """Rows of the existing hand §0.2 'State-of-belief' table (id -> the 5 cells after
    id), located by its 'verdict now' header; ends at the first non-table line."""
    out, in_tbl = {}, False
    for s in md.splitlines():
        low = s.lower()
        if "verdict now" in low and s.count("|") >= 6:
            in_tbl = True
            continue
        if in_tbl:
            if not s.strip().startswith("|"):
                if out:
                    break
                continue
            if re.match(r"^\|[-\s|]+\|$", s.strip()):
                continue
            m = _HAND_LEDGER_ROW.match(s.strip())
            if m:
                cells = [c.strip() for c in m.group(2).split("|")]
                if len(cells) >= 5:
                    out[m.group(1)] = cells[:5]
    return out


def belief_ledger_diff(md, nodes, edges, project=None):
    """Human-readable mismatches between the GENERATED belief ledger and the hand
    §0.2 table in `md` (R5 transition — warn, don't replace). Only opted-in claims
    (with a bottom-line) are compared, so it is silent until the migration populates
    the fields, then flags every cell that drifted — the hand-sync bug this kills."""
    gen = {r["id"]: r for r in belief_ledger_rows(nodes, edges, project)}
    if not gen:
        return []
    hand = _parse_hand_ledger(md)
    out = []
    for cid, r in gen.items():
        if cid not in hand:
            out.append(f"{cid}: has a bottom-line (opted in) but is absent from the "
                       f"hand §0.2 table")
            continue
        _claim, h_verdict, h_conf, h_keyre, h_status = hand[cid]
        for label, g, h in (("verdict now", r["verdict"], h_verdict),
                            ("conf", r["conf"], h_conf),
                            ("key RE", r["key_re"], h_keyre),
                            ("status", r["status"], h_status)):
            if _norm_cell(g) != _norm_cell(h):
                out.append(f"{cid} {label}: generated {g!r} != hand {h!r}")
    return out


def _ledger_region(lines):
    """(start, end) inclusive line indices of the §0.2 table block in `lines`:
    the marker-fenced generated block if already present, else the hand table
    located by its 'verdict now' header row through its last contiguous table
    row. None if no §0.2 table is found. Lets `--write` be idempotent (replace
    the fenced block) yet also drive the FIRST swap (replace the hand table)."""
    begin = end = None
    for i, s in enumerate(lines):
        t = s.strip()
        if t == _BL_BEGIN:
            begin = i
        elif t == _BL_END and begin is not None:
            end = i
            break
    if begin is not None and end is not None:
        return begin, end
    for i, s in enumerate(lines):
        if "verdict now" in s.lower() and s.count("|") >= 6:
            j = i
            while j + 1 < len(lines) and lines[j + 1].strip().startswith("|"):
                j += 1
            return i, j
    return None


def belief_ledger_write(md, nodes, edges, project=None):
    """Rewrite the §0.2 table region of `md` with the generated marker-fenced
    block (idempotent — regenerating a fenced block is a no-op when nothing
    drifted). Returns (new_md, changed). Raises if there is nothing to generate
    or no region to replace."""
    table = belief_ledger_table(nodes, edges, project)
    if not table:
        raise ValueError(f"no claim in {project or '(all)'} carries a "
                         f"**bottom-line:** field — nothing to generate")
    lines = md.splitlines()
    region = _ledger_region(lines)
    if region is None:
        raise ValueError("no §0.2 belief-ledger table found to replace "
                         "(expected a 'verdict now' header row or generated markers)")
    a, b = region
    new_md = "\n".join(lines[:a] + table.splitlines() + lines[b + 1:])
    if md.endswith("\n"):
        new_md += "\n"
    return new_md, new_md != md


def belief_ledger_write_file(project, nodes, edges, root=None):
    """Rewrite the §0.2 table region in a project's primary journal file
    (`files[0]` — the claims/ledger file) in-place. Returns (path, changed)."""
    from smh import config
    root = root or config.resolve_root(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = ledger_files(project, root)
    if not files:
        raise ValueError(f"unknown project {project!r}")
    path = os.path.join(root, files[0])
    with open(path, encoding="utf-8") as f:
        md = f.read()
    new_md, changed = belief_ledger_write(md, nodes, edges, project)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_md)
    return path, changed


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _edge_src_key(e):
    """(project, id) of an edge's source in the global keyspace. Cross edges (T4b)
    already carry resolved `src_key`/`dst_key` tuples; within-segment edges key on
    the segment's project + bare id."""
    return tuple(e["src_key"]) if e.get("cross") else (e.get("project", "ldp"), e["src"])


def _edge_dst_key(e):
    return tuple(e["dst_key"]) if e.get("cross") else (e.get("project", "ldp"), e["dst"])


def _decision_gate(g_nodes, edges):
    """Warnings specific to `decision` nodes (SPEC_journal_decision_node §Gate).
    A decision asserts a CHOICE, not a result, so it is the easiest node to gate:

      - it must NOT carry a result/evidence edge (no supports/tested_by/produces/
        contradicts, no bare co-mention of an RE row) — that would make it a claim.
        EXCEPTION (R2): an explicit `informed-by` edge is allowed, even to an RE
        row — it says the evidence shaped the choice, not that it tested it;
      - it needs a `rationale` line and ≥1 outgoing `underpins`/`supersedes` edge
        (a decision that affects nothing is noise);
      - a `superseded` decision needs an outgoing `supersedes` edge to the
        replacing decision (else its successor is dangling).

    Keyed on the global (project, id) keyspace so cross-project `underpins` edges
    count. Returns a list of warning strings (gating; counted by --check)."""
    decisions = {k: n for k, n in g_nodes.items() if n.get("type") == "decision"}
    if not decisions:
        return []
    evidence_keys = {k for k, n in g_nodes.items() if n.get("type") == "evidence"}
    _RESULT_KINDS = {"supports", "tested_by", "produces", "contradicts"}
    warns = []
    for dk, n in decisions.items():
        label = f"{dk[0]}:{dk[1]}"
        touches_result = has_link = has_supersedes = False
        for e in edges:
            sk, dkk = _edge_src_key(e), _edge_dst_key(e)
            if dk != sk and dk != dkk:
                continue
            other = dkk if sk == dk else sk
            # R2: an explicit `informed-by` edge is a legal decision→evidence link
            # (the fact shaped the choice); it never counts as a result edge.
            if e["kind"] == "informed_by":
                pass
            elif e["kind"] in _RESULT_KINDS or other in evidence_keys:
                touches_result = True
            # `deprioritizes` (SPEC_journal_focus) is a curatorial edge: it counts
            # toward "affects something" so a pure-focus decision (demotes claims
            # without underpinning any) isn't flagged as noise — but it is NOT a
            # result kind, so it never trips touches_result above.
            if sk == dk and e["kind"] in ("underpins", "supersedes", "deprioritizes"):
                has_link = True
                has_supersedes = has_supersedes or e["kind"] == "supersedes"
        if touches_result:
            warns.append(
                f"decision {label} carries a result/evidence edge — a decision "
                f"asserts a choice, not a result. "
                f"fix: if the evidence shaped the choice, cite it as "
                f"`informed-by RE-NN` in links: (allowed); otherwise drop the "
                f"supports/tested_by link and keep the number in its RE row")
        if not has_link:
            warns.append(
                f"decision {label} has no `underpins`/`supersedes` edge — it affects "
                f"nothing. "
                f"fix: add `underpins <claim>` (or `supersedes <D-id>` / "
                f"`deprioritizes <claim>`) to its links: line")
        if not n.get("has_rationale"):
            warns.append(
                f"decision {label} has no `rationale` line (§Gate requires one). "
                f"fix: add a `**rationale:** …` line to the decision body")
        if n.get("status") == "superseded" and not has_supersedes:
            warns.append(
                f"decision {label} is 'superseded' but has no outgoing `supersedes` "
                f"edge to the decision that replaced it. "
                f"fix: add `supersedes <D-id>` to its links: line")
    return warns


def _nearest_hint(tok, nodeids):
    """R8: ` (nearest ids: …)` suffix for an unresolved-token warning, or "" when
    nothing is close. A typo'd `RE-99` should point the writer at `RE-19`, not just
    say 'no node'. difflib over the same-prefix id space keeps the suggestion in the
    right series."""
    import difflib
    pre = re.match(r"(RE-|C|H|D|Q)", tok)
    pool = [i for i in nodeids
            if not pre or i.startswith(pre.group(1))]
    near = difflib.get_close_matches(tok, pool, n=3, cutoff=0.5)
    return f" (nearest ids: {', '.join(near)})" if near else ""


def _collect_warnings(md, nodes):
    """Best-effort + loud (spec §6): flag inline tokens that resolve to no node
    (typo'd RE-99, out-of-range C-numbers), plus §3 queue entries that SILENTLY
    drop an experiment node or its provenance edge. Silent drops are the failure
    mode."""
    warnings = []
    flagged = set()
    nodeids = set(nodes)
    lines = md.splitlines()

    # §3 queue lint — guards the EXP-node writer contract (see
    # extract_experiment_nodes, which reads ONE line per entry). An entry needs a
    # `[STATUS]` bracket AND a same-line title; a `DONE → RE-NN` marker must name a
    # real node. Either failure otherwise drops a node / a `produces` edge with no
    # other signal — exactly the multi-line-bullet trap (bracket on one source
    # line, title on the next) that hid two queue items pre-Tier-1.
    start = end = None
    for i, line in enumerate(lines, 1):
        if re.match(r"##\s+3\.\s", line):   # §3 by number (retitled to backlog, task B)
            start = i
        elif start and i > start and re.match(r"##\s+\d+\.", line):
            end = i
            break
    if start is not None:
        for i in range(start, end or len(lines) + 1):
            s = lines[i - 1].strip()
            if not (s.startswith("###") or s.startswith("- **")):
                continue
            bm = re.search(r"\[([^\]]*)\]", s)
            if not bm:
                continue
            bracket = bm.group(1)
            if not _clean_exp_title(s[bm.end():]):
                warnings.append(
                    f"line {i}: §3 queue entry has a [{bracket}] bracket but no "
                    f"same-line title — the experiment node is silently dropped "
                    f"(bracket and title on separate source lines?). "
                    f"fix: put the [STATUS] bracket and the title on the SAME "
                    f"source line")
            for rid in re.findall(r"RE-\d+[a-z]?|Q\d+", bracket):
                if rid not in nodeids:
                    flagged.add(rid)
                    warnings.append(
                        f"line {i}: §3 marker [{bracket}] names '{rid}', which "
                        f"defines no node (typo, or the RE row isn't written yet). "
                        f"fix: write the RE row / Q claim first, or correct the id"
                        f"{_nearest_hint(rid, nodeids)}")

    # Generic: inline tokens that resolve to no node. Strip cross tokens first —
    # a valid `ldp:H8` (resolved in build_graph's 2nd pass) must NOT warn here, and
    # a dangling one is flagged loudly by that pass instead (T4b, D6). A BARE token
    # mistakenly written in the wrong segment still warns (it should carry a prefix).
    for i, line in enumerate(lines, 1):
        for tok in TOKEN_RE.findall(_strip_cross(line)):
            if tok not in nodeids and tok not in flagged:
                flagged.add(tok)
                warnings.append(
                    f"line {i}: token '{tok}' is referenced but defines no node. "
                    f"fix: define the node, or fix the typo"
                    f"{_nearest_hint(tok, nodeids)}")
    return warnings
