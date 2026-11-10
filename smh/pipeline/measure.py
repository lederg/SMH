"""Per-project MeasurementCatalog resolver (SMH adapter 4, consumer glue).

The honest digest (`ingest.py`) and replay (`authored_facts.py`) must measure a
finished run *in its project's terms* — a co-located ledger holds >1 project,
and each can decode β differently. This maps a project name to its
`MeasurementCatalog` instance, so the consumers call
`measurement_for(project).b2(tag)` instead of importing an instance module.

Resolution = `config.catalog_ref(ROOT, "measurement_catalog", project)`: the
project's `[projects.<name>]` table in `smh.toml` (or a solo repo's top-level
key for its default project), instantiated via `smh.config.instantiate`.
`project=None` is the repo-default project. A project that registers no catalog
— the `smh` meta-instance, a fresh scaffold, an unknown id — gets
`NullMeasurementCatalog` (every leaf None, no kinds), NEVER another project's
numbers. There is no in-code project→class table (it drifted; 2026-11-02).
"""
import os

from smh.config import resolve_root
from smh.adapters.measurement import NullMeasurementCatalog

ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_NULL = "__null__"
_cache = {}


def measurement_for(project=None):
    """The `MeasurementCatalog` for `project` (None = the repo-default project).
    Registered in `smh.toml` → that class (one instance per ref, cached); not
    registered → the shared `NullMeasurementCatalog`."""
    from smh import config
    ref = config.catalog_ref(ROOT, "measurement_catalog", project)
    key = ref or _NULL
    if key not in _cache:
        _cache[key] = config.instantiate(ref) if ref else NullMeasurementCatalog()
    return _cache[key]


def reset_cache():
    """Test hook: drop the per-ref instance cache."""
    _cache.clear()
