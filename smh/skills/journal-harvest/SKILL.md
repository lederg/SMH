---
name: journal-harvest
description: Harvest the CLAIMS and QUESTIONS that surfaced in session *reasoning* (not results) into the journal — the mirror of journal-round. Scans the session reasoning/observation stream since the last harvest, diffs each candidate against what the journal already holds (the graph node set), and proposes draft H/Q claim entries for the real gaps — each gated like a half-round (it may state a prediction/design but cannot fabricate a result), human-confirmed before it lands in JOURNAL.md §1 via `smh mint`. Use whenever the user wants to capture reasoning into the journal: "harvest the journal" / "do a harvest", "did we conclude anything that isn't written down?", "what claims/questions came up that aren't in the journal?", "pull our reasoning / discussion into the journal", "mint the hypotheses we've been talking about", "what's in my head but not in JOURNAL.md?". This is the REASONING→claims side of the loop. NOT for: finished experiment results (use journal-round — the results side), running/tracking jobs (use request-experiment — the submit side), building a report, or read-only journal questions.
---

# journal-harvest

The symmetric twin of `journal-round`. `journal-round` harvests **results → RE
rows**; `journal-harvest` harvests the **claims/questions that surface in session
*reasoning* → H/Q/D entries**. Same loop shape, other axis:

```
journal-round   :  finished run logs      → digest → (gate) → RE rows      (results)
journal-harvest :  session reasoning       → digest → (gate) → H/Q/D claims (reasoning)
```

The journal is **curated science prose** — this skill *proposes*, the user
*disposes*. It never silently writes the journal or commits it. The machinery is
`smh.pipeline.harvest` (the digest engine) + `smh mint` (the transactional land
step) + `smh.pipeline.propose`'s **half-round** (to queue the experiments a claim
names — already built; harvest does NOT reinvent the gate).

> **The canonical write contract is `JOURNAL.md` §5.1** (two-file targets, the claim
> schema, IDs-forever / demote-don't-delete / no-renumber, the gate, post-edit green
> gate). Defer to it; the steps below are the harvest-specific operating procedure,
> not a second source of truth.

**Where state lives.** Every harvest artifact (`harvest_state.json`,
`harvest_input.json`, `harvest_digest.{md,json}`) is written under **`<root>/.smh/`**
— the gitignored runtime-state dir beside your `smh.toml` (resolved via
`smh.config.state_path`). Nothing is written into the package or into the ledgers
until you land a pick.

## The loop (run in order)

### 1. Gather candidates — reasoning since the last harvest
The judge lives **here, in the skill** (the engine is pure compute). Pull the session
reasoning since the watermark and dump it for the engine:

- **Preferred: a session observation stream / the conversation itself.** If you have
  a per-session memory or observation stream, collect entries newer than the last
  harvest (`.smh/harvest_state.json` → `last_ts`, skip ids already in `seen_ids`).
  Otherwise, distill the conversation so far into candidate one-liners yourself. Either
  way, write them as a JSON list to **`.smh/harvest_input.json`**:
  ```json
  [{"id":"S500","ts":"2026-06-23T06:00:00Z","title":"<one-line>","text":"<the reasoning>"}]
  ```
- **Fallback (no stream, nothing dumped):** skip the dump — the engine reads the
  project `memory/*.md` files directly if present (fewer, curated candidates, but the
  loop still runs).

### 2. Digest — candidates diffed against the journal
```bash
python -m smh.pipeline.harvest                       # uses .smh/harvest_input.json, else memory files
python -m smh.pipeline.harvest --input <file.json>   # explicit dump
```
Writes + prints `.smh/harvest_digest.md`: a **board** (one row per candidate:
likely-gap? · nearest existing node · lexical score) then per-candidate detail with
the nearest nodes and a **ready-to-edit draft claim** in the §1 format.
`harvest_digest.json` is the machine copy. The diff oracle is the journal graph's node
set — a candidate with no matching node is a **gap** (a claim the journal doesn't yet
hold).

