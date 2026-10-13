# Presentation pipeline — journal facts → story-prompt → gated presentation

Implements the design in `HANDOFF_report_pipeline.md`: **one truth, many stories.**
`RESEARCH_JOURNAL.md` is the single source of truth for *facts*; every
presentation is a derivative = a **story-spec prompt** × a **format**, generated
by `LLM(story-prompt + journal facts)`, **fact-checked against the journal before
it ships**.

```
RESEARCH_JOURNAL.md ──facts.py──► structured facts ─┐
                                                    ├─► LLM ─► narrative.md ─► gate.py ─► build.py ─► HTML
presentations/<name>/v<N>.prompt.md ────────────────┘                         (HARD GATE)   (+ figures)
```

## Files

| file | role |
|---|---|
| `facts.py` | journal → `journal_numbers()` (the allow-list) + `structured_facts()` (stable-keyed headline facts, values live-extracted) — **Layer B part 1** |
| `gate.py` | verify every number in a narrative against the journal; fail-closed on any unsourced number — **Layer B part 2, the HARD GATE** |
| `build.py` | story-prompt + facts → narrative → gate → self-contained HTML with figures — **Layer C build runner** |
| `../Reports/figdata.py` | ONE data source for every figure (static PNG + interactive) — static and interactive can't diverge |
| `../Reports/interactive.py` | Plotly figures (zoom/pan/box-select), `plotly.min.js` inlined (no CDN) |
| `../presentations/<name>/v<N>.prompt.md` | the named, versioned, forkable story-spec |

## Run

```bash
python pipeline/build.py                       # build technical-report v1 (reproduces report_v2)
python pipeline/build.py --narrative draft.md  # gate + render a freshly-generated narrative
python pipeline/gate.py Reports/report_v2_draft.md   # gate a file standalone
python pipeline/facts.py                        # dump structured facts
scripts/reports-serve.sh up                     # publish runs/technical-report.html over the tailnet
```

All scripts run under `/opt/conda/bin/python` (has markdown, matplotlib, plotly).

## The LLM seam (how `LLM(prompt+facts) → narrative` works today)

`build.py` writes the LLM input to `presentations/<name>/build_context.md`
(prompt + injected facts) so the generation step is reproducible. For Milestone 1
the narrative defaults to `Reports/report_v2_draft.md` — the v1 baseline the
prompt was distilled from (the degenerate "use exactly this prose" case). To
regenerate from the prompt, hand `build.py` a fresh narrative via `--narrative`;
the assistant (or a scripted API call) is the LLM that produces it from
`build_context.md`. The gate makes that safe regardless of who writes the prose.

## Status (Milestone 1 — done)

- ✅ story-prompt reverse-engineered → `presentations/technical-report/v1.prompt.md`
- ✅ fact gate (caught 3 report-only numbers → migrated to journal as RE-16)
- ✅ interactive Plotly Pareto / §3 / §5 (inline, zoomable)
- ✅ build runner reproduces `report_v2_draft.md` through the pipeline, gate PASS
- serve: report placed in `runs/`; `reports-serve.sh up` needs one-time tailscale auth (user)

## Next (HANDOFF §4 steps 6–7 — not yet built)

- **Distillation runner** — finished session + approved output → compact
  `v<N+1>.prompt.md` (distill *taste*, the non-obvious corrections; not the
  transcript). The payoff feature: "condense 2 h of tweaking into a reusable prompt."
- **Second format** (e.g. reveal.js slides) against the same story-prompt + facts,
  to prove formats are pluggable.
- Grow `facts.py` from a number-consistency check toward full structured
  injection (more stable keys), and have the gate flag *missing* facts too.
