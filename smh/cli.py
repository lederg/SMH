"""`smh` console entrypoint (SMH extraction, phase E0).

Live subcommands:
  - `smh init <dir> [--name NAME]` — scaffold a new SMH project (`smh.init.init`).
  - `smh use <project> [--global] [--clear]` — set/clear the current-project default
    (session tier, or repo-global with --global) — docs/DESIGN_smh_session_default_project.md.
  - `smh current` — print the resolved current project and its source tier.
  - `smh graph  [args…]` — build/check the journal claim graph (`smh.journal_graph`).
  - `smh gate   [args…]` — run the hard fact gate (`smh.pipeline.gate`).
  - `smh render [args…]` — render the journal to HTML (`smh.render_journal`).

The three journal-tooling verbs dispatch to the moved core modules by running
each as `__main__` via `runpy`, forwarding any trailing args verbatim — so each
module keeps its own arg handling and the `smh` CLI stays a thin router. (Pre-E2
these were stubs that refused to reach into the root-level `pipeline/` modules;
the E2 core move under `smh/` retired the guardrail.)
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys


def _cmd_init(args: argparse.Namespace) -> int:
    from smh import init as smh_init

    written = smh_init.init(args.dir, name=args.name, force=args.force,
                            skills=not args.no_skills)
    for path in written:
        print(f"wrote {path}")
    return 0


def _root() -> str:
    from smh import config

    return config.resolve_root(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cmd_use(args: argparse.Namespace) -> int:
    """Set/clear the current-project default (session tier, or --global tier).
    Validated against the known project set so a typo fails loudly at set-time —
    see docs/DESIGN_smh_session_default_project.md §7.3."""
    from smh import config

    root = _root()
    path = config.global_project_path(root) if args.glob else config.session_project_path(root)
    tier = "global" if args.glob else "session"
    if args.clear:
        try:
            os.remove(path)
            print(f"cleared {tier} project  ({path})")
        except FileNotFoundError:
            print(f"no {tier} project set  ({path})")
        return 0
    if not args.project:
        sys.stderr.write("error: `smh use` needs a project name (or --clear)\n")
        return 2
    known = config.known_projects(root)
    if args.project not in known:
        sys.stderr.write(
            f"error: unknown project {args.project!r}; known: {', '.join(sorted(known))}\n")
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args.project + "\n")
    print(f"set {tier} project = {args.project}  ({path})")
    return 0


def _prune_stale_sessions(root: str) -> None:
    """Opportunistic §7.4 sweep: drop `.smh/session/pane-*.project` whose tmux pane
    is gone (panes recycle ids). Best-effort — only pane-derived files, only when
    tmux is queryable; never touches `$SMH_SESSION`/tty tokens, never raises."""
    import glob
    import subprocess

    from smh import config

    d = os.path.dirname(config.session_project_path(root))
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    if out.returncode != 0:
        return
    live = {"pane-" + config._slug(p) for p in out.stdout.split()}
    for path in glob.glob(os.path.join(d, "pane-*.project")):
        tok = os.path.basename(path)[: -len(".project")]
        if tok not in live:
            try:
                os.remove(path)
            except OSError:
                pass


def _cmd_current(args: argparse.Namespace) -> int:
    from smh import config

    root = _root()
    _prune_stale_sessions(root)
    proj, source = config.resolve_current_project(root)
    print(f"{proj} ({source})")
    return 0


def _shell_quote(v: str) -> str:
    """Single-quote a value for safe `eval` in POSIX sh."""
    return "'" + str(v).replace("'", "'\\''") + "'"


def _cmd_defaults(args: argparse.Namespace) -> int:
    """Show / set / clear the persistent current-defaults record (subject + regime +
    shell knob block) for a project — the one source the shell submit, the journal
    focus banner and the SessionStart brief all derive from. `set` takes `key=value`
    pairs: lowercase keys (subject/regime/label) are record meta, UPPERCASE keys go
    into the `shell` block verbatim (no recipe baked into code). Persistent tier
    (repo-global) by default; `--session` writes an isolated per-session override."""
    from smh import config

    root = _root()

    # --all (session-brief use): one line per project that HAS a record, so a brief
    # surfaces the live defaults regardless of which project the session resolved to.
    if getattr(args, "all", False):
        for proj in sorted(config.known_projects(root)):
            rec, source = config.resolve_current_defaults(root, proj)
            if not rec:
                continue
            knobs = " ".join(f"{k}={v}" for k, v in (rec.get("shell") or {}).items())
            label = rec.get("label") or f"{rec.get('subject','?')} · {rec.get('regime','?')}"
            print(f"{proj} current defaults: {label}"
                  + (f"  [{knobs}]" if knobs else "") + f"  ({source})")
        return 0

    project = args.project or config.current_project(root)
    tier_session = args.session
    path = (config.session_defaults_path(root, project) if tier_session
            else config.defaults_path(root, project))
    tier = "session" if tier_session else "global"

    if args.action == "clear":
        try:
            os.remove(path)
            print(f"cleared {tier} defaults for {project}  ({path})")
        except FileNotFoundError:
            print(f"no {tier} defaults set for {project}  ({path})")
        return 0

    if args.action == "set":
        known = config.known_projects(root)
        if project not in known:
            sys.stderr.write(
                f"error: unknown project {project!r}; known: {', '.join(sorted(known))}\n")
            return 1
        rec = config._read_defaults_file(path) or {}
        shell = dict(rec.get("shell") or {})
        for pair in args.pairs:
            if "=" not in pair:
                sys.stderr.write(f"error: expected key=value, got {pair!r}\n")
                return 2
            k, v = pair.split("=", 1)
            if k[:1].isupper():
                shell[k] = v
            else:
                rec[k] = v
        if shell:
            rec["shell"] = shell
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"set {tier} defaults for {project} = {rec.get('label') or rec}  ({path})")
        return 0

    # show
    rec, source = config.resolve_current_defaults(root, project)
    if args.shell:
        # emit assign-if-unset lines so an explicit env var still wins; nothing when
        # unset ⇒ the sourcing script keeps its own ${VAR:-default} fallbacks.
        for k, v in ((rec or {}).get("shell") or {}).items():
            # assign ONLY when unset (`${VAR+x}` set-check): an explicit empty
            # (`ACT_QUANT= bash …`, e.g. a weights-only arm) is a deliberate choice
            # and must survive — the old `:+` guard matched ${VAR:=…} and silently
            # rewrote empties to the record (bit twice: fracqat leak, Block-AP 1.7B
            # W-only→W4A4). A knob that wants empty≡unset is the consumer's
            # `${VAR:-…}` fallback's job, not the emitter's.
            print(f'[ -n "${{{k}+x}}" ] || {k}={_shell_quote(v)}')
        return 0
    if args.brief:
        if rec:
            knobs = " ".join(f"{k}={v}" for k, v in (rec.get("shell") or {}).items())
            label = rec.get("label") or f"{rec.get('subject','?')} · {rec.get('regime','?')}"
            print(f"{project} current defaults: {label}"
                  + (f"  [{knobs}]" if knobs else "") + f"  ({source})")
        return 0
    if not rec:
        print(f"{project}: no current-defaults set  ({source}) — "
              f"consumers use their hardcoded fallbacks")
        return 0
    label = rec.get("label") or f"{rec.get('subject','?')} · {rec.get('regime','?')}"
    print(f"{project} ({source}): {label}")
    for k in ("subject", "regime"):
        if rec.get(k):
            print(f"  {k}: {rec[k]}")
    for k, v in (rec.get("shell") or {}).items():
        print(f"  shell {k}={v}")
    return 0


def _confirm(summary: str, assume_yes: bool) -> bool:
    """Default-confirm a journal write: print what will land, ask y/N. `--yes`
    skips the prompt; a non-interactive stdin with no `--yes` refuses (safe)."""
    print(summary)
    if assume_yes:
        return True
    try:
        return input("write this to the journal? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        sys.stderr.write("error: no tty to confirm on; pass --yes to write non-interactively\n")
        return False


def _resolve_project(args) -> str:
    from smh import config

    return getattr(args, "project", None) or config.current_project(_root())


def _cmd_inbox(args: argparse.Namespace) -> int:
    """Surface (or list) this session's scheduler failure-notice inbox.

    The owner identity is `$LDP_SESSION` — the SAME token the scheduler stamps on a
    job (`sched.sanitize($LDP_SESSION)`), so a session that submits under a name sees
    the failures for that name. `--surface` (default) prints unseen cluster notices
    and marks them seen; `--list` prints everything without mutating. Silent + exit-0
    on an empty inbox, so a SessionStart hook adds no noise.
    Design: docs/DESIGN_owner_failure_notify.md."""
    from datetime import datetime, timezone

    from smh import inbox

    root = _root()
    session = args.session or os.environ.get("LDP_SESSION", "default")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.list:
        rows = inbox.read(root, session, unseen_only=False)
        if not rows:
            print(f"(inbox empty for session '{session}')")
            return 0
        for r in rows:
            mark = "  " if r.get("seen") else "▶ "
            print(mark + inbox.format_notice(r).replace("\n", "\n  "))
        return 0
    unseen = inbox.read(root, session)                 # --surface (default)
    if not unseen:
        return 0                                       # silent: hook stays quiet
    print(f"📥 {len(unseen)} scheduler failure notice(s) — session '{session}' "
          f"(run `python -m smh.cli inbox --list` for history):")
    for r in unseen:
        print(inbox.format_notice(r))
    inbox.mark_seen(root, session, now=now)
    return 0


def _cmd_mint(args: argparse.Namespace) -> int:
    from smh import journal_mint as jm

    project = _resolve_project(args)
    confirm = (lambda s: _confirm(s, args.yes))
    try:
        cid, kind, path = jm.mint(
            project, args.title, kind=args.kind, status=args.status,
            focus=args.focus, confidence=args.confidence,
            bottom_line=args.bottom_line, links=args.link or (),
            prediction=args.prediction, mechanism=args.mechanism, design=args.design,
            context=args.context, rejected=args.rejected, rationale=args.rationale,
            confirm=confirm)
    except jm.MintError as e:
        sys.stderr.write(f"mint refused: {e}\n")
        return 1
    if cid is None:
        print("aborted — nothing written")
        return 1
    print(f"minted {project}:{cid} ({kind}) → {path}")
    return 0


def _cmd_retract(args: argparse.Namespace) -> int:
    from smh import journal_mint as jm

    project = _resolve_project(args)
    try:
        path = jm.retract(project, args.id, reason=args.reason, supersede=args.supersede,
                          confirm=(lambda s: _confirm(s, args.yes)))
    except jm.MintError as e:
        sys.stderr.write(f"retract refused: {e}\n")
        return 1
    if path is None:
        print("aborted — nothing written")
        return 1
    print(f"retracted {project}:{args.id} → {path}")
    return 0


def _cmd_set_status(args: argparse.Namespace) -> int:
    from smh import journal_mint as jm

    project = _resolve_project(args)
    try:
        path = jm.set_status(project, args.id, args.status, note=args.note,
                             confirm=(lambda s: _confirm(s, args.yes)))
    except jm.MintError as e:
        sys.stderr.write(f"set-status refused: {e}\n")
        return 1
    if path is None:
        print("aborted — nothing written")
        return 1
    print(f"set {project}:{args.id} status → {args.status} → {path}")
    return 0


def _run_as_main(module: str, argv: list[str]) -> int:
    """Execute `module` as if invoked `python -m module argv…`, in-process. Each
    core module owns its own CLI (graph's `main(argv)`, render's argparse, gate's
    inline `__main__`), so running it as `__main__` is the faithful, low-coupling
    router — no per-module signature knowledge here."""
    old_argv = sys.argv
    sys.argv = [module] + argv
    try:
        runpy.run_module(module, run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old_argv


_FORWARD = {
    "gate": "smh.pipeline.gate",
    "graph": "smh.journal_graph",
    "render": "smh.render_journal",
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="smh", description="Scientific Method Harness.")
    sub = ap.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="scaffold a new SMH research project")
    p_init.add_argument("dir", help="target directory for the new project")
    p_init.add_argument("--name", default=None, help="project name (default: dir basename)")
    p_init.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    p_init.add_argument("--no-skills", action="store_true",
                        help="don't copy the bundled Claude Code skills into <dir>/.claude/skills/")
    p_init.set_defaults(func=_cmd_init)

    p_use = sub.add_parser("use", help="set the current-project default for this session")
    p_use.add_argument("project", nargs="?", help="project id (omit with --clear)")
    p_use.add_argument("--global", dest="glob", action="store_true",
                       help="write the repo-global default (all new sessions) instead of this session")
    p_use.add_argument("--clear", action="store_true",
                       help="remove the (session, or --global) default")
    p_use.set_defaults(func=_cmd_use)

    p_cur = sub.add_parser("current", help="print the resolved current project + its source tier")
    p_cur.set_defaults(func=_cmd_current)

    p_def = sub.add_parser("defaults",
                           help="show/set/clear a project's persistent current-defaults (subject/regime + shell knobs)")
    p_def.add_argument("action", nargs="?", choices=("show", "set", "clear"), default="show",
                       help="show (default) | set key=value… | clear")
    p_def.add_argument("pairs", nargs="*",
                       help="for `set`: key=value pairs (lowercase=meta, UPPERCASE=shell knob)")
    p_def.add_argument("--project", default=None,
                       help="project id (default: the resolved current project)")
    p_def.add_argument("--session", action="store_true",
                       help="write/read the per-session override tier instead of the repo-global default")
    p_def.add_argument("--shell", action="store_true",
                       help="emit assign-only-if-UNSET guard lines for `eval` in a submit "
                            "script (explicit empty survives; set or nonempty wins)")
    p_def.add_argument("--brief", action="store_true",
                       help="one-line summary for a session brief (silent when unset)")
    p_def.add_argument("--all", action="store_true",
                       help="one line per project that has a record (for a SessionStart brief)")
    p_def.set_defaults(func=_cmd_defaults)

    # Journal-writer verbs — mint / retract / set-status (docs/DESIGN_smh_mint_command.md).
    # Each writes only the node BLOCK; the §0.2/§1 ledger table is regenerated from it.
    # All project-aware (default: the current-project); every write is confirm-gated
    # (--yes to skip) and transactional (rolls back if it would break the graph gate).
    p_mint = sub.add_parser("mint", help="mint a new H/Q/D journal claim node")
    p_mint.add_argument("title", help="the one-line claim (used as the heading + default bottom-line)")
    p_mint.add_argument("--kind", choices=("hypothesis", "question", "decision"),
                        default=None, help="node kind (default: inferred from the title)")
    p_mint.add_argument("--project", default=None, help="project id (default: the current project)")
    p_mint.add_argument("--status", default=None,
                        help="initial status (default: open for H/Q, accepted for D)")
    p_mint.add_argument("--focus", default="active", choices=("active", "secondary", "historical"))
    p_mint.add_argument("--confidence", default="Low", help="confidence label (H/Q)")
    p_mint.add_argument("--bottom-line", dest="bottom_line", default=None,
                        help="the §0.2 one-liner (default: the title)")
    p_mint.add_argument("--link", action="append", metavar="'verb target'",
                        help="a links: edge, e.g. --link 'refines H8' --link 'ties ldp:H5' (repeatable)")
    p_mint.add_argument("--prediction", default=None, help="the bet (H/Q)")
    p_mint.add_argument("--mechanism", default=None, help="why it would hold (H/Q)")
    p_mint.add_argument("--design", default=None, help="how it is tested (H/Q)")
    p_mint.add_argument("--context", default=None, help="what forced the choice (decisions)")
    p_mint.add_argument("--rejected", default=None, help="alternatives not taken (decisions)")
    p_mint.add_argument("--rationale", default=None, help="why this choice (decisions — required)")
    p_mint.add_argument("--yes", "-y", action="store_true", help="skip the confirm prompt")
    p_mint.set_defaults(func=_cmd_mint)

    p_ret = sub.add_parser("retract", help="retract a claim node (status→retracted, kept)")
    p_ret.add_argument("id", help="node id to retract, e.g. H12 or Q17")
    p_ret.add_argument("--project", default=None, help="project id (default: the current project)")
    p_ret.add_argument("--reason", default=None, help="short why (appended to the RETRACTED marker)")
    p_ret.add_argument("--supersede", default=None, metavar="NEW-ID",
                       help="the node that replaces this one (adds superseded-by/supersedes edges)")
    p_ret.add_argument("--yes", "-y", action="store_true", help="skip the confirm prompt")
    p_ret.set_defaults(func=_cmd_retract)

    p_ss = sub.add_parser("set-status", help="change a claim node's status")
    p_ss.add_argument("id", help="node id, e.g. Q13")
    p_ss.add_argument("status", help="new status (open/testing/supported/settled/answered/…)")
    p_ss.add_argument("--project", default=None, help="project id (default: the current project)")
    p_ss.add_argument("--note", default=None, help="short note prepended to the bottom-line")
    p_ss.add_argument("--yes", "-y", action="store_true", help="skip the confirm prompt")
    p_ss.set_defaults(func=_cmd_set_status)

    p_inbox = sub.add_parser("inbox",
                             help="surface this session's scheduler failure notices")
    p_inbox.add_argument("--session", default=None,
                         help="owner token (default: $LDP_SESSION, else 'default')")
    p_inbox.add_argument("--list", action="store_true",
                         help="print all notices (seen+unseen) without marking seen")
    p_inbox.add_argument("--surface", action="store_true",
                         help="print unseen notices and mark them seen (default)")
    p_inbox.set_defaults(func=_cmd_inbox)

    # Journal-tooling verbs — registered for `--help` visibility only; their args
    # are forwarded verbatim (see `main`), so no per-verb argparse here (argparse
    # REMAINDER drops leading-dash flags like `--check`).
    for verb, helptext in (
        ("gate", "run the journal claim/fact gate  [args forwarded]"),
        ("graph", "build/check the journal claim graph  [args forwarded]"),
        ("render", "render the journal to HTML  [args forwarded]"),
    ):
        sub.add_parser(verb, help=helptext, add_help=False)

    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Forward verbs bypass argparse so leading-dash args (e.g. `--check`) reach
    # the target module untouched.
    if argv and argv[0] in _FORWARD:
        return _run_as_main(_FORWARD[argv[0]], argv[1:])
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
