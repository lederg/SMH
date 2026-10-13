---
name: journal-round
description: Do a "round" on the research journal: find which experiments finished since last time, present an honest digest with a recommended action per claim, discuss it, then fold the picks into ground truth (JOURNAL.md/EVIDENCE.md + plots) and queue the follow-up experiments — the whole results→journal→next-experiments loop. Use whenever the user wants to check finished runs and update the journal: "do a round" / "do a journal round", "what finished?" / "did my runs land?", "pull / ingest / incorporate the results", "update the journal + plots with the latest runs", "close the loop", or "decide the next round of experiments from results" — even if they don't name the pipeline. This is the RESULTS side of the loop. Not for SUBMITTING or tracking jobs (use request-experiment), building a presentation/report, a one-off plot refresh with no journal change, or read-only journal questions.
---

# journal-round

Closes the experiment loop: finished jobs → **honest digest** → discuss → journal
edits + queued follow-ups + refreshed plots → watermark advanced. The hard part is
that the journal is **curated science prose** — this skill *proposes*, the user
*disposes*; it never silently rewrites the journal or commits it.

The machinery ships in the `smh` package (`smh.pipeline.ingest`,
`smh.pipeline.propose`, `smh.pipeline.plots`) plus the transactional node writers
(`smh mint` / `set-status` / `retract`). This skill is the operating procedure over
those modules. Runtime artifacts (`ingest_digest.{md,json}`, `propose_context.md`,
the watermark) live under **`<root>/.smh/`** beside your `smh.toml`.

## The loop (run in order)

