"""`smh init` — scaffold a new SMH research project.

Generates the minimal valid ledger a stranger needs to hit the real loop
(harvest -> gate -> round -> graph) on their own work:

    smh.toml      project + ledger + facts_catalog config (read by both seams);
                  the top-level `project` key is ALSO the repo-default current
                  project (config.repo_default_project), so bare-id `smh mint` /
                  `set-status` / `gate` route to this project, never to `ldp`
    JOURNAL.md    ## 0 (§0.2 GENERATED belief ledger, marker-fenced, seeded with
                  the example row) ; ## 1. Claims with the §1.1 Hypotheses /
                  §1.3 Questions / §1.4 Decisions subsections `smh mint` lands
                  under (+ two live EXAMPLE claims in the mint-rendered block
                  shape) ; ## 3. Backlog ; ## 5.1 the compact write contract
    EVIDENCE.md   ## 2. Evidence (+ the table header, zero data rows)
    smh_facts.py  class Facts(RegexFactsCatalog): ledger from smh.config, _FACTS={}

Why the examples are LIVE, not commented (the spec said "commented-out"): a
`## N.` section becomes a graph node ONLY when it contains a claim/experiment
node (`journal_xref._build_segment` materializes section nodes lazily), and the
line-based parser does not honor HTML comments. A truly-empty — or genuinely
hidden — scaffold therefore yields ZERO section nodes and fails the acceptance
contract ('>= 2 section nodes, 0 warnings'). So the scaffold ships two live,
clearly-marked "replace or delete me" example claims: an `### H1` hypothesis +
`Q1` question in S1 and one `[IDEA]` in S3. They give the graph its >=2 sections,
show the grammar by example, and delete in one edit once the colleague (or
`journal-harvest`) adds their own. The example prose carries NO claim-ID
references and NO numbers beyond the section indices, so the gate stays green.
"""
import os

# The four generated files, in dependency order (config first).
FILES = ("smh.toml", "JOURNAL.md", "EVIDENCE.md", "smh_facts.py")


def _smh_toml(name):
    return f'''project = "{name}"
# ledger: list of [namespace, [files...]]; one namespace for a solo project.
ledger = [["{name}", ["JOURNAL.md", "EVIDENCE.md"]]]
# the instance FactsCatalog as "module:Class", imported by
# pipeline.facts._default_catalog so the gate/build read THIS project's facts.
facts_catalog = "smh_facts:Facts"
'''


_EXAMPLE_H1 = "example hypothesis — replace or delete me"


def _journal_md(name):
    # The §0.2 block below is the GENERATED belief ledger (journal_xref
    # belief_ledger_table): one row per claim that carries a **bottom-line:**.
    # It is seeded here with the example H1 row exactly as the generator emits
    # it, so `smh graph --check` is advisory-free on day one and `smh mint`'s
    # post-write regeneration finds its marker-fenced region to rewrite.
    return f'''# {name} — research journal

Structured research ledger (SMH). Claims (hypotheses / questions / decisions)
live here; measured results live as RE rows in EVIDENCE.md. The discipline is
the gate, and it is automatic: a hypothesis is a *prediction* you write
**before** the result; a result enters only as an RE row with a source. Seed
your first real claims by talking through your research with Claude and
running the `journal-harvest` skill (or `smh mint`), then retract or edit the
two examples below.

## 0. State of belief

### 0.2 State-of-belief ledger (what we believe now)

One row per claim that opts in with a **bottom-line:** field. GENERATED from
the §1 claim blocks — never hand-edit; `smh mint` / `set-status` / `retract`
regenerate it, or run `smh graph --belief-ledger {name} --write`.

<!-- BEGIN generated:belief-ledger (R5) — do not hand-edit; regenerate with `python -m smh.journal_graph --belief-ledger <project>` -->
| id | claim (≤12 words) | verdict now | conf | key RE | status |
|----|---------------------|-------------|------|--------|--------|
| H1 | {_EXAMPLE_H1} | {_EXAMPLE_H1} | low | — | open |
<!-- END generated:belief-ledger -->

## 1. Claims

<!-- One claim node per block; `smh mint` writes them in this exact shape and
lands each kind under its subsection below (matched by the subsection name).
Grammar is shown by the two live EXAMPLE claims — replace them with your own
(or let `journal-harvest` mint them). -->

### 1.1 Hypotheses

### H1: {_EXAMPLE_H1}
- **kind:** hypothesis · **status:** open · **focus:** active
- **bottom-line:** {_EXAMPLE_H1}
- **confidence:** Low
- **prediction:** state one falsifiable prediction and why you expect it; keep
  results out of the claim — they land as RE rows in EVIDENCE.md.
- **evidence:** —
- **links:** (none — standalone)

### 1.3 Questions & mechanisms

- **Q1:** example open question — replace or delete me
  - **kind:** question · **status:** open · **focus:** active
  - **confidence:** Low
  - **evidence:** —
  - **links:** (none — standalone)

### 1.4 Decisions

<!-- `smh mint "<title>" --kind decision --rationale "…" --link "underpins H1"`
lands `#### D<n>: <title>` blocks here. A decision needs a rationale and at
least one link. -->

## 3. Backlog

<!-- Queued experiment ideas, one per bullet, with a status bracket and a
same-line title: a bullet of the shape "[IDEA] <title>". Live job status lives in
your scheduler/runner, not here. -->

- **[IDEA]** example experiment — replace or delete me

## 5. Conventions

### 5.1 Write contract

- **Where things go.** Claims (H / Q / D) → this file, §1, under their kind's
  subsection. Measured results → EVIDENCE.md §2 as `RE-NN` table rows, each with
  a source. Never a third file; never split one claim across files.
- **How claims are written.** Through `smh mint` / `smh set-status` /
  `smh retract` — transactional: the write is rolled back if it adds a graph
  warning or fails the fact gate; §0.2 is regenerated. Hand-edit only to rewrite
  an existing claim's prose or to add RE rows, then run
  `smh graph --belief-ledger {name} --write` and `smh graph --check`.
- **Claim block schema.** First bullet `kind · status · focus`; then
  `bottom-line` (the §0.2 verdict cell), `confidence` (Low / Medium / High),
  optional `prediction` / `mechanism` / `design`, `evidence` (RE ids only —
  numbers live in EVIDENCE.md), `links` (edge verbs such as `refines`,
  `supersedes`, `tested-by`, `ties`, `underpins`, `contrasts`).
  Status vocab: open · testing · supported · contradicted · settled · answered ·
  retracted (decisions: accepted · superseded). Focus: active · secondary ·
  historical — curatorial only, never gates the science.
- **Ids are forever.** Next free id in the series; demote, don't delete
  (`smh retract` keeps the block); a re-scope keeps its id; a split is a new id
  plus a `links` edge. Never renumber ids or the `## N.` section numbers.
- **The gate.** Every number in a claim must already be sourced from the ledger;
  a harvested hypothesis may state a prediction and a design but never a
  result. Green = `smh graph --check` with no warnings and `smh render` exit 0.
- **The human disposes.** Nothing auto-commits science; review each landing.
'''


