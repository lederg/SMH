"""SMH adapter 5 — Config signature / result provenance.

The seam that stops an **invalid-configuration result from winning a headline**
(exemplar: bitdistiller:Q12 / RE-21, where a per-token-fp4 *collapse* arm was
mislabeled "uniform W4A4" because the config lived only in the scheduler job's
`--cmd`, never on the result). Distinct from the Measurement adapter (run tag ->
a measured *scalar*): this one is **a job record -> its authoritative
CONFIGURATION**, rendered as a compact canonical string that makes the
load-bearing distinction (per-token vs g64 activations) impossible to hide.

Core supplies:
  * `parse_cmd` — the one genuinely domain-agnostic piece: a scheduler job's
    `cmd` string carries the real config as a mix of `--flag value`,
    `--flag=value` and `ENV=value` tokens; this flattens all three into a dict.
  * the `ConfigSignatureCatalog` Protocol + a `ConfigSignature` record.
The INSTANCE supplies the only domain-specific piece: WHICH parsed knobs are
load-bearing, how they render to a canonical string, and which knob combinations
are known-invalid / known-collapse (a per-project registry). bitdistiller's
`fp4/pt` (collapse) vs `fp4/g64` (deployable) is the forcing function; LDP and
FracBits name their own knobs without forking this.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# `--flag=value`, `--flag value` (value is the next non-flag token), and bare
# `ENV=value` shell assignments. Train jobs pass quant config as `ENV=val`
# (`ACT_QUANT=fp4-act`); eval jobs pass it as `--flag val`
# (`--act_quant_type fp4-g64-act`) — both are the SAME knob, so both forms must
# flatten to the same key. Keys are lower-cased and hyphens/underscores unified
# so `--act_quant_type`, `ACT_QUANT` and `act-quant` collide onto one entry.
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _key(tok: str) -> str:
    """Canonical knob key: strip leading dashes, lower-case, unify separators."""
    return tok.lstrip("-").lower().replace("-", "_")


def parse_cmd(cmd: str) -> dict:
    """Flatten a scheduler job `cmd` string into `{knob: value}` (last wins).

    Domain-agnostic: recognizes `--flag=value`, `--flag value` and `ENV=value`.
    A `--flag` with no following value (a bare switch) maps to `""` (present).
    Shell operators (`&&`, `|`, `(`, redirects) and non-assignment bare words
    are ignored. Never raises on a malformed cmd — best-effort by design, since
    it runs over historical job records of varying vintage."""
    out: dict = {}
    if not cmd:
        return out
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("--"):
            if "=" in t:
                k, v = t[2:].split("=", 1)
                out[_key(k)] = v
            else:
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                if nxt is not None and not nxt.startswith("-") and "=" not in nxt \
                        and nxt not in ("&&", "||", "|", "(", ")"):
                    out[_key(t)] = nxt
                    i += 1
                else:
                    out[_key(t)] = ""      # bare switch
        else:
            m = _ENV_ASSIGN.match(t)
            if m:
                out[_key(m.group(1))] = m.group(2)
        i += 1
    return out


@dataclass
class ConfigSignature:
    """A result's authoritative configuration, machine-derived from its job.

    `string` is the prominent human token stamped at the head of an RE row's
    Description (`**⚙ W4A4 fp4/g64 coupled**`); `knobs` is the structured set
    that repro_check anchors and re-derives to catch drift; `invalid` is a
    non-None reason iff the config matches the instance's known-bad/collapse
    registry (a ⚠ flag — never blocks recording, but blocks a headline and warns
    every reader). `source` names where it came from (job id / "unrecovered")."""
    string: str
    knobs: dict = field(default_factory=dict)
    invalid: Optional[str] = None
    source: Optional[str] = None
    #: the COMPLETE flattened cmd — every `--flag`/`ENV=` token. `knobs` is the
    #: curated load-bearing subset (the headline); `raw` keeps the rest so a knob
    #: outside the subset is never truly lost, and a variance pass can surface a
    #: SWEPT knob (an LR sweep) that the curated string would otherwise hide.
    raw: dict = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return self.invalid is not None


# A result whose job record was garbage-collected (ckpt/log GC) can't have its
# config recovered — an HONEST marker beats a silent blank (a blank reads as
# "no special config", the exact failure this adapter exists to prevent).
UNRECOVERED = ConfigSignature(string="config: unrecovered", knobs={},
                              source="unrecovered")


@runtime_checkable
class ConfigSignatureCatalog(Protocol):
    """What an instance supplies so core can turn a finished job into its config
    signature. Thin: it says nothing about HOW the job stored config — the
    instance calls `parse_cmd(job['cmd'])` (or reads its own config field) and
    decides which knobs are load-bearing, how they render, and which are invalid.
    """

    def signature(self, job: dict) -> ConfigSignature:
        """The config signature for a finished scheduler job record. When the
        job lacks a usable config (GC'd / never had one), return
        `config_sig.UNRECOVERED` rather than raising."""
        ...
