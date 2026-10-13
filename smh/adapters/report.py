"""SMH adapter 3 — Report figdata.

Core renders narrative → fact-gate → HTML and injects figures at `{{fig:ID}}`
placeholders. The instance supplies the figures themselves (how to build each
div) and, optionally, a section-anchor → figure-id placement map. Core never
imports the instance's figure module by name — it receives a `FigureCatalog`.

INTERFACES ONLY — no implementation this round. See
`docs/DESIGN_adapter_contracts.md` (Adapter 3) for the core/instance split, the
`Reports/` import-direction fix, and the FracBits + far-consumer validation.

LDP today satisfies this implicitly: core = `pipeline/build.py`
(`build_context`, `build`, `_render_report`/`_render_slides`, `CSS`, `FS_JS`,
`inject_figures`/`_fig_html`) + the gate `verify`; instance = `Reports/figdata.py`
(family tuples + β→bits decoders) and `Reports/interactive.py`
(`build_figures`/`figures_html`). The defect to fix in the carve is the import
direction — `pipeline/build.py` does `sys.path.insert(... "Reports")` +
`import interactive`; core should take a catalog instead.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FigureCatalog(Protocol):
    """What an instance supplies so core can inject figures. The render shell,
    CSS, fullscreen JS, `{{fig:ID}}` resolution, gate call, and both render
    targets (report + slides) stay in core."""

    def figures_html(self) -> tuple[str, dict[str, str]]:
        """Build every figure and return `(plotlyjs_script, {fig_id: div_html})`.
        Core's `inject_figures` swaps each `{{fig:<id>}}` placeholder for the
        matching div. LDP impl: `Reports/interactive.figures_html()`."""
        ...

    def placement(self) -> dict[str, str]:
        """Optional legacy map: section anchor -> fig_id, used only when the
        narrative carries no explicit `{{fig:*}}` placeholders. LDP impl: the
        hardcoded map in `inject_figures` (`build.py:118–126`). Return `{}` to opt
        out and rely solely on inline placeholders."""
        ...