def _evidence_md(name):
    return f'''# {name} — evidence log

## 2. Evidence

The evidence-log table — each measured result is one row. Add rows via the
`journal-round` skill, never by hand-asserting a number without a source. The
RE-NN ids number themselves as rows land.

| RE-NN | date | kind | finding | source | claim |
| --- | --- | --- | --- | --- | --- |
'''


def _smh_facts_py():
    return '''"""Generated FactsCatalog for this SMH project (`smh init`).

`_FACTS` starts empty: the gate's number allow-list is derived from the journal
text itself, and named facts are added as the project states stable headline
numbers (or never, for a non-numeric domain). The ledger files come from
`smh.toml` via `smh.config`, so this stays in sync with the configured ledger.
"""
import os

from smh.adapters.facts import RegexFactsCatalog
from smh import config

ROOT = os.path.dirname(os.path.abspath(__file__))

_cfg = config.load_config(ROOT) or {}
# Solo project = one namespace; flatten its files to absolute ledger paths.
_ledger = _cfg.get("ledger") or [["", ["JOURNAL.md", "EVIDENCE.md"]]]
LEDGER_PATHS = [os.path.join(ROOT, f) for _ns, files in _ledger for f in files]

_FACTS = {}


class Facts(RegexFactsCatalog):
    """This project's facts catalog: an empty `_FACTS` map over the configured
    ledger. Add `"key": (regex, group, cast)` entries as you mint headline
    numbers worth citing in a report."""

    def __init__(self):
        super().__init__(ledger_paths=LEDGER_PATHS, facts=_FACTS)
'''


def _content(name):
    return {
        "smh.toml": _smh_toml(name),
        "JOURNAL.md": _journal_md(name),
        "EVIDENCE.md": _evidence_md(name),
        "smh_facts.py": _smh_facts_py(),
    }


# Bundled Claude Code skills (the journal loop rituals). Present only in the
# PACKAGED smh (scripts/smh-package.sh copies plugin/smh-journal/skills/ to
# smh/skills/ and declares them package data); absent in the reference repo,
# where the live skills sit in .claude/skills/ and must not be double-loaded.
SKILLS_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
SKILLS_DST = os.path.join(".claude", "skills")


def install_skills(root, force=False):
    """Copy the bundled skills into `<root>/.claude/skills/` so a Claude Code
    session opened in the project picks them up. No-op (returns []) when the
    package carries none. Existing skill dirs are left alone unless `force`."""
    import shutil
    if not os.path.isdir(SKILLS_SRC):
        return []
    dst_root = os.path.join(root, SKILLS_DST)
    written = []
    for name in sorted(os.listdir(SKILLS_SRC)):
        src = os.path.join(SKILLS_SRC, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        dst = os.path.join(dst_root, name)
        if os.path.exists(dst):
            if not force:
                continue
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        written.append(dst)
    return written


def init(root, name=None, force=False, skills=True):
    """Scaffold a new SMH project into `root`. `name` defaults to the directory
    basename. Idempotent-by-refusal: raises FileExistsError if any of the four
    generated files already exist, unless `force=True` (overwrite). With
    `skills` (default) the bundled Claude Code skills, if the package carries
    any, are copied into `<root>/.claude/skills/`. Returns the list of written
    absolute paths."""
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    name = name or os.path.basename(root)

    if not force:
        clash = [f for f in FILES if os.path.exists(os.path.join(root, f))]
        if clash:
            raise FileExistsError(
                f"refusing to overwrite existing files in {root}: "
                f"{', '.join(clash)} (pass force=True to overwrite)")

    written = []
    for fname, text in _content(name).items():
        path = os.path.join(root, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append(path)
    if skills:
        written.extend(install_skills(root, force=force))
    return written


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Scaffold a new SMH project.")
    ap.add_argument("name", nargs="?", help="project name (default: cwd basename)")
    ap.add_argument("--root", default=".", help="target directory (default: cwd)")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    a = ap.parse_args()
    for p in init(a.root, a.name, a.force):
        print(f"wrote {p}")
