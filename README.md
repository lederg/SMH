# SMH — the Scientific Method Harness

A lightweight harness that keeps a research project *honest*: your hypotheses,
open questions, decisions and measured results live in plain-markdown ledgers,
a **gate** refuses claims that outrun their evidence, and a **graph** shows how
everything connects. You talk through your research with Claude Code; the
harness turns that into a disciplined, queryable record.

This README is for a **new user** starting their own project. (The LDP_Journal
repo is the reference instance — you don't need it.)

> Status: early release, packaged for a first outside user. Everything below
> runs from the installed package against your own project dir (resolved from
> the cwd, or `SMH_ROOT`). The GPU/cluster runner ships as an adapter contract
> with a `LocalRunner` reference impl; `acceptance.sh` in this repo exercises
> the whole loop end-to-end and is the fastest way to see it work.

## Install

From the zip or a clone of this repo (Python ≥ 3.11):

```bash
pip install -e .          # or: pip install .
smh --help
./acceptance.sh           # optional: installs into a throwaway venv, scaffolds a
                          # demo project, drives the loop, prints ACCEPTANCE PASS
```

## Quickstart

1. **Scaffold a project.**

   ```bash
   smh init myproject --name myproject
   cd myproject
   smh current            # → "myproject (repo default)"
   ```

   You get:

   - `smh.toml` — project name + ledger files + facts-catalog config. The
     `project` key is also the default namespace every bare id resolves to.
   - `JOURNAL.md` — your claims. §0.2 is a **generated** belief-ledger table
     (never hand-edit it); §1 holds the claim blocks under `1.1 Hypotheses`,
     `1.3 Questions & mechanisms`, `1.4 Decisions`; §5.1 is the write contract
     in one screen. Ships two clearly-marked example claims so the graph is
     valid on day one — retract or edit them.
   - `EVIDENCE.md` — the evidence log: one table row per measured result
     (`RE-NN`), each with a source.
   - `smh_facts.py` — the number allow-list the gate reads (starts empty).
   - `.claude/skills/` — the five bundled Claude Code skills (below). Any
     Claude Code session opened in the project dir picks them up.

2. **Talk through your research with Claude, then harvest it.** Describe what
   you're testing and why. When you've reasoned out a hypothesis or a question,
   run the **`journal-harvest`** skill: it scans the discussion, diffs against
   what the journal already holds, and proposes draft claims for the real
   gaps. Each is gated like a half-round — it can state a *prediction* but
   never fabricate a result — and you confirm before it lands (via `smh mint`).

3. **Run experiments, then close the loop.** When results come back, run the
   **`journal-round`** skill: it finds what finished, shows an honest digest
   with a recommended action per claim, and — once you pick — folds the numbers
   into `EVIDENCE.md` as `RE-NN` rows, updates claim statuses and refreshes the
   graph. Results enter *only* as evidence rows with a source; the gate
   enforces it.

4. **See the graph and the gate.** `smh render` writes
   `Reports/<project>-journal.html`; `smh graph --check` is the green gate.

## The CLI

Write verbs (transactional — each renders the block, regenerates §0.2, and
rolls back if it would add a graph warning or fail the fact gate; they confirm
by default, `-y` skips):

```bash
smh mint "<title>" --kind hypothesis|question|decision \
        [--bottom-line …] [--prediction …] [--mechanism …] [--design …] \
        [--link "refines H3"] [--confidence Low|Medium|High]
        # decisions: --rationale "…" and at least one --link are required
smh set-status H3 testing --note "…"     # open testing supported contradicted
                                         # settled answered retracted
                                         # (decisions: accepted superseded)
smh retract H3 --reason "…" [--supersede H7]   # kept, ids are forever
```

Read / check / regenerate:

```bash
smh graph --check                        # 0 warnings = green
smh graph --belief-ledger <project> --write   # regenerate §0.2 after a hand-edit
smh render                               # journal → HTML
smh gate <file>                          # fact-gate a drafted edit / report
smh current · smh use <project> [--global|--clear]   # current-project default
```

Lower-level loop pieces the skills drive: `python -m smh.pipeline.harvest`,
`…propose`, `…ingest`.

## The discipline (why it stays honest)

- A **hypothesis** is a falsifiable prediction you write **before** you have the
  answer. Keep results out of it.
- A **result** enters only as an `RE-NN` row in `EVIDENCE.md`, with a source.
- The **gate** blocks claims whose numbers aren't backed by evidence — so the
  ledger can't quietly drift ahead of what you actually measured.
- **Ids are forever.** Retract, don't delete; a re-scope keeps its id.

## The `focus` axis

Claims carry a curatorial focus so a long-running project stays readable:

- **active** — what you're working on now (leads reports and the graph view)
- **secondary** — in the record but not front-and-centre
- **historical** — settled or set aside; kept for provenance, de-emphasized

Focus is *curatorial only* — it never gates the science.

## The bundled skills

| skill | what it does |
|---|---|
| `journal-harvest` | reasoning → claims: mine the discussion for H/Q/D the journal lacks, land them gated |
| `journal-round` | results → journal: digest finished runs, fold RE rows + status changes, queue follow-ups |
| `request-experiment` | run a claim-tied job through the `Runner` seam (`LocalRunner` by default) and track it |
| `dejargonize` | rewrite journal-speak (RE-NN / H / Q ids, run tags) into plain English for an outsider |
| `write-handoff` | crystallize a job's decided state into `docs/HANDOFF_<name>.md` for a fresh session |

## Running experiments

The harness separates *what to run* (your claim-tied `JobSpec`) from *where it
runs* (a `Runner`). SMH ships a `LocalRunner` (`smh.adapters.runner`) that
executes a job as a local subprocess and captures its log — no GPU pool or SSH
required. Projects with a cluster supply their own `Runner`; the claim-gate and
journal loop are identical either way.