### 3. Judge + present — which candidates are real gaps?
The nearest-node **score is a lexical hint, not the verdict.** YOU judge, against the
node titles, whether each candidate is:
- a **real new claim** the journal doesn't hold → a gap worth a draft, **or**
- already covered by an existing node (restated, refined) → fold into that node's
  `history:`/`verdict:` instead of minting a duplicate, or skip.
- a **chosen course with rationale** (a locked decision — "we picked X over Y because…",
  no open question to resolve and no prediction to falsify) → draft a **`decision`**,
  NOT a question. It `underpins` the claim(s) it is a premise of, so changing it later
  flags them.

Every harvested draft carries **`**focus:** active`** on its schema line: a claim minted
from current session reasoning is, by construction, on the live research front. Leave it
`active`; hand-demote to `secondary`/`historical` only when it's off the current path.

Surface the gaps to the user with the recommended draft. **Don't land before they pick.**
A candidate that's just chatter/infra is not a claim — **park** it (§5 `--dismiss`);
harvest mints **H/Q/D only** (hypothesis / question / decision), never an RE row, never
an infra C-entry (that's `journal-round`). Three outcomes per candidate: **land** (a real
new H/Q/D), **fold** (real, but an existing node already holds it → reviewed Edit to that
node's `verdict:`/`history:`, then `--accept` the obs), or **park** (not a §1 claim →
`--dismiss`).

> **The score is a weak hint — scan all nodes, don't trust the top row.** The oracle is
> lexical; the judgment is yours. The node list is the reference, not the ranking.

