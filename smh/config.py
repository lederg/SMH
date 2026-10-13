"""SMH instance config — `smh.toml` at the project root.

A stranger's repo carries a generated `smh.toml` naming its ledger files and its
FactsCatalog. The two LDP-hardcoded seams — `journal_xref.DEFAULT_LEDGER` and
`pipeline.facts._default_catalog` — read this to resolve the *instance* instead of
defaulting to LDP. LDP itself ships NO `smh.toml`, so `load_config` returns None
and both seams fall back to their byte-unchanged LDP defaults (the fallbacks are
runtime + root-aware so this stays a pure addition).

Stdlib `tomllib` (Python 3.11+), no third-party dep.
"""
import importlib
import json
import os
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on <3.11
    tomllib = None


def resolve_root(pkg_fallback, start=None):
    """Resolve the project ROOT (where `smh.toml` / `JOURNAL.md` live) for an
    smh that may be *installed* (pip) rather than living inside the instance repo.

    Order: `SMH_ROOT` env > the nearest ancestor of the cwd (or `start`) that
    contains an `smh.toml` > `pkg_fallback` — the caller's `__file__`-derived
    guess, which is correct ONLY when `smh/` sits inside the instance repo (the
    LDP reference instance). A pip-installed `smh` driving a separate project
    dir would otherwise resolve ROOT to its site-packages location.

    LDP stays byte-identical: run from the repo, the cwd walk-up finds the
    tracked top-level `smh.toml` → ROOT is `LDP_Journal`; even with no cwd match
    the in-repo `pkg_fallback` is also `LDP_Journal`."""
    env = os.environ.get("SMH_ROOT")
    if env:
        return os.path.abspath(env)
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, "smh.toml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return pkg_fallback


def config_path(root):
    """The conventional config location: `<root>/smh.toml`."""
    return os.path.join(root, "smh.toml")


STATE_SUBDIR = ".smh"


def state_path(root, name):
    """Path for a runtime-state / digest artifact under `<root>/.smh/`.

    The journal-loop modules (ingest/harvest/propose/plots/repro_check) write
    their transient state here — the digest, the watermark, the drafting context.
    It hangs off the resolved project ROOT, NOT the `smh` package dir, so a
    pip-installed `smh` driving a separate project writes into that project (a
    `.smh/` beside its `smh.toml`), never into site-packages. Callers create the
    dir lazily at write time (`makedirs(dirname(path))`), so importing a module
    to define these constants has no filesystem side effect."""
    return os.path.join(root, STATE_SUBDIR, name)


def load_config(root):
    """Parse `<root>/smh.toml` -> dict, or None when it is absent (or tomllib is
    unavailable). Returns None — not {} — so callers branch cleanly on
    `cfg["ledger"] if cfg else DEFAULT`."""
    path = config_path(root)
    if tomllib is None or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return tomllib.load(f)


def instantiate(ref):
    """Resolve a `"module:Class"` reference to a zero-arg instance. The one way
    core turns a config string (a FactsCatalog / FigureCatalog name) into an
    object — so `pipeline.facts._default_catalog` and
    `pipeline.build._default_figure_catalog` share one import mechanism and core
    never hardcodes an instance module."""
    mod, cls = ref.split(":")
    return getattr(importlib.import_module(mod), cls)()


# ── session-level "current project" default ─────────────────────────────────────
# docs/DESIGN_smh_session_default_project.md §7. A five-tier resolver so SMH
# commands stop needing an explicit project id on every call, without leaking
# across concurrent single-user sessions in the same checkout:
#     explicit --project/prefix > $SMH_PROJECT env pin > per-session file
#     > repo-global file > repo default sentinel ('ldp').
# Absent env + session + global ⇒ the sentinel ⇒ byte-identical to the old
# hardcoded-`ldp` behavior (the standing LDP-renders-byte-identically invariant).
DEFAULT_PROJECT = "ldp"


def repo_default_project(root):
    """Tier 5 — the repo's own default project. A scaffolded (`smh init`) or
    otherwise single-instance repo names itself via the top-level `project`
    key in `smh.toml`; that wins over the `DEFAULT_PROJECT` sentinel so a
    stranger's `smh mint` / `gate` / `set-status` never route to a phantom
    `ldp` namespace. The co-located reference repo carries no top-level
    `project` key, so it resolves to the sentinel — byte-identical."""
    cfg = load_config(root)
    v = (cfg or {}).get("project")
    return v if isinstance(v, str) and v.strip() else DEFAULT_PROJECT


