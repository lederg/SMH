"""Write journal claim nodes (H / Q / D) safely: mint · retract · set-status.

The node detail block is the single source of truth; the §0.2/§1 belief-ledger
table is GENERATED from it (`journal_xref.belief_ledger_write_file`). So a write
touches only the node block, then regenerates the table — no hand-matched table
row, no block↔row drift by construction.

Every write is a TRANSACTION that preserves the two journal invariants
(`smh graph` at 0 warnings, `smh gate` green):

    snapshot the ledger file → apply the block edit → regenerate the table →
    rebuild the graph → if it introduces NO new warning and the fact gate passes,
    keep it; otherwise restore the snapshot and raise.

Project-agnostic: everything routes through the current-project default (or an
explicit project), reuses the shared id allocator + reservation store
(`pipeline.harvest`), the canonical vocab (`journal_xref`), and the shared block
renderer (`journal_node`). A co-located project with no §0.2 table (FracBits/SMH)
simply skips the table-regen step.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from smh import config
from smh import journal_xref as jx
from smh.journal_node import render_node, schema_line
from smh.pipeline import harvest
from smh.pipeline import gate
from smh.pipeline import facts


class MintError(Exception):
    """A mint/retract/set-status was refused (bad input, or would break a journal
    invariant). Carries a human-readable reason; the CLI prints it and exits 1."""


_PREFIX = {"hypothesis": "H", "question": "Q", "decision": "D"}
_MINT_KINDS = ("hypothesis", "question", "decision")
# subsection a node kind lands under — matched case-insensitively against the
# ledger's `### N.M <Heading>` rows (works for BitDistiller's "1.1 Hypotheses" /
# "1.3 Questions & mechanisms" / "1.4 Decisions" and any journal that names its
# subsections the same way).
_SECTION_KW = {"hypothesis": "hypothes", "question": "question", "decision": "decision"}


def _root() -> str:
    return config.resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_project(project: str, root: str) -> None:
    known = config.known_projects(root)
    if project not in known:
        raise MintError(f"unknown project {project!r}; known: {', '.join(sorted(known))}")


def _ledger_path(project: str, root: str) -> str:
    files = jx.ledger_files(project, root)
    if not files:
        raise MintError(f"no ledger file for project {project!r}")
    return os.path.join(root, files[0])


# ── link parsing / validation ──────────────────────────────────────────────

def _links_value(links, project: str, node_keys: set) -> str:
    """Render `--link "verb target"` specs into the `links:` bullet value, after
    validating each verb (∈ `_LINK_KIND`) and that its target resolves to an
    existing node. `target` may be a bare id (current project) or `proj:id`
    (cross-project, T4b)."""
    parts = []
    for spec in links:
        m = re.match(r"^\s*([A-Za-z][A-Za-z-]*)\s+(\S+)\s*$", spec)
        if not m:
            raise MintError(f"bad --link {spec!r}; expected 'verb target', e.g. 'refines H8'")
        verb, target = m.group(1).lower(), m.group(2)
        if verb not in jx._LINK_KIND:
            raise MintError(f"unknown link verb {verb!r}; known: "
                            f"{', '.join(sorted(jx._LINK_KIND))}")
        tp, tid = target.split(":", 1) if ":" in target else (project, target)
        if (tp, tid) not in node_keys:
            raise MintError(f"link target {target!r} → {tp}:{tid} is not an existing node")
        parts.append(f"{verb} {target}")
    return ", ".join(parts)


# ── markdown block insertion / field editing ───────────────────────────────

def _insert_block(md: str, kind: str, block: str) -> str:
    """Append `block` at the end of the subsection matching `kind` (before the
    next same-or-higher-level header). Raises if the subsection is absent — a
    journal must declare where a kind lives; we never guess a location."""
    lines = md.splitlines()
    kw = _SECTION_KW[kind]
    hdr_i = hdr_lvl = None
    for i, ln in enumerate(lines):
        m = re.match(r"^(#{2,4})\s+(.*)", ln)
        if m and kw in m.group(2).lower():
            hdr_i, hdr_lvl = i, len(m.group(1))
            break
    if hdr_i is None:
        raise MintError(f"no subsection for {kind} (a header containing {kw!r}) "
                        f"in the ledger — create it first")
    end = len(lines)
    for j in range(hdr_i + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s", lines[j])
        if m and len(m.group(1)) <= hdr_lvl:
            end = j
            break
    while end - 1 > hdr_i and lines[end - 1].strip() == "":   # trim trailing blanks
        end -= 1
    block_lines = block.rstrip("\n").split("\n")
    new = lines[:end] + [""] + block_lines + lines[end:]
    out = "\n".join(new)
    return out + "\n" if md.endswith("\n") else out


def _node_span(lines, cid: str):
    """[start, end) line span of node `cid`'s block. A heading node (H/D) runs to
    the next same-or-higher heading; a bullet node (Q) runs while following lines
    stay indented (its nested sub-bullets)."""
    defre = re.compile(rf"^\s*(#{{2,4}}\s+{re.escape(cid)}\b|[-*]\s*\*\*{re.escape(cid)}\b)")
    start = next((i for i, ln in enumerate(lines) if defre.match(ln)), None)
    if start is None:
        raise MintError(f"node {cid} not found in the ledger")
    head = re.match(r"^(#{2,4})\s", lines[start])
    if head:
        lvl = len(head.group(1))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            m = re.match(r"^(#{1,6})\s", lines[j])
            if m and len(m.group(1)) <= lvl:
                end = j
                break
        return start, end
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() == "" or re.match(r"^\s+\S", ln):   # blank or indented → in block
            continue
        end = j
        break
    return start, end


def _edit_field(lines, start, end, field, fn) -> bool:
    """Apply `fn(line) -> line` to the first line in [start,end) carrying
    `**field:**`. Returns True if a line matched."""
    rex = re.compile(rf"\*\*{re.escape(field)}:\*\*")
    for j in range(start, end):
        if rex.search(lines[j]):
            lines[j] = fn(lines[j])
            return True
    return False


def _set_status_in(lines, start, end, status: str) -> None:
    if not _edit_field(lines, start, end, "status",
                       lambda ln: re.sub(r"(\*\*status:\*\*\s*)([A-Za-z]+)",
                                         lambda m: m.group(1) + status, ln)):
        raise MintError("node has no **status:** field to update")


def _prepend_bottom_line(lines, start, end, note: str) -> None:
    _edit_field(lines, start, end, "bottom-line",
                lambda ln: re.sub(r"(\*\*bottom-line:\*\*\s*)", lambda m: m.group(1) + note + " — ", ln, count=1))


def _append_link(lines, start, end, link: str) -> None:
    def add(ln):
        if re.search(r"\(none[^)]*\)|—\s*$", ln):            # placeholder value → replace
            return re.sub(r"(\*\*links:\*\*\s*).*", lambda m: m.group(1) + link, ln)
        return ln.rstrip() + f", {link}"
    if not _edit_field(lines, start, end, "links", add):
        raise MintError("node has no **links:** field to append to")


# ── the transaction ─────────────────────────────────────────────────────────

def _transact(project: str, root: str, mutate, *, gate_text: str | None = None) -> str:
    """Snapshot → mutate → regen table → verify (graph warnings delta + fact gate)
    → commit or roll back. `mutate(md)->md` is the pure block edit; `gate_text`
    (if given) is fact-gated BEFORE any write. Returns the ledger path."""
    path = _ledger_path(project, root)
    with open(path, encoding="utf-8") as f:
        orig = f.read()

    if gate_text is not None:
        res = gate.verify(gate_text)
        if not res["ok"]:
            raise MintError("fact gate: unsourced number(s) — "
                            + "; ".join(f"{t!r} (L{ln})" for t, ln, _ in res["unsourced"])
                            + ". A minted claim's prose may not introduce a number "
                              "not already in the journal.")

    baseline = set(jx.build_graph(jx.load_segments(root))["warnings"])
    md2 = mutate(orig)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md2)
    try:
        g = jx.build_graph(jx.load_segments(root))
        try:
            jx.belief_ledger_write_file(project, g["nodes"], g["edges"], root)
        except ValueError:
            pass          # project has no generated §0.2 table region — nothing to sync
        after = jx.build_graph(jx.load_segments(root))
        new_warn = set(after["warnings"]) - baseline
        if new_warn:
            raise MintError("edit would introduce graph warning(s):\n  "
                            + "\n  ".join(sorted(new_warn)))
    except Exception:
        with open(path, "w", encoding="utf-8") as f:
            f.write(orig)          # roll back — journal never left in a warned state
        raise
    return path


# ── public operations ───────────────────────────────────────────────────────

def mint(project, title, *, kind=None, status=None, focus="active",
         confidence="Low", bottom_line=None, links=(), prediction=None,
         mechanism=None, design=None, context=None, rejected=None,
         rationale=None, root=None, confirm=None):
    """Mint a new H/Q/D node. `kind` is inferred from `title` when omitted
    (`harvest._infer_kind`). Allocates the next free id in the project's series,
    renders the §1 block, and lands it via `_transact`. Returns (id, kind, path)."""
    root = root or _root()
    _check_project(project, root)
    kind = kind or harvest._infer_kind({"title": title})
    if kind not in _MINT_KINDS:
        raise MintError(f"kind must be one of {_MINT_KINDS}, not {kind!r}")
    if focus not in jx._FOCI:
        raise MintError(f"focus must be one of {sorted(jx._FOCI)}, not {focus!r}")
    status = status or ("accepted" if kind == "decision" else "open")
    if status not in jx._STATUSES:
        raise MintError(f"status must be one of {sorted(jx._STATUSES)}, not {status!r}")
    bottom_line = bottom_line or title

    g = jx.build_graph(jx.load_segments(root))
    node_keys = {(n.get("project", "ldp"), n["id"]) for n in g["nodes"]}
    cid = harvest.next_free_id(_PREFIX[kind], g["nodes"], project=project,
                               reserved=harvest.reserved_ids(project))
    links_val = _links_value(links, project, node_keys)

    if kind == "decision":
        if not rationale:
            raise MintError("a decision needs --rationale (the decision-gate requires it)")
        if not links_val:
            raise MintError("a decision needs ≥1 --link (e.g. 'underpins H8' / "
                            "'supersedes D2') — a decision must affect something")
        fields = [("kind", schema_line("decision", status, focus)),
                  ("bottom-line", bottom_line)]
        if context:
            fields.append(("context", context))
        if rejected:
            fields.append(("rejected", rejected))
        fields += [("rationale", rationale), ("links", links_val)]
    else:
        fields = [("kind", schema_line(kind, status, focus)),
                  ("bottom-line", bottom_line),
                  ("confidence", confidence)]
        if prediction:
            fields.append(("prediction (the bet)", prediction))
        if mechanism:
            fields.append(("mechanism", mechanism))
        if design:
            fields.append(("design", design))
        fields += [("evidence", "—"),
                   ("links", links_val or "(none — standalone)")]

    block = render_node(cid, kind, title, fields)
    if confirm and not confirm(f"mint {project}:{cid} ({kind}) —\n\n{block}"):
        return None, kind, None
    harvest.reserve_id(project, cid)
    try:
        path = _transact(project, root, lambda md: _insert_block(md, kind, block),
                         gate_text=block)
    finally:
        harvest.release_id(project, cid)     # the id now lives in the file (or the write rolled back)
    return cid, kind, path


def set_status(project, cid, status, *, note=None, root=None, confirm=None):
    """Change node `cid`'s status (validated ∈ vocab); optionally prepend a `note`
    to its bottom-line. Regenerates the table + re-verifies. Returns the path."""
    root = root or _root()
    _check_project(project, root)
    if status not in jx._STATUSES:
        raise MintError(f"status must be one of {sorted(jx._STATUSES)}, not {status!r}")
    if confirm and not confirm(f"set {project}:{cid} status → {status}"
                               + (f"  (+note: {note})" if note else "")):
        return None

    def mutate(md):
        lines = md.splitlines()
        s, e = _node_span(lines, cid)
        _set_status_in(lines, s, e, status)
        if note:
            _prepend_bottom_line(lines, s, e, note)
        out = "\n".join(lines)
        return out + "\n" if md.endswith("\n") else out

    return _transact(project, root, mutate)


def retract(project, cid, *, reason=None, supersede=None, date=None, root=None, confirm=None):
    """Retract node `cid`: status → retracted, a dated `RETRACTED …` marker on its
    bottom-line, and (with `--supersede NEW`) a superseded-by/supersedes edge pair.
    The block is kept (IDs-forever). Returns the path."""
    root = root or _root()
    _check_project(project, root)
    date = date or _today()
    marker = f"RETRACTED {date}" + (f" ({reason})" if reason else "")

    if supersede:
        g = jx.build_graph(jx.load_segments(root))
        keys = {(n.get("project", "ldp"), n["id"]) for n in g["nodes"]}
        tp, tid = supersede.split(":", 1) if ":" in supersede else (project, supersede)
        if (tp, tid) not in keys:
            raise MintError(f"--supersede target {supersede!r} → {tp}:{tid} is not an existing node")

    if confirm and not confirm(f"retract {project}:{cid}  ({marker})"
                               + (f", superseded by {supersede}" if supersede else "")):
        return None

    def mutate(md):
        lines = md.splitlines()
        s, e = _node_span(lines, cid)
        _set_status_in(lines, s, e, "retracted")
        _prepend_bottom_line(lines, s, e, marker)
        if supersede:
            _append_link(lines, s, e, f"superseded-by {supersede}")
            # the reciprocal edge on the superseding node (best-effort: same file)
            try:
                s2, e2 = _node_span(lines, supersede.split(":", 1)[-1])
                _append_link(lines, s2, e2, f"supersedes {cid}")
            except MintError:
                pass          # superseding node lives in another file / project — skip reciprocal
        out = "\n".join(lines)
        return out + "\n" if md.endswith("\n") else out

    return _transact(project, root, mutate)
