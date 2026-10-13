"""Render a journal claim node (H / Q / D) to its as-built §1 markdown block.

ONE format source. The exact on-disk shape a landed claim must take — the heading
form and the `- **field:** value` bullets — lived inline in
`pipeline/harvest.py:_draft`; it is extracted here so the harvest drafter and the
`smh mint` writer share a single renderer and can never drift apart. A block this
produces round-trips through `journal_xref.build_graph` into a graph node.

Per-kind form (matches JOURNAL.md §1.1/§1.3/§1.4, confirmed identical across the
co-located LDP / BitDistiller / FracBits / SMH ledgers):

  hypothesis → `### <id>: <title>`  + `- **field:** value` bullets   (§1.1)
  question   → `- **<id>:** <title>` + `  - **field:** value` bullets (§1.3, nested)
  decision   → `#### <id>: <title>` + `- **field:** value` bullets   (§1.4)

`fields` is an ordered list of (label, value) pairs — the caller owns the field
set and content (so a harvested stub and a hand-mint can carry different fields);
this module only owns the punctuation. The house-style first bullet is the
combined schema line `kind: <kind> · **status:** <s> · **focus:** <f>`; build it
with `schema_line()` so its exact spacing is written down once too.
"""
from __future__ import annotations


def schema_line(kind: str, status: str, focus: str) -> str:
    """The combined first-bullet VALUE: `<kind> · **status:** <s> · **focus:** <f>`.
    Pair it with the label `kind` in `fields` — the parser reads all three fields
    off this one line (journal_xref `_claim_fields`)."""
    return f"{kind} · **status:** {status} · **focus:** {focus}"


def render_node(cid: str, kind: str, title: str, fields) -> str:
    """The §1 markdown block for one claim node, newline-terminated.

    `cid` is the allocated id (H9 / Q7 / D3), `kind` one of
    hypothesis/question/decision, `title` the one-line claim, `fields` the ordered
    (label, value) bullets (typically opening with a `schema_line()` under label
    `kind`). Raises ValueError on an unknown kind."""
    if kind == "hypothesis":
        lines = [f"### {cid}: {title}"] + [f"- **{k}:** {v}" for k, v in fields]
    elif kind == "decision":
        lines = [f"#### {cid}: {title}"] + [f"- **{k}:** {v}" for k, v in fields]
    elif kind == "question":
        lines = [f"- **{cid}:** {title}"] + [f"  - **{k}:** {v}" for k, v in fields]
    else:
        raise ValueError(f"render_node handles hypothesis/question/decision, not {kind!r}")
    return "\n".join(lines) + "\n"
