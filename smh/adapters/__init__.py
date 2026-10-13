"""SMH adapter contracts — the 3 seams that make LDP the first consumer of `smh/`.

INTERFACES ONLY this round (no carve yet). Design note + validation:
`docs/DESIGN_adapter_contracts.md`. Vision: `docs/SMH_VISION.md`.

  1. Facts   — FactsCatalog / RegexFactsCatalog   (smh.adapters.facts)
  2. Runner  — Runner / ClaimGate / JobSpec ...    (smh.adapters.runner)
  3. Report  — FigureCatalog                        (smh.adapters.report)
"""
from .facts import FactsCatalog, RegexFactsCatalog
from .report import FigureCatalog
from .runner import Capacity, ClaimGate, Handle, JobSpec, Placement, Result, Runner, Status

__all__ = [
    "FactsCatalog",
    "RegexFactsCatalog",
    "FigureCatalog",
    "Runner",
    "ClaimGate",
    "JobSpec",
    "Result",
    "Status",
    "Capacity",
    "Placement",
    "Handle",
]
