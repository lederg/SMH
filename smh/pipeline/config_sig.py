"""Per-project ConfigSignatureCatalog resolver + tag->job lookup (adapter 5 glue).

Mirrors `pipeline/measure.py`: maps a project name to its
`ConfigSignatureCatalog`, so ingest / the gate / repro_check derive a result's
config *in its project's terms* (bitdistiller's `fp4/pt` vs `fp4/g64` means
nothing to LDP). resolved via `config.catalog_ref(..., "config_signature_catalog", project)`
(`[projects.<name>]` in `smh.toml`); a project with no catalog registered
lands on `GenericConfigSignature` (renders whatever knobs the cmd carries, no
invalid registry) so a stranger's repo Just Works and LDP/FracBits get a
best-effort signature without a hand-authored knob list.

Also owns `signature_for_tag(tag, project)`: the RE row's Source column names a
run TAG, but the config lives on the scheduler JOB behind that tag AND on the
jobs it depends on (`deps` — the train / block-AP arm whose checkpoint it
scored). This finds the job record (done/ then failed/), walks its producer
chain, and returns the catalog's fold over both, or UNRECOVERED when the job
JSON was GC'd.
"""
import glob
import inspect
import itertools
import json
import os
import re

from smh.config import resolve_root
from smh.adapters.config_sig import (ConfigSignature, ConfigSignatureCatalog,
                                     UNRECOVERED, job_code, parse_cmd,
                                     producer_chain)

ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DONE = os.path.join(ROOT, "scheduler", "done")
FAILED = os.path.join(ROOT, "scheduler", "failed")

# No in-code project→class table: a project with load-bearing knobs / a collapse
# registry registers `config_signature_catalog` in its `smh.toml` table;
# everything else falls to GenericConfigSignature.
_GENERIC = "__generic__"

_cache = {}
_tag_index = None


class GenericConfigSignature(ConfigSignatureCatalog):
    """Instance-agnostic fallback: render every parsed knob as `k=v`, no invalid
    registry. Honest but unopinionated — a project with real load-bearing knobs
    should register its own catalog so the canonical string stays compact and the
    collapse set is enforced."""

    #: knobs too noisy to headline (paths, model ids, shell scaffolding)
    _SKIP = {"model", "out_json", "eval_ppl"}

    def signature(self, job: dict, chain=()) -> ConfigSignature:
        # the producer chain is ignored on purpose: without a knob list there is
        # no way to tell which upstream knobs persist into the artifact.
        knobs = parse_cmd(job.get("cmd") or "")
        if not knobs:
            return ConfigSignature(**{**UNRECOVERED.__dict__, "source": job.get("id"),
                                      "code": job_code(job)})
        shown = {k: v for k, v in sorted(knobs.items())
                 if k not in self._SKIP and v not in ("", None)}
        string = " ".join(f"{k}={v}" for k, v in list(shown.items())[:6]) or "config: (none)"
        return ConfigSignature(string=string, knobs=knobs, source=job.get("id"),
                               raw=dict(knobs), code=job_code(job))


def config_sig_for(project=None):
    """The `ConfigSignatureCatalog` for `project` (None = the repo-default
    project): the `config_signature_catalog` registered in its `smh.toml` table,
    else the generic fallback."""
    from smh import config
    ref = config.catalog_ref(ROOT, "config_signature_catalog", project)
    key = ref or _GENERIC
    if key not in _cache:
        _cache[key] = config.instantiate(ref) if ref else GenericConfigSignature()
    return _cache[key]


def _job_for_tag(tag):
    """The scheduler job record whose `tag` matches, done/ preferred over failed/
    (a completed run is the authoritative config; a failure is a fallback so a
    collapse/OOM arm still resolves). Lazily indexes both dirs once."""
    global _tag_index
    if _tag_index is None:
        _tag_index = {}
        for d in (FAILED, DONE):          # DONE second -> wins on collision
            for f in glob.glob(os.path.join(d, "*.json")):
                try:
                    j = json.load(open(f))
                except (ValueError, OSError):
                    continue
                t = j.get("tag")
                if t:
                    _tag_index[t] = j
    return _tag_index.get(tag)


