"""SMH adapter 4 — Measurement / evidence extraction.

The un-adaptered seam surfaced by the authored-facts migration
(`docs/DESIGN_adapter_contracts.md`, Addendum 2026-06-25). Distinct from the
Facts adapter (prose -> numbers) and the Figure adapter (numbers -> figure divs):
this one is **run tag -> a measured scalar** (final eval_loss, realized effective
bits², or any instance-defined leaf). Three consumers share it —
`pipeline/ingest.py` (the honest digest), `pipeline/authored_facts.py` (replay
leaves) and `repro_check` (via authored_facts) — so naming it once stops each
reaching into `Reports/figdata.py` by name (the import-direction defect the
design flags).

Core supplies the Protocol + a tiny run-log convenience base. The INSTANCE
supplies the only domain-specific piece: how a run's β trajectory decodes to
effective bits (LDP's family router vs FracBits' fixed fp ladder). FracBits is
the forcing function — its conclusions are interpolation-gap numbers, not
`eval_loss`/`b²`, so it needs its own leaf ops; the seam lets it diverge without
forking the digest.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional, Protocol, runtime_checkable

# FINAL eval_loss as written to a completed run's log — the one genuinely
# domain-agnostic leaf (a regex over a text file, no decoder, no model shape).
_FINAL_RE = re.compile(r"FINAL .*eval_loss=([\d.]+)")


@runtime_checkable
class MeasurementCatalog(Protocol):
    """What an instance must supply so core can turn a finished run into numbers.

    Thin by design: it says nothing about *how* a value is measured (a log line,
    a β decoder, a saved metric). `value(tag, kind)` is the generic entry the
    replay engine calls; `eval`/`b2` are the two leaves every quant project has,
    named for convenience. A non-quant consumer implements `value` for its own
    kinds and lets `eval`/`b2` return None.
    """

    def eval(self, tag: str) -> Optional[float]:
        """Final eval_loss of a completed run, or None if not finished/synced."""
        ...

    def b2(self, tag: str) -> Optional[float]:
        """Realized effective-bits² (the run's deployed budget), or None."""
        ...

    def value(self, tag: str, kind: str) -> Optional[float]:
        """The leaf named `kind` for `tag` — the generic entry replay leaves call.
        `kind` in {"EVAL","B2"} for quant projects; other strings are
        instance-defined (e.g. FracBits' interpolation-gap leaves)."""
        ...

    # ── OPTIONAL surface (declared defaults; consumers `getattr` these) ───────
    # The three methods above are the required contract. The consumers below
    # also read these class attributes, each gated on its presence/truthiness so
    # an `eval`/`b²`-shaped catalog (LDP, FracBits) leaves every one at its
    # default and the pipelines stay inert on it. A catalog subclasses this
    # Protocol explicitly (`class X(MeasurementCatalog)`) and overrides what it
    # needs; the defaults below ARE the "not supplied" value, so a consumer never
    # needs `getattr(cat, NAME, default)` — plain attribute access is the contract.
    #
    #   LEAF_KINDS      ingest._leaf_values / propose._catalog_kinds — the
    #                   reportable non-eval_loss leaf kinds (`value(tag, k)` for
    #                   each). Empty → the digest reports `eval`/`b²` only.
    #   LINT_KINDS      metric_lint.build_pool — the value BASES the lint pools
    #                   (canonical leaves + legacy/aux bases that must never be
    #                   reportable). Empty → LEAF_KINDS.
    #   METRIC_LEXICON  metric_lint.metric_check — (label-regex → kinds) table;
    #                   empty DISABLES metric lint for the catalog.
    #   CONFUSABLE      metric_lint — groups of bases a labeled number is checked
    #                   against for cross-basis confusion.
    #   TAG_PREFIX      metric_lint.extract_tags — the run-tag prefix that marks a
    #                   token as this project's tag (None → any `results_dir` hit).
    #   results_dir     metric_lint.extract_tags — where `<tag>/` result dirs live
    #                   ("" → tag existence is not checked on disk).
    #   lint_value(tag, kind)   optional METHOD — resolve a LINT_KINDS base (a
    #                   superset of `value`); absent → `value` is used.
    LEAF_KINDS: tuple = ()
    LINT_KINDS: tuple = ()
    METRIC_LEXICON: tuple = ()
    CONFUSABLE: tuple = ()
    TAG_PREFIX: Optional[str] = None
    results_dir: str = ""


class NullMeasurementCatalog(MeasurementCatalog):
    """The catalog a project that registers no `measurement_catalog` gets (the
    `smh` meta-instance; a scaffold before its first run; an unknown project id).
    Every leaf is None and it advertises no kinds, so the digest / replay /
    lint pipelines are inert on it — honestly "nothing measurable here", never
    another project's numbers (the 2026-11 audit found `measurement_for('smh')`
    returning LDP's catalog)."""

    LEAF_KINDS: tuple = ()

    def eval(self, tag: str) -> Optional[float]:
        return None

    def b2(self, tag: str) -> Optional[float]:
        return None

    def value(self, tag: str, kind: str) -> Optional[float]:
        return None


class RunLogMeasurement(MeasurementCatalog):
    """Core convenience: a `MeasurementCatalog` whose `eval` is the generic
    FINAL-line parse and whose `b2` is an **instance-supplied callable** (the one
    domain-specific piece — it owns the β->bits decoder and the MAC weighting).
    LDP and FracBits each construct one with their own `b2_fn`; a far consumer
    implements `MeasurementCatalog` directly and ignores this.

    Core imports nothing instance-specific: the figdata β decoders live entirely
    inside the `b2_fn` the instance passes in.
    """

    def __init__(
        self,
        b2_fn: Callable[[str], Optional[float]],
        runs_dir: str,
        eval_fn: Optional[Callable[[str], Optional[float]]] = None,
        extra_kinds: Optional[dict[str, Callable[[str], Optional[float]]]] = None,
    ) -> None:
        self._b2_fn = b2_fn
        self._runs_dir = runs_dir
        self._eval_fn = eval_fn          # None -> the generic FINAL parse below
        # instance-defined extra kinds beyond EVAL/B2 (FracBits' gap leaves);
        # `extra_kinds` (property) is the public enumeration propose.py reads.
        self._extra = dict(extra_kinds or {})

    @property
    def extra_kinds(self) -> tuple:
        """The instance-defined kinds beyond EVAL/B2 that `value` resolves."""
        return tuple(self._extra)

    def eval(self, tag: str) -> Optional[float]:
        if self._eval_fn is not None:
            return self._eval_fn(tag)
        f = os.path.join(self._runs_dir, f"{tag}.log")
        if not os.path.exists(f):
            return None
        m = _FINAL_RE.search(open(f, errors="replace").read())
        return float(m.group(1)) if m else None

    def b2(self, tag: str) -> Optional[float]:
        return self._b2_fn(tag)

    def value(self, tag: str, kind: str) -> Optional[float]:
        if kind == "EVAL":
            return self.eval(tag)
        if kind == "B2":
            return self.b2(tag)
        if kind in self._extra:
            return self._extra[kind](tag)
        raise KeyError(
            f"measurement kind {kind!r} not defined by this catalog "
            f"(have: EVAL, B2{''.join(', ' + k for k in self._extra)})")
