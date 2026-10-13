"""Per-project MeasurementCatalog resolver (SMH adapter 4, consumer glue).

The honest digest (`ingest.py`) and replay (`authored_facts.py`) must measure a
finished run *in its project's terms* — a co-located ledger holds >1 project
(LDP + FracBits today), and each can decode β differently. This maps a project
name to its `MeasurementCatalog` instance, so the consumers call
`measurement_for(project).b2(tag)` instead of importing `figdata` by name (the
import-direction defect the adapter design flags).

Resolution mirrors `facts._default_catalog` / `build._default_figure_catalog`:
`smh.toml` is consulted first — a stranger's repo names its catalog there
(top-level `measurement_catalog` for the default instance, or
`[projects.<name>].measurement_catalog` for a co-located one) and it is resolved
via `smh.config.instantiate`. Absent the config (or the key), the `_REGISTRY`
below is the LDP-repo fallback — data ("project -> module:Class") resolved lazily
via importlib, so core stays generic and LDP output is byte-unchanged. A
stranger's single-project repo never needs a `_REGISTRY` entry: it lands on the
top-level `measurement_catalog` key, or the default (LDP) catalog if that too is
absent.
"""
import importlib
import os

from smh.config import resolve_root
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# project -> "module:Class" for its MeasurementCatalog. The default project
# (LDP) and every co-located namespace in THIS repo. A new co-located project
# adds one row; a separate single-project repo names its catalog in smh.toml
# (`measurement_catalog`) instead — see `_from_config`.
_REGISTRY = {
    "ldp": "ldp.measurement_catalog:LdpMeasurementCatalog",
    "fracbits": "fracbits.measurement_catalog:FracBitsMeasurementCatalog",
    "bitdistiller":
        "bitdistiller.measurement_catalog:BitDistillerMeasurementCatalog",
}
DEFAULT = "ldp"

_cache = {}


def _from_config(project, root=None):
    """The `"module:Class"` ref named in `smh.toml` for `project`, or None.

    `project=None` -> the top-level `measurement_catalog` key (the default
    instance); a named project -> its `[projects.<name>].measurement_catalog`.
    None (config absent, or the key omitted) sends the caller to `_REGISTRY`."""
    from smh import config
    root = root or ROOT
    if project is None:
        cfg = config.load_config(root)
        return cfg.get("measurement_catalog") if cfg else None
    pc = config.project_config(root, project)
    return pc.get("measurement_catalog") if pc else None


def measurement_for(project=None):
    """The `MeasurementCatalog` for `project` (default = LDP). `smh.toml` wins;
    else the `_REGISTRY` fallback; an unknown project resolves to the default
    catalog so a single-project repo Just Works."""
    ref = _from_config(project)
    key = ref or (project if project in _REGISTRY else DEFAULT)
    if key not in _cache:
        spec = ref or _REGISTRY[key]
        mod, cls = spec.split(":")
        _cache[key] = getattr(importlib.import_module(mod), cls)()
    return _cache[key]