def producers_for(job):
    """The job's producer chain (`adapters.config_sig.producer_chain`) resolved
    through the done/failed tag index — nearest producer first."""
    _job_for_tag("")                      # force the tag index to build
    return producer_chain(job, _job_for_tag)


def _takes_chain(cat):
    """Whether `cat.signature` accepts the `chain` argument (an instance written
    against the pre-chain Protocol may not) — checked once per catalog class."""
    cls = type(cat)
    if cls not in _takes_chain_cache:
        try:
            params = inspect.signature(cat.signature).parameters
            _takes_chain_cache[cls] = "chain" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            _takes_chain_cache[cls] = False
    return _takes_chain_cache[cls]


_takes_chain_cache = {}


def signature_of(job, project=None):
    """Config signature of a scheduler job record, folded over its producer
    chain by the project's catalog."""
    cat = config_sig_for(project)
    if _takes_chain(cat):
        return cat.signature(job, chain=producers_for(job))
    return cat.signature(job)


def signature_for_tag(tag, project=None):
    """Config signature for a run TAG (as it appears in an RE row's Source),
    resolved via its scheduler job record AND the producer chain behind it
    (`deps`). UNRECOVERED when no job carries the tag (GC'd / never scheduled)."""
    job = _job_for_tag(tag)
    if job is None:
        return ConfigSignature(**{**UNRECOVERED.__dict__, "source": f"tag:{tag}"})
    return signature_of(job, project)


def claim_for_tag(tag):
    """The journal claim (`goal.claim`) the run TAG was submitted under, or None."""
    job = _job_for_tag(tag)
    return (job.get("goal") or {}).get("claim") if job else None


def sigs_for_claim(claim):
    """Signature strings of EVERY run submitted under `claim` — the claim-wide set
    the fact gate checks a draft against. Rendered with the claim's own project so
    a cross-project link resolves to the same string the gate saw. repro_check
    Part C uses this to accept a disclosure token: a sig the gate forced onto a
    row for a superseded/invalid run the row itself never re-ran."""
    _job_for_tag("")                      # force the tag index to build
    proj = claim.split(":")[0] if ":" in claim else None
    out = set()
    for t, j in _tag_index.items():
        if (j.get("goal") or {}).get("claim") == claim:
            out.add(signature_for_tag(t, proj).string)
    return out


def reset_cache():
    """Test hook: drop the catalog + tag index caches."""
    global _tag_index
    _cache.clear()
    _tag_index = None


# ── committed-journal helpers (repro_check Part C) ─────────────────────────────
# RE-row Source cells compress many runs with shell brace globs
# (`..._q12_{wikialpaca,cosmopedia}{,_s1,_s2}`). Expand those to concrete tags,
# keep only the ones a real job record carries, and resolve each to a signature —
# so an out-of-loop check can re-derive a committed row's configs from its runs.
_CFG_TOKEN_RE = re.compile(r"\*\*\s*⚙\s*(.+?)\s*\*\*")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
# a Source path pattern chunk: keeps `{ } ,` so brace-expansion runs BEFORE
# tokenizing (splitting first would strip the braces and lose the glob).
_CHUNK_RE = re.compile(r"[A-Za-z0-9][\w./{},-]*")


def expand_braces(pattern):
    """Expand shell-style `{a,b}{,_s1}` products in one Source token into concrete
    strings (bounded — a runaway pattern is capped so a pathological cell can't
    blow up). No brace -> [pattern]."""
    m = _BRACE_RE.search(pattern)
    if not m:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    opts = m.group(1).split(",")
    out = []
    for opt in opts:
        for rest in expand_braces(head + opt + tail):
            out.append(rest)
            if len(out) > 4096:
                return out
    return out


