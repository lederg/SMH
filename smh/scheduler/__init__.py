"""SMH scheduler core — the domain-agnostic queue/state machine.

The instance (LDP) keeps `scheduler/sched.py`, which owns the CLI, the GPU/SSH/
Docker execution (the `Runner`), and the co-located journal ledger; it imports
the portable primitives from here. This split is SMH-extraction E2b: the pieces
that any project's scheduler needs regardless of *how* a job runs live in
`smh.scheduler`, while `scheduler/sched.py` stays the LDP wiring.
"""
