"""SMH adapter 2 — Experiment runner.

Core keeps the queue, the flock-serialized state machine, deps/retries, and the
journal-claim pre-run gate (the differentiator). The instance supplies *where and
how* a dispatched job actually executes. LDP's remote-GPU pool is one `Runner`
impl (`GpuPoolRunner`); a non-GPU project supplies its own (local process, CI
job, cloud batch).

INTERFACES ONLY — no implementation this round. See
`docs/DESIGN_adapter_contracts.md` (Adapter 2) for the core/instance split, the
`cmd_submit` split line, and the FracBits + far-consumer validation.

LDP today satisfies this implicitly in `scheduler/sched.py`: core =
`next_id`/`save_job`/`all_jobs`/`find_job` + `_tick_body`/`cmd_tick` + the claim
gate (`_resolve_goal`, `_journal_claim_nodes`, `goals.load_goal`); instance =
`poll_remotes`/`compute_slots`/`fit_multi`/`assign_slot`/`launch_lane` + SSH/
Docker/`remotes.json`. The GPU resource fields (`min_gpu_gb`,`affinity`,`n_gpu`)
move out of the typed job dict into `JobSpec.resource_hints`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class JobSpec:
    """Domain-agnostic job description. The claim tie (`goal`) and `deps`/retries
    are core; `resource_hints` is an opaque bag the chosen `Runner` interprets
    (GPU puts `min_gpu_gb`/`affinity`/`n_gpu` here; a local runner ignores it)."""

    id: str
    config_ref: str | None           # config name/path, or None for an inline command
    cmd: str | None                  # inline command (mutually exclusive with config_ref)
    tag: str                         # run identifier (drives the log path)
    deps: list[str] = field(default_factory=list)
    max_retries: int = 0
    priority: int = 50               # 0 = urgent .. 99 = idle
    goal: dict | None = None         # {kind, claim, text} — validated by the core ClaimGate
    meta: dict = field(default_factory=dict)
    resource_hints: dict = field(default_factory=dict)   # runner-defined (GPU: min_gpu_gb/affinity/n_gpu)


# Opaque to core — each Runner defines its own shapes.
Capacity = Any        # GPU: poll_remotes + compute_slots snapshot; local: free-worker count
Placement = Any       # GPU: chosen slot(s); local: a worker handle
Handle = Any          # runner-private dispatch handle (GPU: assigned slot + pgid; local: PID)


@dataclass
class Status:
    """Where a launched job stands, in core's vocabulary."""

    state: str        # "queued" | "running" | "done" | "failed" | "lost"
    detail: str = ""  # runner-specific note (e.g. ssh-unreachable, exit code)


@dataclass
class Result:
    """Terminal outcome core records and may feed back to the journal."""

    metrics: dict           # domain-specific (LDP: {eval_loss, realized_b2, ...})
    log_path: str
    exit: int | None = None


@runtime_checkable
class Runner(Protocol):
    """Execution backend. LDP is a *pull* scheduler (core owns the queue), so the
    Runner owns capacity, placement, launch, and liveness — richer than plain
    submit/poll. `GpuPoolRunner` implements all of this over SSH; a
    `LocalProcessRunner` would back each verb with `subprocess`."""

    def capacity(self) -> Capacity:
        """Current free capacity. GPU: `poll_remotes` + `compute_slots`."""
        ...

    def place(self, spec: JobSpec, cap: Capacity) -> Placement | None:
        """Choose where `spec` runs given capacity, honoring `resource_hints`.
        Return None if it cannot be placed right now (core leaves it queued).
        GPU: `fit_multi`/`fit_slots`."""
        ...

    def launch(self, spec: JobSpec, placement: Placement) -> Handle:
        """Start the job at `placement`; return a handle to track it.
        GPU: `assign_slot` + `launch_lane` (setsid ssh dispatch)."""
        ...

    def poll(self, handle: Handle) -> Status:
        """Liveness/terminal check. GPU: pgid/tmux liveness from `harvest`."""
        ...

    def result(self, handle: Handle) -> Result:
        """Collect metrics + log for a terminal job. GPU: pull log + parse betas."""
        ...

    def cancel(self, handle: Handle) -> None:
        """Stop a running/queued job. GPU: kill the lane on its box."""
        ...


@runtime_checkable
class ClaimGate(Protocol):
    """The journal-claim pre-run gate, factored OUT of `Runner` on purpose — it is
    journal-coupled, not runner-coupled, so a project keeps it even with a trivial
    runner. Core's submit path calls this before a job is queued.

    LDP impl: `_resolve_goal` + `_journal_claim_nodes` + `goals.load_goal`. A job
    must name an *open* claim; closed/unknown claims are refused."""

    def validate(self, spec: JobSpec) -> None:
        """Raise/reject if `spec.goal` does not tie to an open journal claim;
        return cleanly otherwise."""
        ...


# ---------------------------------------------------------------------------
# LocalRunner — a non-GPU reference backend (SMH extraction E0).
#
# The simplest thing that satisfies `Runner`: run `spec.cmd` in a local
# subprocess. It exists so a stranger's SMH project has a working runner out of
# the box (no SSH/Docker/GPU pool) and so the Protocol has an executable witness
# to test against. Capacity/placement are trivial (one local slot, always
# placeable). Nothing live imports this module, so adding it is purely additive.
# ---------------------------------------------------------------------------
import os
import signal
import subprocess


@dataclass
class LocalHandle:
    """Private dispatch handle for a `LocalRunner` job."""

    proc: subprocess.Popen
    tag: str
    log_path: str


class LocalRunner:
    """Structural `Runner` over `subprocess`. `spec.cmd` is launched with the
    shell; stdout+stderr are captured to `<log_dir>/<tag>.log`. `config_ref`
    (config-file jobs) is out of scope for this reference backend — supply a
    `cmd`."""

    def __init__(self, log_dir: str = "."):
        self.log_dir = log_dir

    def capacity(self):
        """One local slot, always free (the OS schedules the actual process)."""
        return 1

    def place(self, spec: JobSpec, cap):
        """Trivially placeable while any capacity remains; else leave it queued."""
        return "local" if cap else None

    def launch(self, spec: JobSpec, placement) -> LocalHandle:
        """Start `spec.cmd` in a subprocess, capturing output to the tag's log."""
        if not spec.cmd:
            raise ValueError("LocalRunner requires spec.cmd (config_ref not supported)")
        os.makedirs(self.log_dir, exist_ok=True)
        log_path = os.path.join(self.log_dir, f"{spec.tag}.log")
        log = open(log_path, "wb")
        # New session so `cancel` can signal the whole process group.
        proc = subprocess.Popen(
            spec.cmd, shell=True, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc._smh_log = log  # keep the fd alive with the handle
        return LocalHandle(proc=proc, tag=spec.tag, log_path=log_path)

    def poll(self, handle: LocalHandle) -> Status:
        """running until the process exits; then done (rc 0) / failed (rc != 0)."""
        rc = handle.proc.poll()
        if rc is None:
            return Status(state="running")
        if rc == 0:
            return Status(state="done", detail="exit 0")
        return Status(state="failed", detail=f"exit {rc}")

    def result(self, handle: LocalHandle) -> Result:
        """Collect exit code + captured log for a terminal job."""
        rc = handle.proc.wait()
        log = getattr(handle.proc, "_smh_log", None)
        if log is not None and not log.closed:
            log.close()
        return Result(metrics={"exit": rc}, log_path=handle.log_path, exit=rc)

    def cancel(self, handle: LocalHandle) -> None:
        """Terminate the job's process group; ignore if already gone."""
        try:
            os.killpg(os.getpgid(handle.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