def _slug(s):
    """Filename-safe token component: `%3` -> `-3`, collapsed/trimmed."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "session"


def _session_token():
    """Stable per-session isolation key — the boundary that stops one session's
    `smh use` from repointing another. `$SMH_SESSION` (exported by
    `new-claude.sh`) > `$TMUX_PANE` (inherited by every child of a tmux pane,
    unique per pane) > the controlling tty > ppid, the last resort for a bare
    `docker exec` with no tmux (not stable across Bash-tool calls, but nothing
    better exists there). Pane-derived tokens are prefixed `pane-` so the stale
    sweep (see the CLI) only ever touches recyclable pane files."""
    v = os.environ.get("SMH_SESSION")
    if v:
        return _slug(v)
    v = os.environ.get("TMUX_PANE")
    if v:
        return "pane-" + _slug(v)
    try:
        return "tty-" + _slug(os.ttyname(0))
    except OSError:
        return f"ppid-{os.getppid()}"


def session_project_path(root, token=None):
    """The per-session state file (tier 3): `<root>/.smh/session/<token>.project`.
    `.smh/` is gitignored (`STATE_SUBDIR`), so this never pollutes git or a render."""
    return state_path(root, os.path.join("session", f"{token or _session_token()}.project"))


def global_project_path(root):
    """The repo-global state file (tier 4): `<root>/.smh/project`. Repo-local, NOT
    `~/.smh/` — scoped to THIS clone; a second clone starts at the repo default."""
    return state_path(root, "project")


def _read_project_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def resolve_current_project(root, explicit=None):
    """`(project, source)` under the five-tier precedence (§7.1). `source` is one of
    `explicit` / `env pin` / `session` / `global default` / `repo default` — used
    for the stderr echo on minting/gating commands and by `smh current`."""
    if explicit:
        return explicit, "explicit"
    env = os.environ.get("SMH_PROJECT")
    if env:
        return env, "env pin"
    v = _read_project_file(session_project_path(root))
    if v:
        return v, "session"
    v = _read_project_file(global_project_path(root))
    if v:
        return v, "global default"
    return repo_default_project(root), "repo default"


def current_project(root, explicit=None):
    """The resolved current project id (§7.1). Absent env + session + global it is
    `DEFAULT_PROJECT`, so every existing caller stays byte-identical. This is the
    ONE resolver every consumer (render/build/harvest --next-id/propose+ingest
    bare-id) routes through instead of hardcoding `'ldp'` / `project=None`."""
    return resolve_current_project(root, explicit)[0]


def known_projects(root):
    """The set of project ids `smh use` / `$SMH_PROJECT` may name: the co-located
    ledger keys ∪ the default sentinel. Set-time validation rejects a typo
    (`smh use bitdistller`) instead of minting into a phantom namespace."""
    from smh import journal_xref as jx
    return {p for p, _ in jx.resolved_ledger(root)} | {repo_default_project(root)}


# ── persistent per-project "current defaults" (subject / regime) ────────────────
# A sibling to the current-project resolver above, for the OTHER drift the user
# hit: "current default" (which model SUBJECT + which quant REGIME is the live
# front) was smeared across the shell submit (`job_templates.sh`), the journal
# framing (D2 `regime:` line + §0.1 headline) and the session briefings — three
# layers that disagreed and kept re-surfacing settled history (the 3-bit `nf3`
# regime) as if current. This makes ONE persistent record the source all three
# derive from: the shell reads its `shell` knob block, the journal focus banner
# reads `label`/`regime`, the SessionStart brief prints it.
#
# Record (JSON): {subject, regime, label, shell:{ENV:VALUE, …}}. Per-project,
# repo-local under `.smh/` (gitignored), so a second clone / an un-set project
# resolves to None ⇒ every consumer keeps its own hardcoded fallback ⇒
# byte-identical to before the record existed (same standing invariant as the
# current-project resolver). Tiers mirror §7: session file > repo-global file.
DEFAULTS_SUBDIR = "defaults"


def defaults_path(root, project):
    """Repo-global current-defaults record (tier 2): `<root>/.smh/defaults/<project>.json`.
    The `set once, every session inherits` tier — the fix for settled regimes
    resurfacing across fresh sessions."""
    return state_path(root, os.path.join(DEFAULTS_SUBDIR, f"{_slug(project)}.json"))


def session_defaults_path(root, project, token=None):
    """Per-session current-defaults override (tier 1): isolated by the same session
    token as `session_project_path`, so an A/B of a different regime in one session
    never repoints another. `.smh/session/<token>.<project>.defaults.json`."""
    return state_path(root, os.path.join(
        "session", f"{token or _session_token()}.{_slug(project)}.defaults.json"))


def _read_defaults_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_current_defaults(root, project):
    """`(record | None, source)` for the current-defaults of `project`, under
    session > global precedence. `None` (absent both files) is the byte-identical
    fallback: consumers keep their own hardcoded defaults. `source` is one of
    `session` / `global default` / `unset`."""
    v = _read_defaults_file(session_defaults_path(root, project))
    if v is not None:
        return v, "session"
    v = _read_defaults_file(defaults_path(root, project))
    if v is not None:
        return v, "global default"
    return None, "unset"


def current_defaults(root, project):
    """The resolved current-defaults record for `project`, or `None` when unset.
    The ONE resolver every consumer routes through (shell submit / focus banner /
    session brief) instead of hardcoding a subject/regime."""
    return resolve_current_defaults(root, project)[0]


def project_config(root, project):
    """The `[projects.<name>]` sub-table of `<root>/smh.toml`, or None. A repo that
    co-locates several SMH instances (LDP + BitDistiller + FracBits in one tree)
    keeps LDP the *default* (top-level keys absent → the byte-unchanged fallbacks)
    and addresses each non-default instance by name here — its `facts_catalog`,
    `figure_catalog`, `presentations` dir. `pipeline/build.py --project <name>`
    resolves through this."""
    cfg = load_config(root)
    if not cfg:
        return None
    return (cfg.get("projects") or {}).get(project)