def tags_in_source(source_cell):
    """Concrete run tags referenced by an RE row's Source cell that a real job
    record carries. Each path chunk is brace-expanded FIRST (`.../q12_{a,b}/...`
    -> `.../q12_a/...`, `.../q12_b/...`), then every `/`-segment of each expansion
    is tested against the job index, so a tag buried under `runs/<tag>/lm_eval.json`
    still resolves."""
    _job_for_tag("")                      # force the tag index to build
    found = set()
    for chunk in _CHUNK_RE.findall(source_cell or ""):
        if "{" not in chunk and "/" not in chunk:
            if chunk in _tag_index:
                found.add(chunk)
            continue
        for exp in expand_braces(chunk):
            for seg in exp.split("/"):
                if seg in _tag_index:
                    found.add(seg)
    return sorted(found)


def config_tokens(text):
    """The `**⚙ <sig>**` config tokens written in a chunk of journal prose."""
    return [t.strip() for t in _CFG_TOKEN_RE.findall(text or "")]


# ── variance-aware surfacing (a swept knob becomes load-bearing) ───────────────
# A curated signature string hides a knob outside the load-bearing set — so an LR
# sweep would render every arm identically. This promotes any knob that VARIES
# across a result-SET into each arm's string, so the swept axis is visible and the
# arms are distinguishable. `raw` (the full cmd) is the diff source. A knob whose
# variation is EXPECTED noise (a reseed, an output path) is suppressed, so n=3
# seeds don't clutter but a real recipe sweep surfaces.
_NOISE_KNOBS = {
    "seed", "s", "tag", "out", "out_json", "outdir", "output", "logfile", "log",
    "eval_ppl",
    # quant knobs ALREADY rendered in the compact signature string — their
    # variation is visible via the string token (`fp4/pt` vs `fp4/g64`, `+Nfp8`),
    # so re-appending them as `k=v` is redundant clutter, not new signal.
    "quant_type", "act_quant", "act_quant_type", "act_quant_sites", "group_size",
    "q_group_size", "couple_w_to_a", "bits", "wbit",
    # HF-internal, per-arm scaffolding that varies but isn't config.
    "deepspeed_plugin", "hf_deepspeed_config", "accelerator_config", "output_dir",
    "run_name", "logging_dir", "hub_model_id", "resume_from_checkpoint",
    "push_to_hub_model_id", "distributed_state",
}
# knob-name suffixes that mark run scaffolding (paths, ids, nested configs).
_NOISE_SUFFIXES = ("_dir", "_path", "_name", "_id", "_config", "_plugin")


def _is_noise(key, values):
    if key in _NOISE_KNOBS or key.endswith(_NOISE_SUFFIXES):
        return True
    # object-valued (a nested config dict/list) or path-valued (a value with '/')
    # knobs are scaffolding, never a headline config axis.
    if any(isinstance(v, (dict, list)) for v in values):
        return True
    return any(isinstance(v, str) and "/" in v for v in values)


def swept_knobs(raws, noise=None):
    """Keys whose value differs across the `raws` dicts of a result-set (a knob
    present in some and absent in others counts as varying), minus noise. Returns
    a sorted list — the SWEPT axes."""
    raws = [r or {} for r in raws]
    if len(raws) < 2:
        return []
    keys = set().union(*raws)
    out = []
    for k in keys:
        vals = [r.get(k) for r in raws]
        if len({repr(v) for v in vals}) > 1 and not _is_noise(k, vals):
            out.append(k)
    return sorted(out)


def augment_with_sweeps(rows, get_raw=lambda r: r.get("config_raw"),
                        get_sig=lambda r: r.get("config_sig"),
                        set_sig=None):
    """Append each swept knob (`k=v`) to every row's config signature in a
    result-set, so a swept axis is never hidden by the curated string. Mutates via
    `set_sig(row, new_string)` (defaults to `row['config_sig'] = ...`). Returns the
    list of swept knobs (≥2 non-noise axes = a confounded ladder — the Q4 hook)."""
    if set_sig is None:
        def set_sig(r, s):
            r["config_sig"] = s
    swept = swept_knobs([get_raw(r) for r in rows])
    if not swept:
        return swept
    for r in rows:
        raw = get_raw(r) or {}
        extra = " ".join(f"{k}={raw.get(k, '—')}" for k in swept
                         if str(get_sig(r) or "").find(f"{k}=") < 0)
        if extra:
            set_sig(r, f"{get_sig(r)} {extra}".strip())
    return swept
