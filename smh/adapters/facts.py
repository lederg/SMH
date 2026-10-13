"""SMH adapter 1 — Facts.

Core turns a project's journal into (a) the *bag of numbers* the gate enforces
and (b) a *named-fact catalog* the report prompt cites. The instance supplies
which files are the ledger and which named facts to expose; core supplies every
operation over them.

INTERFACES ONLY — no implementation this round. See
`docs/DESIGN_adapter_contracts.md` (Adapter 1) for the core/instance split and
the FracBits + far-consumer validation.

LDP today satisfies this implicitly: `pipeline/facts.py` (the `_FACTS` dict + the
`JOURNAL.md`/`EVIDENCE.md` two-file load via `journal_xref`). In the carve the
machinery (`NUM_RE`, `to_decimal`, `number_matches`, `journal_numbers`, the
`structured_facts` loop) becomes core; `_FACTS` and the file list become an
LDP-side `RegexFactsCatalog`.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Protocol, runtime_checkable


@runtime_checkable
class FactsCatalog(Protocol):
    """What an instance must supply for the gate + report-prompt to work.

    Thin by design: it says nothing about *how* facts are extracted (regex, CSV,
    a database). The LDP/FracBits regex path is a core-provided convenience
    (`RegexFactsCatalog`), not part of this contract — so a non-prose consumer
    can implement these two methods directly.
    """

    def journal_text(self) -> str:
        """The ground-truth ledger, concatenated to a single string. Every number
        a generated narrative emits must trace to a number in here (the gate's
        allow-list is derived from this via core `journal_numbers`)."""
        ...

    def named_facts(self) -> dict[str, Decimal | None]:
        """Stable key -> live value, re-resolved from the ledger each call so keys
        are durable but values never go stale. A key whose source prose has
        drifted resolves to None (the build warns; it does not fabricate)."""
        ...


class RegexFactsCatalog(FactsCatalog):
    """Core-provided convenience: a `FactsCatalog` backed by a
    `{key: (regex, group, cast)}` map over a list of ledger files. LDP and
    FracBits subclass this with their own map + files. Far (non-prose) consumers
    implement `FactsCatalog` directly and ignore this.

    The regex loop and the ledger-concatenation are pure machinery — domain
    agnostic. What's instance-specific (the map + which files are the ledger) is
    constructor data, supplied by the subclass.
    """

    def __init__(
        self,
        ledger_paths: list[str],
        facts: dict[str, tuple[str, int, type]],
    ) -> None:
        self.ledger_paths = list(ledger_paths)
        self.facts = dict(facts)

    def journal_text(self) -> str:
        """The ledger files concatenated in order, blank-line-free `\\n` join —
        the single contract point a project's readers share (cf. LDP's old
        `journal_xref.load_journal`)."""
        return "\n".join(
            open(p, encoding="utf-8").read() for p in self.ledger_paths)

    def resolve(self, text: str) -> dict[str, Decimal | None]:
        """Run the `{key: (regex, group, cast)}` map over arbitrary `text`. Each
        key resolves to its live captured value, or None if the regex no longer
        matches (prose drifted). Keys are durable; values are never cached."""
        out: dict[str, Decimal | None] = {}
        for key, (pat, grp, cast) in self.facts.items():
            m = re.search(pat, text, re.DOTALL)
            out[key] = cast(m.group(grp).rstrip(".")) if m else None
        return out

    def named_facts(self) -> dict[str, Decimal | None]:
        return self.resolve(self.journal_text())
