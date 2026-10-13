---
name: write-handoff
description: Write a HANDOFF_*.md doc that crystallizes the current job's decided state so a fresh-context session can continue it — then (optionally) hand off to a new session. Use when the remainder of a job is a baton-pass, not a next inline step — "write a handoff", "hand this off", "package this for a new session", or when you (Claude) judge the current session has done the thinking and the rest is execute-or-wait that belongs in its own context. NOT for in-process parallel work whose result returns here (use the Agent tool), and NOT for capturing reasoning into the journal (use journal-harvest).
---

# write-handoff

Package a job's **decided state** into `docs/HANDOFF_<slug>.md` so a session with
fresh context can read it and continue cheaply. This skill is the doc-author step;
launching the new session is optional and up to the user (Step N).

## Step 0 — decide FIRST: inline · subagent · session

Before writing anything, answer one fork question:

**Does the result feed my very next step, or is the baton the whole point?**

- **Feeds my next step** → don't hand off. Either do it inline, or delegate a
  *scoped* piece with the **Agent tool** (subagent runs in-process, result returns
  to THIS context — for parallel search / bounded edits / verification).
- **Baton is the point** → this skill. The remaining work should own its own
  context and continue independently.

Only proceed if the baton-pass signals hold:

**FOR handoff (write the doc):**
- remainder is a long or serial **poll/wait** (trickling jobs, CI, staged deps)
- remainder is **mechanical**, framing already locked (audit done → apply; bug
  diagnosed → fix; phase boundary crossed → next phase)
- clean **phase / round boundary** in a multi-step project
- needs its own **project pin / memory lane** to not tangle this session (e.g.
  a different `smh use <p>` default)
- live context is large AND the remainder is self-contained

**AGAINST (keep inline, no doc):**
- tight user feedback loop — framing still moving
- result feeds your next decision
- short (a few steps) — a handoff doc costs more than the work
- a STOP-gate would trip immediately — you'd just start a session to sit idle
  waiting on the user. Don't. Wait inline instead.

Anchor the call on **task shape** (serial-wait / phase-boundary / decided-
mechanical), which you can see — NOT on guessed context-fullness, which you
can't introspect reliably.

If the signals are mixed, say so and ask the user rather than guess.

## The 7 bones (every handoff doc)

Keep every `docs/HANDOFF_*.md` to the same skeleton so the next session knows
where to look:

1. **Title** — one line, the goal as an arrow chain.
   `# HANDOFF — <subject>: <step> → <step> → <deliverable>`
2. **Link to durable context** — the memory file / journal section
   (`JOURNAL.md` claim ids, `EVIDENCE.md` rows) that holds the full backstory.
   "Read that first; this doc = the immediate live task only." Don't inline the
   backstory — point to it.
3. **Hard rules** — the constraints that must not be violated (don't re-run job
   X, don't touch the shared dataset, ask before destructive, which project the
   journal verbs default to). Pull the live ones from the project's `CLAUDE.md`
   + memory; state them flat.
4. **Where things stand** — current state, what's already done, what pends. Real
   tags / job ids / file paths, not prose.
5. **The task** — numbered, imperative steps. Exact commands where known.
6. **STOP / gate** — where the session must halt and wait for the user (before
   `smh mint` / commit / narrative edit / irreversible act). Most jobs have one —
   name it.
7. **Uncommitted edits owed a commit** — files touched, so the next session (or
   the user) knows what's dirty and why.

Keep it short — a good handoff runs 50–100 lines. Point to context, don't copy it.

## Naming

`docs/HANDOFF_<slug>.md`, slug = short kebab of the subject
(`HANDOFF_warmstart_ablation_fold.md`). If a session name is wanted, derive it
from the slug (`"warmstart ablation fold handoff"`).

## Step N — propose, then (optionally) hand off

Writing the doc is safe and needs no confirmation. Starting a new session is a
real outward action, so **exercise the judgment to propose, but let the user
decide the launch.**

1. Write `docs/HANDOFF_<slug>.md`.
2. Tell the user: the doc path, the derived session name, and **one line on why
   handoff-not-inline** (which FOR-signal fired).
3. Offer the handoff. If they want it, the generic recipe is: **open a new Claude
   Code session in the project directory and tell it to read
   `docs/HANDOFF_<slug>.md` in full, then continue the work it hands off, asking
   before any destructive or irreversible action.** If the job lives in a
   co-located SMH project, the new session should first run `smh use <p>` (or
   export `SMH_PROJECT=<p>`) so its journal verbs default correctly — say which.
4. If the user prefers to keep going here, the doc still stands as a checkpoint
   — continue inline.

A project may wrap step 3 in its own launcher script (terminal multiplexer
window, container, permission flags); that's the project's concern, not this
skill's. Never launch a
session that skips permission prompts without the user's explicit say-so for this
job.
