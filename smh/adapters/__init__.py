"""SMH adapter contracts — the 5 seams between core and an instance.

Design note + validation: `docs/DESIGN_adapter_contracts.md` (1–3, addendum for
4); adapter 5 is documented in `smh/adapters/config_sig.py`. Vision:
`docs/SMH_VISION.md`. An instance registers its implementation of each seam in
its `smh.toml` table (`config.catalog_ref`); an unregistered seam resolves to the
null/generic implementation here, never to another project's.

  1. Facts        — FactsCatalog / RegexFactsCatalog        (smh.adapters.facts)
  2. Runner       — Runner / ClaimGate / JobSpec ...         (smh.adapters.runner)
  3. Report       — FigureCatalog / NullFigureCatalog        (smh.adapters.report)
  4. Measurement  — MeasurementCatalog / RunLogMeasurement /
                    NullMeasurementCatalog                   (smh.adapters.measurement)
  5. Config sig   — ConfigSignatureCatalog / ConfigSignature /
                    parse_cmd / producer_chain               (smh.adapters.config_sig)
"""
from .config_sig import (UNRECOVERED, ConfigSignature, ConfigSignatureCatalog,
                         parse_cmd, producer_chain)
from .facts import FactsCatalog, RegexFactsCatalog
from .measurement import MeasurementCatalog, NullMeasurementCatalog, RunLogMeasurement
from .report import FigureCatalog, NullFigureCatalog
from .runner import Capacity, ClaimGate, Handle, JobSpec, Placement, Result, Runner, Status

__all__ = [
    "FactsCatalog",
    "RegexFactsCatalog",
    "FigureCatalog",
    "NullFigureCatalog",
    "MeasurementCatalog",
    "RunLogMeasurement",
    "NullMeasurementCatalog",
    "ConfigSignatureCatalog",
    "ConfigSignature",
    "UNRECOVERED",
    "parse_cmd",
    "producer_chain",
    "Runner",
    "ClaimGate",
    "JobSpec",
    "Result",
    "Status",
    "Capacity",
    "Placement",
    "Handle",
]
