---
name: dejargonize
description: Rewrite jargon-heavy research text (journal/evidence summaries, results, conclusions full of RE-NN/H/Q/D claim ids, run tags, arm codenames, project shorthand) into plain English for an outside reader, grounded in what the internal references actually mean, with a whitelist of concepts the reader is assumed to know. Use whenever the user asks to "dejargonize", "make this plain English", "write this for an outsider / colleague / slides / a paper / an email", "explain without the shorthand", or pastes journal-style text and asks for a readable version — even if they don't name the skill.
---

# Dejargonize

Turn internal research prose into text an outside reader can follow. The input
is usually optimized for the journal — dense with claim ids (`RE-12`, `H7`,
`otherproj:Q3`), arm/config codenames (`warm-b`, `twostage`, `lr3e-4-g8`),
run tags, and project metaphors ("the floor", "coupled", "honest"). Those tokens
carry precise meaning *inside* the project and none outside it. The job is not
to delete them but to replace each with the thing it stands for.

## 1. Establish the audience (whitelist)

The whitelist is the set of concepts the reader is assumed to already know —
these may be used freely, unexplained. Build it from two sources:

1. **Standing file**: `docs/dejargonize_allowed.md` in the user's project (read
   it if it exists; **create it if absent** — a short bulleted list of terms,
   one per line, is enough). This is the default audience.
2. **Inline additions**: anything the user says in the request ("assume the
   reader knows X and Y"). Inline statements override the file in both
   directions — they can add terms or revoke them ("don't assume they know
   gradient descent").

Everything not on the whitelist gets the plain-English treatment. When genuinely
unsure whether a term is common knowledge for the stated audience, prefer a
short in-place gloss over assuming — a redundant half-sentence costs little; an
unexplained term loses the reader.

## 2. Inventory the jargon

List every token in the input that a whitelist-only reader would stumble on:

- claim/evidence ids (`RE-NN`, `H`/`Q`/`D`-numbers, `project:id` forms)
- run tags and job names (`abl_warm_s42`, `sweep-0003`)
- arm, config, and codename shorthand (`warm-b`, `twostage`, `base+aug`,
  `ep4`, `n=3`)
- internal metaphors and verdict language ("floor", "honest", "wash",
  "folded", "lever", "margin survives")
- eval-suite shorthand (`core-3`, `val-loss`, `held-out-acc`) and acronyms

## 3. Ground every token — never guess

Each inventoried token must be resolved to what it *means* before rewriting.
Look it up; do not expand from vibes — a plausible-sounding wrong expansion is
worse than the jargon, because the reader can't detect it.

Where to look (the user's project):
- Claim ids → the project's ledger files, `JOURNAL.md` (H/Q/D claim blocks) and
  `EVIDENCE.md` (RE-NN rows and C-entries), as named in `smh.toml`. A
  `project:RE-NN` cross-reference resolves to **that** project's ledger pair
  (`smh.toml` lists the co-located projects and their files); a bare id belongs
  to the project the text came from. The claim's own statement/verdict is the
  meaning. `smh render` gives a browsable view if the raw markdown is unwieldy.
- Arm/config names → the code, configs, or design notes that define them
  (the training/eval scripts, `configs/`, `docs/DESIGN_*.md`); grep is enough.
- Numbers → keep them exactly as written in the source. Never recompute,
  round differently, or add numbers the input doesn't contain.

If a token can't be resolved from the project or conversation, ask the user for
a gloss rather than inventing one. Listing 2–3 unresolved terms in one question
is fine.

## 4. Rewrite

Principles, in priority order:

1. **Findings, not bookkeeping.** Claim ids and run tags never appear in the
   output. `RE-12` becomes the finding it recorded ("an earlier experiment
   showed the warm-start hurt accuracy at both model sizes"), or disappears
   entirely if it's pure cross-reference plumbing. Provenance the outside reader
   can't follow is noise.
2. **Expand meaning, not name.** Don't rename `warm-b` to "the warm variant";
   say what it is ("a run that initializes from the smaller model's weights
   instead of from scratch"). The reader should understand the *thing*, not
   learn a new label.
3. **Preserve the epistemics.** Keep hedges, caveats, sample sizes, and
   negative results. Dejargonizing is translation, not marketing — "n=2 seeds,
   spread negligible" becomes "two random seeds, which agreed closely", not
   silence.
4. **Numbers survive verbatim.** Every quantitative claim in the output must
   appear in the input or its grounded sources.
5. **Simple English for everything off-whitelist.** Short sentences, everyday
   verbs, no stacked noun phrases. A term used repeatedly may be introduced
   once with a plain gloss and then reused ("the smaller model — hereafter
   the student").

Output shape: unless the user specifies one, write flowing prose of roughly the
input's length, keeping the input's section structure if it had one. If the
user asks for "1–2 paragraphs" or a specific format, that wins and detail may
be dropped to fit.

Before delivering, reread the draft once through the outside reader's eyes and
catch stray repo words that slipped through — "fork", "arm", "floor", "owed",
"landed", run tags, anything only an insider would parse. These leak most often
in closing sentences about code/process state, where the register drifts back
to journal voice.

## Worked example

Input (journal register):

> RE-12: warm-b redeems the small-data regime at floor, fine-tune washes the
> endpoint (~0.3 pt); practical pick warm-b.

Output (whitelist: fine-tuning, accuracy):

> When we initialized the model from the smaller pretrained checkpoint instead
> of from scratch, it was clearly better in the low-data setting before any
> fine-tuning. After fine-tuning, however, the difference almost vanished
> (about 0.3 accuracy points), so the warm-start initialization is the
> practical choice even though fine-tuning largely erases the gap.

Note what happened: the claim id vanished, the arm name became a description,
the internal verdict words ("redeems", "washes", "floor", "endpoint") became
plain statements, and the number and the honest "fine-tuning erases the gap"
caveat both survived.
