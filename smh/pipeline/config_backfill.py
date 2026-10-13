"""Best-effort config backfill for existing RE rows (adapter-5, Q5 = forward +
best-effort backfill).

For every committed RE row, re-derive the config signature(s) of its runs from
the scheduler job records and PROPOSE a `**⚙ <config>**` prefix for the row's
Description — so a reader (and repro_check Part C) can see each result's config.

DRY-RUN by design: this NEVER writes the science ledger. RE rows live in the
EVIDENCE ledger (ground truth, human-committed); auto-editing them — especially a
MULTI-config aggregate row like RE-21 where WHICH config is the headline is a
judgment call — is exactly the irreversible move to leave to a human. So this
prints, per owed row:
  * the distinct configs its runs ran (⚠ marks known-invalid/collapse),
  * a suggested single-config prefix when unambiguous,
  * a 'MULTI-CONFIG — author must place per headline' note otherwise,
  * rows whose runs were GC'd -> `config: unrecovered` (honest, not blank).
Apply by hand (or via the normal round/gate flow), then repro_check Part C
enforces drift/undisclosed on the now-tokened row.

    python -m smh.pipeline.config_backfill              # all projects
    python -m smh.pipeline.config_backfill bitdistiller # one project
"""
import os
import re
import sys

from smh.config import resolve_root
from smh.pipeline import config_sig as cs
from smh.pipeline import repro_check as rc

ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_RE_ROW = re.compile(r"^\|\s*(RE-\d+[a-z]?)\s*\|")


def _row_cells(line):
    cells = [c.strip() for c in line.replace(r"\|", "\x00").split("|")]
    desc = cells[4].replace("\x00", "|") if len(cells) > 4 else ""
    source = cells[5].replace("\x00", "|") if len(cells) > 5 else ""
    return desc, source


def scan(project_filter=None):
    """Per RE row: (project, reid, has_token, configs[], suggestion, note).
    configs = [(sig, invalid_reason_or_None, n_runs)]."""
    cs.reset_cache()
    out = []
    for project, ev in rc._evidence_files(ROOT):
        if project_filter and project != project_filter:
            continue
        for line in open(ev, errors="replace"):
            m = _RE_ROW.match(line)
            if not m:
                continue
            reid = m.group(1)
            desc, source = _row_cells(line)
            has_token = bool(cs.config_tokens(desc))
            tags = cs.tags_in_source(source)
            agg = {}
            for t in tags:
                s = cs.signature_for_tag(t, project)
                e = agg.setdefault(s.string, [s.string, s.invalid, 0])
                e[2] += 1
            configs = sorted(agg.values(), key=lambda e: (-e[2], e[0]))
            if not tags:
                sugg, note = None, "no run resolves (GC'd) -> `config: unrecovered`"
            elif len(configs) == 1:
                sugg = f"**⚙ {configs[0][0]}**"
                note = "single config" + (" ⚠INVALID" if configs[0][1] else "")
            else:
                sugg = None
                note = "MULTI-CONFIG — author must place per headline" + \
                       (" (⚠ includes an INVALID/collapse config — MUST disclose)"
                        if any(c[1] for c in configs) else "")
            out.append((project, reid, has_token, configs, sugg, note))
    return out


def report(project_filter=None):
    rows = scan(project_filter)
    owed = [r for r in rows if not r[2]]        # rows without a ⚙ token yet
    L = [f"CONFIG BACKFILL (dry-run — writes nothing) — {len(rows)} RE rows, "
         f"{len(owed)} owe a config token", ""]
    for project, reid, has_token, configs, sugg, note in rows:
        if has_token:
            continue
        L.append(f"{project}:{reid}  — {note}")
        for sig, inv, n in configs:
            flag = "  ⚠ " + inv if inv else ""
            L.append(f"    · {sig}   (n={n}){flag}")
        if sugg:
            L.append(f"    suggest prefix: {sugg}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    print(report(sys.argv[1] if len(sys.argv) > 1 else None))
