---
name: request-experiment
description: Run an experiment for a claim and track it through SMH's Runner seam. Use whenever you want to RUN a training/eval/analysis job tied to a journal claim and collect its result — the shipped LocalRunner executes it as a local subprocess (no cluster required); a project with a GPU pool or CI backend supplies its own Runner behind the same interface. Also use to check whether a job is still running, collect its result + log, or cancel it. This is the SUBMIT/RUN side of the loop; fold the results into the journal with journal-round.
---

# request-experiment

SMH separates **what to run** (a claim-tied job) from **where it runs** (a
`Runner`). The journal loop — gate, harvest, round, graph — is identical no matter
where jobs execute. This skill is the run side: describe a job, launch it through a
`Runner`, track it, collect the log, and hand the result to `journal-round`.

The reference backend is **`LocalRunner`** (`smh.adapters.runner`): it runs a
job's command as a local subprocess and captures its output to `<log_dir>/<tag>.log`
— no GPU pool, no SSH, nothing to install. A project with a cluster, a batch
service, or CI supplies its own `Runner` (implementing the same Protocol); a
shared GPU queue is one such backend. Everything below works against any of them.

## Tie every job to a claim

The one rule that makes an SMH run different from a bare `python train.py`: a job
**names the open claim it serves**. That claim tie (`JobSpec.goal`) is what lets
`journal-round` group the result under the right hypothesis, and — for a project
that wires the scheduler core + `ClaimGate` — is checked *before* the job runs
(a job may only name an *open* journal claim; closed/unknown claims are refused).

- **Every job declares a goal.** The `goal:` block (`kind` + `claim` + `text`)
  travels with the job — in `JobSpec.goal` for a `cmd` job, or as a `goal:` block
  inside the job's config file for a config-file job (stamped `goal.claim=<id>`;
  `propose queue`/`round --finish` write it for you). The result inherits it, so
  `journal-round` can group the run under its claim without guessing.
- `kind` is `science` or `scaffold`. A `science` job's `claim` must be a real
  journal id (`H<n>`/`Q<n>`/`D<n>`/`RE-NN`/`C<n>` from `JOURNAL.md`/`EVIDENCE.md`);
  a `scaffold` job's `claim` is a free tag (tooling/infra, not a finding).
- **Cross-project claims need the explicit `project:id` form** (`otherproj:H4`).
  The runner does NOT apply the SMH current-project default that the journal verbs
  use — a bare `H4` in a job is read against the project the job's config/ledger
  belongs to. Check what you're on with `smh current` before tying a job to a bare
  id, and prefix whenever the claim lives in another co-located project.
- `tag` is the run identifier — it drives the log path (`<tag>.log`) that
  `journal-round` reads. Keep it unique.

## Run a job (LocalRunner)

`LocalRunner` implements `capacity → place → launch → poll → result`. Drive it from
a short Python snippet (or your own runner script):

```python
from smh.adapters.runner import JobSpec, LocalRunner

runner = LocalRunner(log_dir="runs")            # where <tag>.log lands
spec = JobSpec(
    id="j1",
    config_ref=None,                            # LocalRunner runs cmd, not config files
    cmd="python train.py --alpha 3 --seed 6",   # the actual work
    tag="ana-a3-s6",                            # -> runs/ana-a3-s6.log
    goal={"kind": "science", "claim": "H1", "text": "does alpha=3 beat the baseline?"},
)

placement = runner.place(spec, runner.capacity())   # None => no capacity, leave queued
handle = runner.launch(spec, placement)             # starts the subprocess
```

`config_ref` (config-file jobs) is out of scope for the reference backend — pass a
`cmd`. A project whose runner understands config files puts the config name in
`config_ref` instead and leaves `cmd` `None`.

## Track / collect

```python
status = runner.poll(handle)     # Status(state=running|done|failed, detail=...)
result = runner.result(handle)   # Result(metrics=..., log_path=..., exit=...)
runner.cancel(handle)            # terminate a running/queued job
```

A job flows `running → done|failed`. `result` is populated once terminal:
`result.exit` is the process exit code, `result.log_path` points at the captured
log, `result.metrics` carries whatever the runner extracted (LocalRunner records
`{"exit": rc}`; a domain runner adds `eval_loss`, etc.). The log at
`<log_dir>/<tag>.log` is what `journal-round` ingests to build its digest.

## Batch / dependencies

Loop `JobSpec`s to launch several; gate a follow-up on a predecessor by polling
its `handle` to `done` before launching the next. Richer scheduling — a shared
queue, cross-machine placement, dependency chains, retries — is exactly what a
project's own `Runner` (backed by the scheduler core in `smh.scheduler`) provides;
the reference `LocalRunner` is deliberately minimal (one local slot, always
placeable).

## Close the loop

Launching a job doesn't touch the journal — results enter **only** through
`journal-round`, and **only** as evidence rows with a source. Once your jobs are
`done`, run the **journal-round** skill: it finds what finished, shows an honest
digest with a recommended action per claim, and — after you pick — folds the
numbers into `EVIDENCE.md` as `RE-NN` rows. That separation is the whole point:
the runner produces logs, the human-reviewed round decides what becomes a claim.

## Don't
- Don't write results straight into `JOURNAL.md`/`EVIDENCE.md` from here — that's
  `journal-round`, gated and human-reviewed. This skill only *runs* the work.
- Don't reuse a `tag` whose log already exists — it collides in the digest.
- Don't launch a `science` job that names a closed or non-existent claim; mint the
  claim first (`journal-harvest`) or use `kind: scaffold` for pure tooling.

To fold finished results into the journal, see **journal-round**. To mint the
hypothesis a job tests before you run it, see **journal-harvest**.