### 4. Land a pick — `smh mint` (one transactional, gated write)
A harvested claim lands exactly like a new hypothesis with no results. The id is
auto-allocated; if you want to see it first:
```bash
python -m smh.pipeline.harvest --next-id H|Q|D
#   --next-id and `smh mint` allocate into the SMH CURRENT PROJECT: the session default
#   (`smh use <p>`), else `$SMH_PROJECT`, else the top-level `project` name in your
#   `smh.toml` (a scaffolded repo names itself; a bare id like `H8` belongs to it).
#   The resolved project is echoed to stderr so a mis-pin is visible. Override with
#   --project <p>; `smh current` shows what's active and which tier set it.
```
Then:
```
# a) decide the fields (use the engine's draft block as the seed; kind is INFERRED from
#    the candidate phrasing (directional assertion → hypothesis, interrogative → question);
#    confirm it — keep hypothesis if it carries a prediction + a queued experiment, else
#    demote to question; flip to decision if it's a chosen course with rationale, no open
#    question — lands in §1.4)
# b) LAND via `smh mint`. Replaces the old "reviewed Edit into §1 + hand-written §0.2 row":
#    mint writes only the node BLOCK, then REGENERATES the §0.2 belief-ledger table from it
#    (no dual-write, no drift), and ROLLS BACK if the write would add a graph warning or
#    fail the fact gate.
smh mint "<title>" --kind <hypothesis|question|decision> \
    --bottom-line "<the §0.2 one-liner>" [--prediction "…"] [--mechanism "…"] [--design "…"] \
    [--link "refines H8"] [--link "ties otherproj:H5"] [--confidence Low]
#    · DECISION: --rationale + ≥1 --link REQUIRED (the §1.4 decision gate). --focus active default.
#    · default-CONFIRMS: it prints the rendered block for you to approve — THAT is the reviewed
#      land step (do not also hand-Edit). Pass -y ONLY after the user approved the exact block.
# c) if the claim names experiments it needs, open the half-round so the journal can cite them:
python -m smh.pipeline.propose round <id> --configs <cfg,...>   # new code + configs
#   (or --from <base_tag> --set k=v --name <tag> for a variant of an existing shape)
python -m smh.pipeline.propose round <id> --finish              # once they are queued
```
mint's **fact gate** is the cannot-fabricate-a-result property: any number the prose
cites must already be in the journal (no eval/Δ/σ a run hasn't produced). It is
journal-number-scoped — if the draft legitimately cites a **design-only** number (a
step budget, a learning rate), add it to the journal first or pre-check the draft with
`python -m smh.pipeline.propose gate <id> <edit_file>` (whose allow-list is design ∪
journal). Don't work around the gate either way.

**Boundary (hard):** harvest **proposes claims, never authors code/configs.** A new
hypothesis usually needs new code (a new mode + a bespoke config) — that's the actual
research, done in discussion, not by this skill. Harvest brackets it: mint the gated
claim, and (via the half-round) queue the experiments that test it.

### 5. Green gate + accept the watermark
After a claim lands (per §5.1):
- the **§0.2 belief ledger is GENERATED** — `smh mint` regenerated it from the node's
  `bottom-line`. **Never hand-edit it.** If you later touch a node's prose by raw Edit,
  regenerate with `smh graph --belief-ledger <project> --write`. Only touch the
  **§0.3 invariants registry** if a guard-rail changed;
- run the **green gate**: `smh graph --check` → 0 warnings · `smh render` → exit 0.
  A landed claim adds exactly the node/edges you minted, nothing else.
- advance the harvest watermark over **only the candidates you landed**:
```bash
python -m smh.pipeline.harvest --accept  --id S500,S501  # JOURNALED (landed or folded)
python -m smh.pipeline.harvest --dismiss --id S600,S601  # PARKED (judged not a §1 claim)
python -m smh.pipeline.harvest --accept                  # ALL candidates in the digest
```
`--accept` and `--dismiss` have the same watermark effect — both retire ids so they stop
re-surfacing; the verb just records whether you journaled it or judged it a non-claim.
Anything you neither land/fold (`--accept`) nor park (`--dismiss`) keeps re-surfacing. A
re-run without either is idempotent.

## Hard rules
- **Never auto-commit, never blind-append.** Every claim is drafted, **gated**, landed
  via `smh mint` (its default-confirm block preview IS the review; the transaction
  rolls back rather than leaving a warned journal), left for the user to commit. The
  pipeline proposes; the human disposes.
- **H/Q/D only.** Harvest mints hypotheses, questions, and decisions (a chosen course
  with rationale). Results are RE rows (`journal-round`); infra is a §4 changelog
  C-entry. Don't cross the streams.
- **No fabricated results.** A harvested verdict is prose + a prediction; any number must
  already be in the journal (the gate fail-closes on the rest).
- **IDs forever, next-free-id, no renumber, demote-don't-delete** — §5.1. A claim that
  refines an existing one mints a new id + a `--link` edge; it never overwrites.
- **§0.2 is generated.** Never hand-write a belief-ledger row; mint / set-status /
  retract regenerate it, and `smh graph --belief-ledger <project> --write` covers raw
  prose edits.
- **Don't author code/configs** from a harvested hypothesis — propose the claim, queue
  its experiments via the half-round, stop there.
- **Match the as-built §1 format** so the claim parses into a graph node: H → `### H<n>:`
  heading; Q → `- **Q<n>:**` bullet with nested sub-bullets; D → `#### D<n>:` heading
  with a `rationale` line + ≥1 `underpins`/`supersedes` link (the decision gate
  requires both; it carries no result/RE edge). `smh mint` emits exactly this — don't
  hand-build it.

## Don't
- Don't mint a claim a node already covers — fold into that node instead.
- Don't skip the gate, or apply an entry the gate rejected.
- Don't pass `-y` to `smh mint` before the user has approved the block.
- Don't `--accept` before the picks are landed + the green gate passes.
- Don't harvest **results** here (use `journal-round`) or **run jobs** (use
  `request-experiment`).

To harvest finished experiment RESULTS, see **journal-round**. To RUN the experiments a
harvested hypothesis needs, see **request-experiment**.
