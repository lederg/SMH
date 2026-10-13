"""Phase G of the journal restructure (docs/journal_restructure_spec.md §11):
the claim-harvest digest engine — the MIRROR of pipeline/ingest.py, other axis.

  ingest  :  scheduler/done/*.json  → digest → (gate) → RE rows    (results)
  harvest :  session memory stream  → digest → (gate) → H/Q claims (reasoning)

ingest harvests RESULTS into RE rows; harvest harvests the CLAIMS/QUESTIONS that
surface in session *reasoning* (e.g. "learned β never exceeds 0.333") into H/Q
entries. Same loop shape: a watermark over the input stream, a "newcomers since
last time" digest, an explicit `--accept` to advance — idempotent until acted on.

Pure compute. Writes NOTHING into the journal (mirrors ingest's rule). It emits
pipeline/harvest_digest.{md,json}: candidate claims diffed against the journal's
graph node set (the diff oracle), each with its nearest existing node + a draft
claim block in the JOURNAL.md §5.1 schema. The HUMAN picks; picks land through the
EXISTING propose.py half-round (gate = design ∪ journal, fail-closed) — harvest
does NOT reinvent the gate and never authors code/configs.

Two roles (Phase G decisions D1/D4):
  - the journal-harvest SKILL (the in-session agent) does the semantic judging:
    it fetches observations via the claude-mem MCP since the watermark, writes them
    to pipeline/harvest_input.json, and decides which candidates are real gaps
    against the node index this engine emits. (The LLM-judge lives in the skill,
    exactly as ingest's prose-drafting does.)
  - this engine (pure Python, no MCP) does the mechanical part: watermark, the node
    diff oracle, a cheap lexical nearest-node hint, the digest, the draft skeleton,
    next-free-id minting, and the accept. When MCP is absent (headless/cron) it
    DEGRADES GRACEFULLY by reading the project memory/*.md files directly (D1).

CLI:
  harvest.py                       read candidates (input file or memory fallback),
                                   diff vs journal nodes, write+print the digest
  harvest.py --input <file.json>   explicit candidate file (the skill's MCP dump)
  harvest.py --accept              advance the watermark over ALL digest candidates
  harvest.py --accept --id a,b     selective accept (others keep re-surfacing)
  harvest.py --next-id H           print the next free id in a kind's series (helper)
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from smh.config import resolve_root, state_path
ROOT = resolve_root(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from smh import journal_xref as jx  # noqa: E402  (load_journal/build_graph — two-file contract)
from smh.journal_node import render_node  # noqa: E402  (shared §1 block renderer)

STATE = state_path(ROOT, "harvest_state.json")
INPUT = state_path(ROOT, "harvest_input.json")
DIGEST_MD = state_path(ROOT, "harvest_digest.md")
DIGEST_JSON = state_path(ROOT, "harvest_digest.json")
# Memory-file fallback (D1): the project's curated memory dir. Used only when the
# skill could not dump an observation stream (no MCP in a headless/cron run).
MEMORY_DIR = os.environ.get(
    "LDP_MEMORY_DIR",
    "/home/gil/.claude/projects/-home-gil-dev-LDP-Journal/memory",
)

# A candidate "matches" an existing node (i.e. the journal already holds it, NOT a
# gap) when its best lexical overlap with a node clears this. Below it → likely gap.
# Deliberately low: harvest only *hints*; the skill's LLM judges the real boundary.
GAP_THRESHOLD = 0.34

# harvest mints these claim kinds (§11 boundary; RE rows are ingest's job). A
# `decision` (SPEC_journal_decision_node) is drafted when the reasoning shows a
# CHOSEN COURSE with rationale rather than an open question — same human-confirm
# gate, but it lands as a `#### D<n>:` decision node, not a Q.
HARVEST_KINDS = ("hypothesis", "question", "decision")

# Mint-time kind inference (#1 of the stuck-as-question fix, docs/AUDIT_stuck_as_
# question.md §5). A harvested point that COMMITS A DIRECTION is a hypothesis, not
# an open question — drafting it as `question` is what lets a live bet land stuck.
# We infer from the candidate title: an interrogative lead ("does/whether/which…")
# stays a question; a declarative directional assertion ("X beats Y", "lowers…",
# "the bet: …") drafts as a hypothesis (mints an H id). Conservative by design —
# ambiguous → question — and the draft is human-reviewed + gated before it lands,
# so a wrong guess is cheap. `decision` stays explicit (needs a rationale the skill
# detects), never inferred here.
_Q_LEAD_RE = re.compile(
    r"(?i)^\W*(does|do|is|are|can|could|would|will|which|where|what|whether|"
    r"how|why|when|should)\b")
_DIRECTIONAL_RE = re.compile(
    r"(?i)\b(the bet|predicts?|predicted|prediction|beats?|lowers?|raises?|"
    r"wins?|reduces?|improves?|increases?|decreases?|dominates?|outperforms?|"
    r"super-additiv\w*|recovers?|closes? the gap)\b")


def _infer_kind(cand):
    """question | hypothesis, inferred from the candidate title (see block above).
    Interrogative phrasing → question; a directional assertion → hypothesis;
    ambiguous → question (the safe default)."""
    title = (cand.get("title") or "").strip()
    if _Q_LEAD_RE.search(title):
        return "question"
    if _DIRECTIONAL_RE.search(title):
        return "hypothesis"
    return "question"

_STOP = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "to",
    "of", "in", "on", "at", "for", "with", "by", "as", "it", "its", "this", "that",
    "from", "not", "no", "yet", "than", "vs", "via", "we", "i", "our", "now",
    "more", "less", "over", "under", "into", "per", "any", "all", "new", "still",
}


# ── watermark (mirror ingest.load_state/save_state) ─────────────────────────────
def load_state():
    if os.path.exists(STATE):
        try:
            s = json.load(open(STATE))
            s.setdefault("seen_ids", [])
            s.setdefault("last_ts", "")
            return s
        except (ValueError, OSError):
            pass
    return {"seen_ids": [], "last_ts": ""}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"), indent=2, sort_keys=True)


# ── the diff oracle: journal graph node set ─────────────────────────────────────
def node_index():
    """Every tier-1 claim/question/evidence node already in the journal — the diff
    oracle (§11). A candidate whose substance matches no node is a gap. Section
    containers are dropped (they're layout, not claims). Returns [{id,type,title,
    summary,status}]."""
    g = jx.build_graph(jx.load_segments(ROOT))   # both projects, so next_free_id scopes per project (T3b₁)
    return [{"id": n["id"], "project": n.get("project", "ldp"),
             "type": n["type"], "title": n.get("title", ""),
             "summary": n.get("summary", ""), "status": n.get("status", "")}
            for n in g["nodes"] if n["type"] != "section"]


def next_free_id(kind, nodes=None, project="ldp", reserved=()):
    """Next FREE numeric id in a kind's series (IDs-forever, never reuse/renumber).
    H1..H8 exist → H9; Q1..Q5 → Q6. `kind` is the series prefix or a kind word.
    Allocation is scoped to `project` (T4a): a fresh project's first H is H1,
    independent of LDP's H-series — IDs are namespaced on (project, id).

    R3-reserve: `reserved` is an iterable of already-reserved id strings for this
    project (from the reservation store); they count as USED so a second concurrent
    session skips past them. The function stays pure — the CLI passes the store in;
    tests and other callers default to no reservations."""
    pre = {"hypothesis": "H", "question": "Q", "decision": "D",
           "H": "H", "Q": "Q", "D": "D"}.get(kind)
    if pre is None:
        raise ValueError(f"harvest mints H/Q/D only, not {kind!r}")
    nodes = node_index() if nodes is None else nodes
    used = [int(m.group(1)) for n in nodes
            if n.get("project", "ldp") == project
            for m in [re.match(rf"{pre}(\d+)$", n["id"])] if m]
    used += [int(m.group(1)) for rid in reserved
             for m in [re.match(rf"{pre}(\d+)$", rid)] if m]
    return f"{pre}{(max(used) + 1) if used else 1}"


# ── R3-reserve: cross-session id reservation ────────────────────────────────
# A minted-but-not-yet-landed id is written to a small store so a concurrent
# session's next_free_id skips it (two sessions can no longer both mint Q13). A
# reservation is released explicitly when the round/harvest is cancelled.
RESERVATIONS = state_path(ROOT, "id_reservations.json")


def _load_reservations():
    try:
        with open(RESERVATIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_reservations(res):
    os.makedirs(os.path.dirname(RESERVATIONS), exist_ok=True)
    json.dump(res, open(RESERVATIONS, "w"), indent=2, sort_keys=True)


def reserved_ids(project):
    """The list of ids currently reserved for `project` (R3-reserve)."""
    return list(_load_reservations().get(project, []))


def reserve_id(project, rid):
    """Record `rid` as reserved for `project`; idempotent."""
    res = _load_reservations()
    ids = res.setdefault(project, [])
    if rid not in ids:
        ids.append(rid)
        _save_reservations(res)


def release_id(project, rid):
    """Drop a reservation (round cancelled / id landed). Returns True if present."""
    res = _load_reservations()
    ids = res.get(project, [])
    if rid in ids:
        ids.remove(rid)
        if not ids:
            res.pop(project, None)
        _save_reservations(res)
        return True
    return False


# R3 (SPEC_journal_v2_drift_resistance): `--next-id` mints into a project's series,
# so it MUST know the project — hardcoding `ldp` returned Q8 while `bitdistiller:Q12`
# existed (concurrent-collision bug). Resolve it, never silently. propose.py's
# claim-prefix routing (`_project_of`) is the model; here the input is a KIND, not
# a `project:id`, so the project comes from the session-default precedence instead
# of a prefix (config.resolve_current_project — the one five-tier resolver).
DEFAULT_PROJECT = "ldp"


def resolve_project(explicit=None):
    """(project, source) for id minting, via the ONE five-tier resolver
    (docs/DESIGN_smh_session_default_project.md §7): explicit `--project` >
    `$SMH_PROJECT` env pin > `smh use` session file > `smh use --global` file >
    repo default (`ldp`). Never silent: the CLI echoes the resolved project unless
    it is the plain repo default."""
    from smh.config import resolve_current_project
    return resolve_current_project(ROOT, explicit)


# ── candidate input (D1: observation dump, else memory-file fallback) ───────────
def _tokens(text):
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _norm_candidate(c, source):
    """Normalize one raw candidate → {id, ts, title, text, source}. id is REQUIRED
    (the watermark keys on it). title defaults to the first non-empty text line."""
    cid = str(c.get("id") or c.get("name") or "").strip()
    if not cid:
        return None
    text = (c.get("text") or c.get("body") or c.get("content") or "").strip()
    title = (c.get("title") or c.get("description") or "").strip()
    if not title:
        title = next((ln.strip() for ln in text.splitlines() if ln.strip()), cid)
    return {"id": cid, "ts": str(c.get("ts") or c.get("ended") or ""),
            "title": title, "text": text or title, "source": source}


def _read_memory_files():
    """Fallback candidate source: the curated project memory/*.md (D1). Each file is
    one candidate — frontmatter `name`→id, `description`→title, the body→text. Used
    only when no observation dump is present (no MCP in a headless run)."""
    cands = []
    for p in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
        if os.path.basename(p) == "MEMORY.md":      # the index, not a fact
            continue
        try:
            raw = open(p).read()
        except OSError:
            continue
        name = re.search(r"^name:\s*(.+)$", raw, re.M)
        desc = re.search(r"^description:\s*(.+)$", raw, re.M)
        body = re.sub(r"^---\n.*?\n---\n", "", raw, count=1, flags=re.S).strip()
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(p),
                                        timezone.utc).isoformat()
        except OSError:
            ts = ""
        c = _norm_candidate(
            {"id": name.group(1).strip() if name else os.path.basename(p)[:-3],
             "title": desc.group(1).strip() if desc else "", "text": body,
             "ts": ts}, source=f"memory:{os.path.basename(p)}")
        if c:
            cands.append(c)
    return cands


def read_candidates(input_path=None):
    """The candidate claims to diff. Order (D1): an explicit/default observation dump
    (the skill's MCP fetch → harvest_input.json, a JSON list), else the memory-file
    fallback. Returns (candidates, source_label)."""
    path = input_path or (INPUT if os.path.exists(INPUT) else None)
    if path:
        try:
            raw = json.load(open(path))
        except (ValueError, OSError) as e:
            sys.exit(f"error: bad candidate input {path}: {e}")
        items = raw.get("observations", raw) if isinstance(raw, dict) else raw
        cands = [c for c in (_norm_candidate(x, "observation") for x in items) if c]
        return cands, f"observation dump ({os.path.relpath(path, ROOT)})"
    cands = _read_memory_files()
    return cands, f"memory files ({os.path.relpath(MEMORY_DIR, ROOT)}) [MCP fallback]"


# ── diff: candidate vs node set ─────────────────────────────────────────────────
def _overlap(a_tokens, b_tokens):
    """Overlap COEFFICIENT (|a∩b| / min(|a|,|b|)) — the cheap nearest-node hint (NOT
    the judgment). Jaccard divides by the union, which penalizes a long candidate
    against a short node summary (the first real harvest buried Q5 under H2 for the
    β-boundary obs that way). The coefficient is length-asymmetric: it asks "how much
    of the SMALLER side is covered," so a verbose candidate that fully contains a
    node's terms ranks it high. Still only a hint — the skill's LLM judges the gap.

    TODO(oracle): this is still lexical bag-of-words — it matches surface tokens, not
    meaning (a candidate phrased differently from its true neighbor still ranks low,
    and the LLM-judge has to scan the whole node list to recover). The next lift is
    semantic: embed candidate text + node title/summary and rank by cosine. Deferred
    (needs an embedding dep + a cache keyed on node text); the overlap-coefficient
    fix was enough to stop burying the right neighbor. See the skill's "weak hint"
    caveat. Match `node_index()` if it starts carrying more node context to embed."""
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))


def nearest(cand, nodes, k=3):
    """Top-k nearest existing nodes by lexical overlap, best first. The skill judges
    the real gap; this just points at the node a candidate is adjacent to."""
    ct = _tokens(cand["title"] + " " + cand["text"])
    scored = sorted(
        ({"id": n["id"], "type": n["type"], "title": n["title"],
          "score": round(_overlap(ct, _tokens(n["title"] + " " + n["summary"])), 3)}
         for n in nodes), key=lambda x: x["score"], reverse=True)
    return scored[:k]


def _draft(cand, near, nodes, kind="question"):
    """A ready-to-edit draft claim matching the AS-BUILT JOURNAL.md §1 format for its
    kind (so a landed claim actually parses into a graph node — handoff step 6/7):

      hypothesis → a `### H<n>:` HEADING (§1.1 form)
      question   → a `- **Q<n>:**` BULLET with nested sub-bullets (§1.3 form)
      decision   → a `#### D<n>:` HEADING (§1.4 form, SPEC_journal_decision_node)

    `kind` is INFERRED by `_infer_kind` from the candidate phrasing (#1 of the
    stuck-as-question fix): a directional assertion drafts as hypothesis, an
    interrogative as question, ambiguous → question. The skill can still override
    (to decision for a chosen course with rationale, or to correct a mis-inference)
    before landing — the draft is a reviewed, gated skeleton. Mints the next free id, wires
    a `links:` edge to the nearest node. NO numbers invented — the verdict is prose;
    any number must already be in the journal (the gate enforces this when gated)."""
    if kind not in HARVEST_KINDS:
        raise ValueError(f"harvest mints {HARVEST_KINDS}, not {kind!r}")
    pre = {"hypothesis": "H", "question": "Q", "decision": "D"}[kind]
    cid = next_free_id(pre, nodes)
    hist = (f"harvested {cand['source']} (obs `{cand['id']}`"
            + (f", {cand['ts']}" if cand['ts'] else "") + ")")
    if kind == "decision":                          # §1.4 decision form
        # A decision needs a rationale line AND ≥1 underpins/supersedes edge to
        # clear the gate — seed an `underpins` to the nearest claim (edit to the
        # real load-bearing claim); a decision carries NO result/evidence link.
        und = (f"underpins {near[0]['id']}" if near
               else "underpins <claim — REQUIRED, a decision must affect something>")
        fields = [
            # focus=active: a freshly harvested claim is, by construction, on the
            # current research front (in-regime) — SPEC_journal_focus §Harvest. Demote
            # to secondary/historical by hand (via the focus decision) if off-path.
            ("kind", "decision · **status:** accepted · **focus:** active"),
            ("context", "(what forced the choice — edit)"),
            ("choice", cand["title"]),
            ("rejected", "(the alternatives not taken — edit)"),
            ("rationale", "(why this choice over the alternatives — REQUIRED, gated)"),
            ("links", und),
            ("history", hist),
        ]
        return {"id": cid, "kind": "decision", "status": "accepted",
                "text": render_node(cid, "decision", cand["title"], fields)}
    link = f"see {near[0]['id']}" if near else "(none — standalone)"
    fields = [
        # combined `· ` line, house style. focus=active: an in-regime mint joins the
        # live front by default (SPEC_journal_focus §Harvest); hand-demote if off-path.
        ("kind", f"{kind} · **status:** open · **focus:** active"),
        ("confidence", "Low"),
        ("verdict", cand["title"]),
        ("evidence", "(none yet — harvested from session reasoning)"),
        ("links", link),
        ("history", hist),
    ]
    return {"id": cid, "kind": kind, "status": "open",
            "text": render_node(cid, kind, cand["title"], fields)}


def collect(state, candidates, nodes):
    """Newcomers since the watermark, annotated with nearest node + likely-gap flag.
    Mirrors ingest.collect: filters by seen_ids (a re-run re-surfaces the same set
    until --accept)."""
    seen = set(state["seen_ids"])
    out = []
    for c in candidates:
        if c["id"] in seen:
            continue
        near = nearest(c, nodes)
        best = near[0]["score"] if near else 0.0
        out.append({**c, "nearest": near, "best_score": best,
                    "likely_gap": best < GAP_THRESHOLD,
                    "draft": _draft(c, near, nodes, kind=_infer_kind(c))})
    return out


# ── emit ────────────────────────────────────────────────────────────────────────
def build_digest(newcomers, nodes, source):
    new_ids = sorted(c["id"] for c in newcomers)
    last_ts = max([c["ts"] for c in newcomers] + [""])
    return {"source": source, "last_ts": last_ts, "new_ids": new_ids,
            "n_new": len(newcomers), "n_gaps": sum(c["likely_gap"] for c in newcomers),
            "candidates": newcomers,
            "node_index": [{"id": n["id"], "type": n["type"], "title": n["title"]}
                           for n in nodes]}


def accept_ids(dg, sel=None):
    """The id-set + max ts to consume on --accept. sel=None → every candidate (full
    accept). Otherwise only the chosen ids — selective, so unchosen candidates keep
    re-surfacing until their owner acts (mirrors ingest.accept_ids)."""
    cands = dg["candidates"] if sel is None else [c for c in dg["candidates"]
                                                  if c["id"] in sel]
    return {c["id"] for c in cands}, max([c["ts"] for c in cands] + [""])


def render_md(dg):
    L = ["# Harvest digest — claims/questions in reasoning since last time", "",
         f"_Source: {dg['source']}._", ""]
    if not dg["n_new"]:
        L.append("_Nothing new since the watermark._")
        return "\n".join(L) + "\n"
    L += [f"**{dg['n_new']} new** candidate(s), **{dg['n_gaps']} likely gap(s)** "
          f"(no matching journal node), latest `{dg['last_ts'] or '—'}`.", "",
          "Harvest *proposes*; you *dispose*. A candidate is a GAP only if you judge "
          "it a real claim the journal doesn't already hold — the nearest-node score "
          "is a lexical hint, not the verdict. Land a pick via the EXISTING half-round "
          "(it gates design ∪ journal, fail-closed): see the per-candidate steps and "
          "**`JOURNAL.md` §5.1** (the write contract). Harvest mints **H/Q/D** "
          "(hypothesis / question / decision), never RE rows, never code/configs.", ""]

    # decision board: one row per candidate
    L += ["## Candidates (for discussion)", "",
          "| candidate id | likely gap? | nearest node | score |",
          "|---|---|---|---|"]
    for c in dg["candidates"]:
        n0 = c["nearest"][0] if c["nearest"] else None
        near = f"{n0['id']} ({n0['title'][:40]})" if n0 else "—"
        L.append(f"| `{c['id']}` | {'**GAP**' if c['likely_gap'] else 'covered'} | "
                 f"{near} | {c['best_score']:.2f} |")
    L.append("")

    for c in dg["candidates"]:
        L += [f"## `{c['id']}`  ·  {'**likely GAP**' if c['likely_gap'] else 'likely covered'}",
              "", f"> {c['title']}", ""]
        if c["text"] and c["text"] != c["title"]:
            snippet = c["text"][:500] + ("…" if len(c["text"]) > 500 else "")
            L += [snippet, ""]
        L += ["_Nearest existing nodes:_"]
        L += [f"  {i+1}. `{n['id']}` ({n['type']}, score {n['score']:.2f}) — {n['title'][:60]}"
              for i, n in enumerate(c["nearest"])] or ["  (none)"]
        d = c["draft"]
        L += ["", f"_Proposed draft claim (`{d['id']}`, kind={d['kind']}, "
              f"status={d['status']}) — edit, gate, then land:_", "",
              "```markdown", d["text"].rstrip(), "```", "",
              "To land (the half-round — drafts a gated entry, queues any experiments "
              "it needs):", "",
              f"    python pipeline/propose.py round {d['id']} --configs <cfg,...>   "
              "# or --from <tag> --set k=v --name <tag>", "",
              f"then gate + reviewed Edit into JOURNAL.md §1 (see §5.1). If it carries "
              "a prediction + an experiment, flip kind→hypothesis; if it is a chosen "
              "course with rationale (no open question), flip kind→decision (§1.4).", "",
              f"**Or PARK** (you judged it not a §1 claim — meta/process/duplicate, or "
              f"it folds into an existing node): `python pipeline/harvest.py --dismiss "
              f"--id {c['id']}` retires it without a journal edit.", ""]
    L += ["---", "",
          "After landing the picks: `python pipeline/harvest.py --accept --id "
          "<ids>` to advance the watermark over what you JOURNALED. Use `--dismiss "
          "--id <ids>` to retire judged non-claims (same watermark effect, no land). "
          "Anything you neither land nor dismiss keeps re-surfacing next run.", ""]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Phase-G claim-harvest digest "
                                 "(reasoning → H/Q candidates), the mirror of ingest.")
    ap.add_argument("--input", help="candidate observation dump (JSON list); default "
                    "pipeline/harvest_input.json, else the memory-file fallback")
    ap.add_argument("--accept", action="store_true",
                    help="advance the watermark over this digest's candidate ids "
                         "(call AFTER the picks are landed). Without it, nothing is "
                         "consumed — a re-run re-surfaces the same set (idempotent).")
    ap.add_argument("--dismiss", action="store_true",
                    help="PARK: retire judged non-claims (meta/process/duplicate, or "
                         "folds into an existing node) — same watermark effect as "
                         "--accept, but the explicit 'not a §1 claim' verb. Mirrors "
                         "ingest's park. Pair with --id; a bare --dismiss parks ALL.")
    ap.add_argument("--id", dest="ids",
                    help="comma-separated candidate id(s) to accept/dismiss (default: "
                         "ALL). Selective consume leaves the rest re-surfacing.")
    ap.add_argument("--next-id", dest="next_id", metavar="KIND",
                    help="print the next free id for a kind (H|Q|D) and exit — the "
                         "skill uses this to mint a harvested claim's id.")
    ap.add_argument("--project", help="project whose id series to mint into "
                    "(R3). Precedence: --project > $SMH_PROJECT > `smh use` session "
                    "> `smh use --global` > 'ldp'. Wrong project = a colliding id "
                    "(Q8 when bitdistiller:Q12 exists).")
    ap.add_argument("--no-reserve", action="store_true",
                    help="with --next-id: print the next id WITHOUT reserving it "
                         "(R3-reserve; default is to reserve so a concurrent "
                         "session skips it).")
    ap.add_argument("--release", metavar="ID",
                    help="release a reserved id (round cancelled / id landed) for "
                         "the resolved project, then exit (R3-reserve).")
    a = ap.parse_args()

    if a.release:
        project, _ = resolve_project(a.project)
        ok = release_id(project, a.release)
        sys.stderr.write(f"[{'released' if ok else 'no reservation for'} "
                         f"{project}:{a.release}]\n")
        return

    if a.next_id:
        project, source = resolve_project(a.project)
        if source == "repo default":
            sys.stderr.write(
                f"[--next-id: no project pin; minting into '{project}'. Set one "
                f"with `smh use <name>` or pass --project.]\n")
        elif source != "explicit":
            sys.stderr.write(f"[--next-id: project {project} ({source})]\n")
        rid = next_free_id(a.next_id, project=project,
                           reserved=reserved_ids(project))
        if not a.no_reserve:
            reserve_id(project, rid)
            sys.stderr.write(f"[reserved {project}:{rid} — release with "
                             f"`--release {rid}` if the round is cancelled]\n")
        print(rid)
        return

    state = load_state()
    nodes = node_index()
    candidates, source = read_candidates(a.input)
    newcomers = collect(state, candidates, nodes)
    dg = build_digest(newcomers, nodes, source)

    os.makedirs(os.path.dirname(DIGEST_JSON), exist_ok=True)
    json.dump(dg, open(DIGEST_JSON, "w"), indent=2)
    open(DIGEST_MD, "w").write(render_md(dg))
    sys.stdout.write(render_md(dg))

    if not (a.accept or a.dismiss):
        return

    sel = None
    if a.ids:
        sel = {i.strip() for i in a.ids.split(",") if i.strip()}
        have = {c["id"] for c in dg["candidates"]}
        unknown = sel - have
        if unknown:
            sys.exit(f"\n[--id: not in this digest: {', '.join(sorted(unknown))} "
                     f"— candidates here: {', '.join(sorted(have)) or '(none)'}]")
    verb = "dismissed" if a.dismiss else "accepted"
    ids, ts = accept_ids(dg, sel)
    if ids:
        state["seen_ids"] = sorted(set(state["seen_ids"]) | ids)
        state["last_ts"] = max(state["last_ts"], ts)
        save_state(state)
        scope = f" for {', '.join(sorted(sel))}" if sel else ""
        sys.stderr.write(f"\n[{verb} {len(ids)} candidate(s){scope}; watermark "
                         f"-> {state['last_ts'] or '(unset)'}]\n")
    else:
        sys.stderr.write(f"\n[--{'dismiss' if a.dismiss else 'accept'}: nothing new "
                         "to consume]\n")


if __name__ == "__main__":
    main()