### 1. Digest — what finished since last time
```bash
python -m smh.pipeline.ingest            # scan finished runs, write + print the digest
```
Run discovery goes through the project's `Runner` seam: the shipped `LocalRunner`
leaves `<log_dir>/<tag>.log` per job, and ingest reads those logs (a project with its
own `Runner` points ingest at wherever its runner lands results). Writes + prints
`.smh/ingest_digest.md`: a **decision board** (one recommended action per claim) then
per-claim detail (the move n/Δ/σ, point table, options), plus orphans and any
failures. `ingest_digest.json` is the machine copy (the gate's allow-list). All
numbers are **honest** — recomputed from each run's own artifacts, never a runner's
stored summary scalar (which can over-count).

### 2. Present + discuss
Surface the board to the user — per claim: the recommended action, the move, and
the options. **Don't act before they pick.** The recommendations are heuristics to
frame the decision, not orders. Failures are *reported*, not acted on.

> **Co-located projects.** If your `smh.toml` carries several projects, a
> `project:id` claim (`otherproj:H10`) routes itself. A **bare** id (`H8`) resolves
> to the SMH **current project** — the session default (`smh use <p>`), else
> `$SMH_PROJECT`, else the top-level `project` name in `smh.toml`. Working another
> project's round? set the default first (`smh use <p>`) or always prefix;
> `smh current` shows what's active and which tier set it. A single-project repo
> never needs a prefix.

### 3. Act on each pick
The recommended `action` maps to a tool:

| action | what it means | do |
|---|---|---|
| `tighten-row` / `new-finding` | a journal edit, usually **with** new experiments | **`round`** (§4) — the combined move |
| (existing claim, more data only) | add runs to a claim that already has a row | `propose queue` (§5) |
| `new-family` | an orphan tag needs a plot family | `plots add-family` (§6) |
| `reinforce` | flat move, just more n | bump n in the row, or a §4 changelog note (EVIDENCE.md) |
| `park` / scaffold | not journal-worthy / infra | §4 changelog `C-`entry (EVIDENCE.md), or nothing |

### 4. `round` — the combined journal+experiments move (the common case)
You almost always decide the journal update AND the next experiments in the **same**
discussion, so the journal entry should NAME the experiments it spawns. A science
claim can only be queued once it exists in the journal, so the order is fixed:
```bash
# a) plan the experiments + open a round (records pending state, builds the context)
python -m smh.pipeline.propose round <claim> --from <base_tag> --set alpha=3,T=4.5 --name <new_tag>
#    (or --seeds 7,8,9 for more seeds of the same config)
```
This writes `.smh/propose_context.md` — a drafting context that embeds the exact
planned tags and **requires an `In flight:` line** naming them (tags aren't numbers,
so they pass the gate). Then:
```
# b) draft the journal edit from that context (include the In flight: line)
# c) python -m smh.pipeline.propose gate <claim> <your_edit_file>     # must PASS
# d) LAND the fold (never auto-commit). Pick the mechanism by what you're writing:
#      · a brand-NEW claim (half-round, §4b) → smh mint "<title>" --kind hypothesis|question|decision …
#          (see journal-harvest §4 for the full flag set; mint is H/Q/D only)
#      · an RE-NN row → reviewed Edit into EVIDENCE.md §2 table (mint does not write RE rows)
#      · a claim's STATUS change → smh set-status <id> <status> --note "<one-liner>"
#          status vocab: open · testing · supported · contradicted · settled · retracted · answered
#          (decisions: accepted · superseded)
#      · RETIRING a claim → smh retract <id> --reason "…" [--supersede <new-id>]
#      · a claim's rich verdict/bottom-line PROSE rewrite → reviewed Edit into JOURNAL.md §1, then
#          smh graph --belief-ledger <project> --write      # regenerate §0.2 from the block
#    mint/set-status/retract write only the node BLOCK, regenerate the §0.2 belief ledger,
#    and ROLL BACK if the edit would add a graph warning or fail the fact gate. They
#    default-confirm (print the block for approval); pass -y only after the user approved it.
# e) python -m smh.pipeline.propose round <claim> --finish            # records the planned runs
```
Step (e) is **not optional** — until it runs, `ingest --accept` refuses to advance
the watermark (the experiments the journal promised aren't running yet). Launch the
runs it names via the **request-experiment** skill (through the project's `Runner`).
`round <claim> --cancel` drops a round you changed your mind about.

### 4b. Half-round — a NEW claim (H/Q) with no results (skip steps 1–3)
Sometimes there are no results to review: you have an idea and want to **mint a new
hypothesis** (`H`/`Q`) and queue its *first* experiments — a "discussion on empty
results." This is `round` on a claim the digest doesn't know yet. `round` auto-detects
that (no digest entry) and flips to **hypothesis mode**: the drafting context asks for
a *prediction* + the design (not results), the gate's allow-list becomes the **design
numbers** ∪ journal — so the entry can state the design it commits to but can't
fabricate a result. It prints a **HALF-ROUND** notice (if you only meant to add data to
an *existing* claim, that's your cue you mistyped the id).

**A new hypothesis usually needs new CODE, not just a new config** — a new mode,
model size, step budget, or algorithm. The tool does NOT author your code or config
(that's the actual research, done in the discussion). So there are two entry shapes:

- **New code / from-scratch config (the common case):** first write the new code +
  its config(s) (stamping `goal.claim=<claim>`), then point the round at the real
  config path(s):
  ```bash
  python -m smh.pipeline.propose round <new_claim> --configs <cfg1,cfg2>
  ```
  `round` reads those real configs for the gate allow-list (so the entry can cite the
  actual design) and names them in the `In flight:` line. No derivation, no fake base.
- **Variant of an existing shape:** clone-and-override, same as a full round —
  `round <new_claim> --from <base_tag> --set alpha=3,T=4.5 --name <tag>`.

The claim itself lands via `smh mint` (step 4d). A half-round is **soft**: because new
code can be days from running, it does NOT block `ingest --accept`. Instead it surfaces
in the digest as a *reminder* ("declared hypotheses — experiments still pending") until
you run `round <claim> --finish`. `--accept` consumes *results*, which a half-round has
none of, so you only call `--finish` here, never `--accept`. (A full round, finished in
the same session, keeps its hard block.)

### 5. `queue` — experiments only (existing claim, no journal change)
```bash
python -m smh.pipeline.propose queue <claim> --from <base_tag> --set k=v,... --name <tag>
python -m smh.pipeline.propose queue <claim> --seeds 7,8,9            # sugar
#   dry-run by default; add --write to write the configs, then launch via request-experiment.
```
Each derived config is stamped `goal.claim=<claim>`, so the next round auto-groups
its results under that claim — the loop tightens itself.

### 6. Journal-only edit (no experiments) + plots
```bash
python -m smh.pipeline.propose journal <claim>       # assemble drafting context
# draft -> gate -> LAND (same as round step d: mint / set-status / retract, or reviewed Edit
#   + `smh graph --belief-ledger <project> --write` for a prose rewrite), no queue

python -m smh.pipeline.plots refresh                 # re-render known-family plots
python -m smh.pipeline.plots orphans                 # propose a plot tuple per orphan
python -m smh.pipeline.plots add-family <tag> --name "…" --color "#…" --symbol … --write
```
Known-family runs are already on the plot the moment their log lands — `refresh` just
regenerates the figures (derivatives, safe to run unattended). An **orphan** (new tag
prefix) needs a plot family; `orphans` proposes one and the user confirms the taste
bits (name/color/marker) before `--write` appends it.

### 7. Accept — advance the watermark
```bash
python -m smh.pipeline.ingest --accept                    # ALL claims in the digest
python -m smh.pipeline.ingest --accept --claim RE-24      # only this claim's ids
python -m smh.pipeline.ingest --accept --claim RE-24,H7   # several
```
Marks the consumed ids seen so they stop re-surfacing. **One digest usually spans many
claims; accept only the ones you actually journaled this round** — pass `--claim
<a,b,…>` so the others keep re-surfacing for their owners (a bare `--accept` consumes
the WHOLE digest, silently retiring claims you never wrote up). `--claim` consumes that
claim's done **and** failed ids. Refuses (**exit 3**) while a `round` is open — with
`--claim` only a *selected* claim's open round blocks (others don't); finish or cancel
it first (`--force` overrides, use sparingly).

## Hard rules (these are why the loop is shaped this way)

> **The canonical write contract is `JOURNAL.md` §5.1** (two-file targets by entry
> kind, the claim schema, IDs-forever / demote-don't-delete / no-renumber, the gate,
> post-edit green gate). The rules below are the loop-specific highlights; §5.1 is the
> source of truth — defer to it, don't re-derive.

- **Never auto-commit journal science, never blind-append.** The journal is curated
  prose. Every edit is drafted, **gated** (`propose gate` fail-closes on any number not
  in the digest ∪ the journal), landed via `smh mint`/`set-status`/`retract` (H/Q/D
  nodes — transactional, default-confirm, §0.2 auto-regenerated) or a *reviewed* Edit
  (RE rows, prose rewrites), and left for the user to commit. Human disposes.
- **§0.2 is generated, never hand-written.** The belief-ledger table is derived from
  the node blocks; after any raw prose Edit run
  `smh graph --belief-ledger <project> --write`, then `smh graph --check` → 0 warnings.
- **Honest numbers only.** The digest recomputes metrics from each run's own artifacts.
  Don't reintroduce a runner's stored summary scalar.
- **Science vs infra routing.** A science finding is an `RE-NN` row (EVIDENCE.md §2) /
  H-section update (JOURNAL.md §1); infra/tooling is a §4 changelog `C-`entry
  (EVIDENCE.md), NOT an RE row. The digest routes `goal.kind=scaffold` jobs →
  `park`/changelog automatically — honor it.
- **Analysis IS evidence (R2).** A post-hoc decomposition or re-analysis of runs
  already in hand is a first-class `RE-NN` row with `Type: analysis` (no new job
  needed) — NEVER a measured number buried in a decision body. "No new run" is not
  "no evidence row"; the evidence log is the only home for measured numbers. A
  decision may then cite that row with `informed-by` (it shaped the choice; the
  decision is not tested by it).
- **Read-only over runs.** Ingestion reads finished logs; it never launches jobs.
  Queueing the follow-up experiments the user approved is fine (that's
  `request-experiment`'s domain, through the `Runner`) — this skill just records the
  plan.
- **Watermark by id, advanced only on `--accept`.** A resubmitted seed reuses a tag but
  is a new point/id. Re-running the digest without `--accept` is idempotent — it keeps
  re-surfacing the same set until you act, by design. The watermark is a **set of seen
  ids** (not a timestamp), so accept is **per-claim**: `--accept --claim X` consumes
  only X's ids and leaves the rest pending — one round needn't dispose of the whole
  digest.

## Don't
- Don't act on a recommendation before the user picks — present, then do.
- Don't skip the gate, or apply a journal edit the gate rejected.
- Don't hand-edit the §0.2 belief ledger — regenerate it.
- Don't forget `round … --finish` — the experiments the journal named must run.
- Don't `--accept` until the journal is approved, plots refreshed, rounds finished.

To RUN/submit experiments (the other half of the loop), see the **request-experiment**
skill. To mint a claim from discussion alone, see **journal-harvest**. To present the
journal as a report, see the report pipeline (`smh.pipeline.build`).
