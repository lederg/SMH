"""Packaged scheduler orchestration — the dispatch algorithm (SMH-extraction
E2b-4a).

This is the reusable *brain* of the tick: harvest finished work, poll capacity,
then the two placement passes (strict tightest-fit + affinity fallback) under the
commit-before-launch invariant, with ETA-gated preemption and the staging
fallbacks. It was lifted verbatim out of `scheduler/sched.py::_tick_body`; the
LDP instance keeps a thin `_tick_body()` wrapper that supplies the two seams.

Two collaborators, passed explicitly:
  - `sm`  — the instance binding: an object exposing the config/duty/store/event
            readers and the pure placement helpers as attributes
            (`load_config`, `compute_slots`, `queued_jobs`, `assign_slot`,
            `save_job`, `append_event`, `fit_multi`, …). LDP backs it with a
            proxy over the live `sched` module globals, so tests' monkeypatches
            are seen. It stands in for the (store, placer, duty/limits) trio the
            plan names — folded into one duck-typed namespace so the extraction
            stays behavior-preserving and the existing suite stays green.
  - `runner` — the GPU/SSH `Runner` (LDP: `GpuPoolRunner`): `harvest`, `stage_gc`,
            `capacity`, `dispatch`. The instance-specific execution backend.

NOTE (owed refinement): the staging queries (`_consumes_ready`,
`_producer_boxes`, `_fallback_producer_boxes`, `_warn_orphaned_consumer`) are
still reached through `sm.*` here; they belong on the runner (GpuPoolRunner
already exposes `consumes_ready`/`producer_boxes`/… for this) and should move to
`runner.*` in a later pass. Routing them via `sm` now keeps this commit a faithful
relocation. Likewise `compute_slots`/`assign_slot`/`_slot_eta_ok`/
`_pick_preemption` are core placement on the move-list but reached via `sm`.
"""
from __future__ import annotations

import glob
import json
import os


def tick_body(sm, runner):
    """One scheduling pass. `sm` supplies the config/store/placement seam; `runner`
    the GPU/SSH execution backend. Side-effecting: mutates queue/running state and
    dispatches lanes. Behavior-identical to the pre-extraction `_tick_body`."""
    cfg = sm.load_config()
    sm.process_schedule(cfg)            # apply due timed duty switches (before poll)
    sm.process_conditionals(cfg)        # evaluate idle-gated conditional switches
    sm.process_cancels(cfg)
    sm.process_preempts(cfg)            # kill+requeue-resumable (before harvest)
    has_work = (glob.glob(os.path.join(sm.ROOT, "queue", "*.json"))
                or glob.glob(os.path.join(sm.ROOT, "running", "*.json")))
    if has_work:
        drained = runner.harvest(cfg) or []   # finished -> done/failed; requeue lost.
                                         # F1: cards vacated THIS tick (occupant left
                                         # running/, GPU memory may still be draining so
                                         # capacity() can show them busy for a tick).
                                         # `or []` tolerates a runner whose harvest
                                         # returns None (older stubs / no terminal jobs).
        runner.stage_gc(cfg)             # reclaim CH disk: drop staged ckpts no
                                         # pending consumer needs (post-harvest, so
                                         # the just-finished consumer is counted out)
        snap = runner.capacity(cfg)      # fresh occupancy (only when there's work)
        running = sm._running_jobs()
        slots = sm.compute_slots(cfg, snap, running)
        duty = sm.load_duty()            # for the explicit off-duty pin guard below
        pcfg = cfg.get("preempt", {})
        calib = sm.estimate.load_calib()  # needed for runtime caps + preempt ETA
        secstep = sm.estimate.load_secstep()   # sec/step buckets for --cmd est
        schedule = sm.load_schedule()     # pending timed OFFs (refuse-window observ.)
        now = sm.now_iso()
        # SIZE-AWARE PLACEMENT (ISSUE_scheduler_speed_lookahead). Two independent,
        # config-gated knobs; both key off the same queued-job estimate and both
        # treat a cold-start job (no estimate -> None) exactly as today.
        lpt = cfg.get("lpt_dispatch", True)          # h1: longest-processing-first
        short_s = cfg.get("short_job_s", 1200)       # h3: below this = "short"

        def _queue_est(job):
            """Full est wall-seconds for a QUEUED job, or None if unestimable
            (cold-start). Pure arithmetic over the preloaded calib/secstep."""
            return sm._job_eta_seconds(job, "queue", calib, secstep)

        # H2 (regret check, off by default): when J's only fit is a slower card
        # while a faster card is busy, decide settle-now vs wait-for-fast. Single-
        # card, unpinned, non-short heavy jobs only. Deferral just leaves J queued;
        # it re-evaluates every tick, so a stalled occupant never wedges it.
        # PER-CARD DRAIN CAP (2026-07-15, hardened 2026-07-16): a busy fast card frees
        # ONE slot per cycle, so only ONE queued job may defer onto each fitting fast
        # card per tick. Additional heavy jobs settle on free slow cards instead of all
        # N piling onto a single card while slow cards idle (starving-card bug). The
        # cap is enforced directly in _regret_defer (reserved>=1 -> card excluded); the
        # earlier per-runtime contention pricing was too weak once the slow card ran
        # >2x slower than the fast one (real a4000 ≈ 4.3x h100).
        regret_on = cfg.get("regret_check", False)
        regret_margin = cfg.get("regret_margin", 1.0)
        regret_cap = cfg.get("regret_max_wait_ratio", 2.0)
        gpu_speed = cfg.get("gpu_speed", {})

        def _type_rank(gtype):
            g = (gtype or "").lower()
            return gpu_speed[g] if g in gpu_speed else sm._speed_rank({"gpu_type": g})

        def _est_on(job, gtype):
            """J's wall-seconds ON a given gpu_type — the real per-type calibrated
            estimate (FLOP proxy, else declared sec/step bucket). None if neither.
            F2: pass gpu_speed so a flat `minutes` hint is scaled per card — else it
            is gpu-type-blind and est_slow==est_fast collapses regret's cost_settle."""
            feat = job.get("cost_features") or {}
            t = sm.estimate.est_seconds(feat, gtype, calib=calib)
            if t is None:
                t = sm.estimate.declared_seconds(job.get("est"), gtype,
                                                 tag=job.get("tag"), secstep=secstep,
                                                 gpu_speed=gpu_speed)
            return t

        def _card_total(remote_name):
            b = sm._box_by_name(cfg, remote_name)
            return (b or {}).get("card_total_gb", 0) if b else 0

        def _regret_defer(job, fit, need, reserved):
            """The faster card key `(remote_name, gpu)` J should wait for instead of
            settling on `fit` (a slower card) this tick, else None. cost_settle =
            extra runtime on the slow card; cost_wait = soonest a fitting faster card
            frees PLUS the runtime of jobs already deferred onto that card THIS tick
            (`reserved`). A fast card serves ONE waiter before it's busy again, so the
            k-th job queued to it waits ~k job-runtimes, not one occupant ETA — the
            contention term is what stops N queued jobs all deferring onto a single
            fast card while free slow cards idle (starving-card bug, 2026-07-15).
            Defer iff waiting is cheaper than the slow-card penalty (margin-scaled)."""
            slot = fit[0]
            chosen_rank = sm._speed_rank(slot)
            slow_type = slot.get("gpu_type")
            # candidate faster cards fitting J, each (key, gpu_type, base_eta):
            # BUSY cards (finite ETA) + F1 drain-race cards (freeing NOW, base 0).
            cands = []
            for j in running:
                a = j.get("assigned") or {}
                if _type_rank(a.get("gpu_type")) <= chosen_rank:
                    continue                      # not strictly faster than settle
                if _card_total(a.get("remote_name")) < need:
                    continue                      # freed card still won't fit J
                eta = sm._job_eta_seconds(j, "running", calib, secstep)
                if eta is None:                   # unestimable occupant -> don't wait
                    continue
                cands.append(((a.get("remote_name"), a.get("gpu")),
                              a.get("gpu_type"), eta))
            # F1 (drain race): a faster card whose occupant left running THIS tick is
            # freeing NOW, but its GPU memory may not be reclaimed until the next poll
            # so it's absent from both `slots` (still shows busy) and `running` (gone).
            # Treat it as a busy-faster card with base ETA 0 so J keeps deferring one
            # tick onto it rather than settling on the slow card in the blind drain window.
            for a in drained:
                if _type_rank(a.get("gpu_type")) <= chosen_rank:
                    continue                      # not strictly faster than settle
                if _card_total(a.get("remote_name")) < need:
                    continue                      # freed card still won't fit J
                cands.append(((a.get("remote_name"), a.get("gpu")),
                              a.get("gpu_type"), 0.0))
            # PER-CARD DRAIN CAP (starving-free-card fix, 2026-07-16). A busy fast
            # card frees exactly ONE slot this cycle, so it can absorb only ONE
            # deferred job per tick; once a higher-priority job has claimed that slot
            # (reserved>=1) the next waiter can't inherit the card until the first
            # runs a FULL job-runtime on it. The earlier contention term
            # (base + reserved*job_len) tried to price that in, but it is too weak
            # when the slow card is much slower than the fast one: it defers iff
            # est_slow >= base + (reserved+1)*est_fast, and at the REAL a4000 ≈ 4.3x
            # h100 ratio (7.48 vs 1.74 s/step) that still holds for the 2nd/3rd/4th
            # job — so N heavy jobs all pile onto ONE h100 while free A100s idle and a
            # deep queue starves (the 2026-07-16 recurrence; the 2x gpu_speed default
            # hid it from the old contention math). Enforce the stated intent (only
            # ONE defer per fitting fast card per tick) as a HARD cap: skip a card that
            # already has its one waiter. The job then settles on a free slow card
            # (`fit` is non-empty whenever regret runs) or waits on another
            # zero-reserved fast card. Re-evaluated every tick, so a genuinely
            # fast-draining card just picks up the next waiter fresh next tick.
            best_wait, best_key, fast_type = None, None, None
            for key, gtype, base in cands:
                if reserved.get(key, 0) >= 1:
                    continue                      # card's one drain slot already taken
                if best_wait is None or base < best_wait:
                    best_wait, best_key, fast_type = base, key, gtype
            if best_wait is None:
                return None                       # no free-to-drain faster card -> settle
            est_slow, est_fast = _est_on(job, slow_type), _est_on(job, fast_type)
            if est_slow is not None and est_fast is not None:
                cost_settle, cap_ref = max(est_slow - est_fast, 0.0), est_slow
            else:                                 # rank-ratio fallback (doc form)
                est_any = est_slow or est_fast or _queue_est(job)
                if est_any is None or chosen_rank <= 0:
                    return None                   # can't estimate -> never block
                cost_settle = est_any * (_type_rank(fast_type) / chosen_rank - 1)
                cap_ref = est_any
            if best_wait > regret_cap * cap_ref:
                return None                       # fast card too far off -> settle
            return best_key if cost_settle >= best_wait * regret_margin else None

        preempted = set()                # victims requested this tick (anti-thrash)
        regret_wait = {}                 # (remote,gpu) -> jobs deferred waiting on it
        placed_ct = {}                   # (remote,gpu) -> new jobs placed this tick
        per_gpu_cap = cfg.get("max_new_jobs_per_gpu_tick", 1)

        def _place(job, path, picks, extra=None):
            # COMMIT-BEFORE-LAUNCH: move the job queue/ -> running/ first, so it
            # is never both queued and launched. A crash/timeout during the launch
            # can therefore never leave a launched lane while the job is still
            # re-dispatchable. assign+save+remove is the atomic claim; only then
            # do we launch. A launch failure leaves the job in running/ with no
            # lane -> the next harvest marks it lost and requeues it (self-heal).
            if isinstance(picks, dict):
                picks = [picks]
            sm.assign_slot(job, picks)
            sm.save_job(job, "running"); os.remove(path)
            for s in picks:
                s["avail_gb"] -= need     # reserve every assigned card this tick
                # COMPUTE THROTTLE (cold-start): cap NEW jobs per GPU per tick so a
                # fully-idle card (util reads 0 at tick start, before any job ramps)
                # isn't memory-packed all at once. Zeroing avail_gb drops the slot
                # from further fits this tick; next tick the util gate takes over.
                k = (s["remote_name"], s["gpu"])
                placed_ct[k] = placed_ct.get(k, 0) + 1
                if placed_ct[k] >= per_gpu_cap:
                    s["avail_gb"] = 0.0
            if runner.dispatch(job, cfg):
                first = picks[0]
                where = f"{first['remote_name']}:" + ",".join(
                    str(s["gpu"]) for s in picks)
                sm.append_event({"ev": "dispatch", "id": job["id"], "to": where,
                                 **(extra or {})})

        # PASS 1 — strict: urgent first; tightest-fit slot (prefer empty>mine>
        # foreign), honoring optional per-job gpu-type affinity.
        # H1 (LPT): within a priority band, walk LONGEST-est-first so the heavy
        # train claims a fast card before short jobs fill the gaps. queued_jobs()
        # is already (priority, created)-ordered; re-sort inserts -est between them
        # (created stays the stable final tiebreak). Cold-start est None -> 0 ->
        # sorts as short, so a queue of only unestimable jobs keeps submit order.
        pass1 = sm.queued_jobs()
        if lpt:
            pass1.sort(key=lambda jp: (jp[0]["priority"],
                                       -(_queue_est(jp[0]) or 0.0),
                                       jp[0]["created"]))
        for job, path in pass1:
            if not sm.deps_done(job):
                continue
            prod_pin = None    # staging-deadlock fallback: box(es) that already
                               # hold this unpinned consumer's not-yet-CH inputs
            if not job.get("remote") and not sm._consumes_ready(job, cfg):
                # Inputs aren't in the CH tree. The old gate deferred forever,
                # deadlocking consumers of artifacts made before the staging layer
                # (or never declared as `produces`) — they live only on their box,
                # so no producer will re-stage them CH-ward (ISSUE_staging_
                # consumes_deadlock). Fall back to the producer box: run where the
                # artifact already is (no CH round-trip, no CH disk cost). Only a
                # truly-orphaned artifact (not in CH, no producer box) stays
                # blocked — and loudly, so idle GPUs aren't mistaken for idle work.
                # An explicit --remote pin asserts box-local input, bypassing this.
                prod_pin = sm._fallback_producer_boxes(job, cfg)
                if not prod_pin:
                    sm._warn_orphaned_consumer(job)
                    continue
                # Mesh widen (2026-07-22): the hard producer pin predates the
                # direct mesh. When EVERY missing input is box->box reachable
                # (_direct_covers), any direct-capable box can be staged at
                # launch (_stage_direct ships it src-box -> dest at ~90 MB/s;
                # note a consumed path INSIDE interval/ transfers fine — the
                # interval/ exclusion is transfer-root-relative). Without this,
                # consumers pile onto a busy producer box while mesh boxes idle.
                if sm._direct_on(cfg) and sm._direct_covers(job, cfg):
                    prod_pin = prod_pin | {
                        b["remote"] for b in cfg.get("pool", [])
                        if sm._direct_capable(b)}
            need = job.get("min_gpu_gb") or cfg["default_job_gb"]
            n_gpu = int(job.get("n_gpu", 1) or 1)
            # drop slots whose runtime cap this job's estimate can't meet
            # (short-only mode), then fit normally.
            elig = [s for s in slots if sm._slot_eta_ok(s, job, calib, secstep)]
            # observability (handoff refuse_scheduled_off): a pending timed OFF
            # shrank a fitting box's runtime cap below this job's est, so it can't
            # land there before the box drains. Emit per (job, box), once per tick
            # (mirrors regret_defer's cadence). Does NOT change placement — the
            # cap already dropped the slot from `elig` above.
            if schedule.get("switches"):
                for box, est_s, cap_s in sm.scheduled_off_refusals(
                        job, slots, need, calib, schedule, cfg, now, secstep):
                    sm.append_event({"ev": "refuse_scheduled_off", "id": job["id"],
                                     "box": box, "est": (round(est_s)
                                                         if est_s is not None else None),
                                     "remaining": round(cap_s)})
            pin = job.get("remote")
            if pin:                              # explicit --remote: hard box-pin.
                elig = [s for s in elig          # input is box-local (e.g. a box-
                        if s.get("remote") == pin]   # local ckpt) -> MUST land here.
                # belt-and-suspenders: a pin must NOT override off-duty. compute_slots
                # already drops off-duty cards (state=="off"), but assert it here so an
                # explicit --remote can never smuggle a job onto an off-duty box:gpu.
                elig = [s for s in elig
                        if not sm.is_off_duty(duty, s["remote_name"], s["gpu"], cfg=cfg)]
            elif prod_pin:                       # staging fallback: consumed inputs
                elig = [s for s in elig          # live ONLY on the producer box(es),
                        if s.get("remote") in prod_pin]  # not CH -> MUST land there
                                                 # (no CH push; artifact already local)
            elif sm._staging_on(cfg) and job.get("consumes"):
                # staged inputs already in the CH tree (gate above passed) -> may
                # run on ANY box. Soft-prefer a box that ALREADY HOLDS the consumed
                # artifact, to skip a needless cross-box push; else leave elig open
                # and the consumed artifacts get pushed to whichever box wins at
                # launch. Two locality signals: the producer box (from done-records)
                # and boxes with a complete copy on disk NOW (Fix A, incident
                # 2026-07-12 — the CH uplink is ~3.4 MB/s, so a cold multi-GB push
                # overruns the 600s stage cap and the lane dies `no_sentinel`;
                # running where the ckpt lives skips the push). The on-disk check
                # (one ssh-stat/box) also credits out-of-band pre-stages and drops a
                # producer box whose copy is gone. `prefer_staged_box` gates it (the
                # ssh cost); default ON.
                prod = sm._producer_boxes(job, cfg)
                cheap = (sm._boxes_cheap_stage(job, cfg)
                         if cfg.get("prefer_staged_box", True) else set())
                pref = [s for s in elig
                        if s.get("remote") in prod or s.get("remote_name") in cheap]
                if (not cheap and cfg.get("async_staging")
                        and sm._cold_push_would_die(job, cfg)):
                    # Fix B (default-off): NO on-duty box can stage this large ckpt
                    # cheaply (a true orphan — e.g. produced on a since-off box), and
                    # a cold push at launch would overrun the 600s cap and die. Push
                    # it in the BACKGROUND (uncapped, off the tick path) and defer
                    # placing until the copy lands; a later tick then runs it via the
                    # guard below. incident 2026-07-12.
                    sm._async_stage_step(job, elig, cfg)
                    continue
                if cheap and sm._cold_push_would_die(job, cfg):
                    # HARD locality gate: some on-duty box can stage this large ckpt
                    # CHEAPLY (already holds the weights; delta ~KB), but cold-pushing
                    # it to a box that lacks it would overrun the 600s stage cap and
                    # die `no_sentinel` (the ~3.4 MB/s CH uplink, incident 2026-07-12).
                    # So restrict to the cheap boxes and DEFER if they're busy —
                    # waiting for one to free strictly beats a push that's doomed. If
                    # every cheap box is busy this tick, `pref` is empty -> `fit`
                    # empty -> the job stays queued and retries a later tick when one
                    # frees (self-heal, no death), instead of cold-pushing elsewhere.
                    elig = pref
                elif pref and sm.fit_multi(pref, need, n_gpu, job.get("affinity")):
                    # soft preference: small/fast-to-push artifact — prefer a cheap
                    # box if one fits now, else fall through to any box (push at launch).
                    elig = pref
            # H3: a SHORT job (known est below short_s) inverts speed preference —
            # take a slower card first, leaving fast cards open for heavy trains.
            # Cold-start (est None) is never "short" -> faster-first, as today.
            est = _queue_est(job)
            prefer_slow = est is not None and est < short_s
            fit = sm.fit_multi(elig, need, n_gpu, job.get("affinity"),
                               prefer_slow=prefer_slow)
            if not fit:
                # no slot: optionally free one by preempting a lower-priority
                # resumable running job (ETA-gated). Placement happens next tick,
                # after process_preempts frees the slot. §7-step-4.
                if (pcfg.get("auto")
                        and len(preempted) < pcfg.get("max_per_tick", 1)):
                    cand = [j for j in running
                            if j["id"] not in preempted
                            and j["id"] != job["id"]]
                    victim = sm._pick_preemption(job, need, cand, slots, cfg, calib)
                    if victim:
                        sm.atomic_write(os.path.join(sm.ROOT, "preempt", victim["id"]),
                                        json.dumps({"by": "auto-sched", "at": sm.now_iso(),
                                                    "reason": f"freeing slot for {job['id']}"}))
                        preempted.add(victim["id"])
                        sm.append_event({"ev": "preempt_request", "id": victim["id"],
                                         "by": "auto-sched", "for": job["id"]})
                continue
            # H2: heavy job settling on a slow card while a fast one is busy — wait
            # instead if the regret math says so. Single-card, unpinned, non-short,
            # estimable jobs only (cold-start places greedily — never block blind).
            if (regret_on and n_gpu == 1 and not prefer_slow
                    and not pin and not prod_pin and not job.get("affinity")
                    and est is not None):
                wait_key = _regret_defer(job, fit, need, regret_wait)
                if wait_key is not None:
                    ahead = regret_wait.get(wait_key, 0)   # jobs already in this line
                    regret_wait[wait_key] = ahead + 1      # J now claims a slot in it
                    sm.append_event({"ev": "regret_defer", "id": job["id"],
                                     "settled_on": fit[0].get("gpu_type"),
                                     "waiting_for": f"{wait_key[0]}:{wait_key[1]}",
                                     "queued_ahead": ahead})
                    continue
            _place(job, path, fit)

        # PASS 2 — affinity fallback: a job still queued only because its
        # preferred gpu_type is busy may run on an off-type card, BUT only onto a
        # slot pass 1 left unclaimed (i.e. no strictly-eligible job wanted it) and
        # only if that slot is genuinely IDLE (state==empty) — never co-locate on
        # a busy card just to dodge affinity. This is the "only affinity-blocked
        # jobs remain, GPUs sit idle" case; running off-first-choice beats idling.
        if cfg.get("affinity_fallback", True):
            for job, path in sm.queued_jobs():     # re-scan: pass 1 removed placed jobs
                aff = job.get("affinity")
                if not aff or not sm.deps_done(job):
                    continue
                if job.get("remote"):            # hard box-pin: never relax off its
                    continue                     # box (its input lives only there)
                prod_pin = None
                if not sm._consumes_ready(job, cfg):   # unpinned consumer, inputs not
                    prod_pin = sm._fallback_producer_boxes(job, cfg)  # in CH -> only a
                    if not prod_pin:               # producer box will do (else
                        continue                   # orphaned: pass 1 already warned)
                    if sm._direct_on(cfg) and sm._direct_covers(job, cfg):
                        prod_pin = prod_pin | {    # mesh widen (see pass 1)
                            b["remote"] for b in cfg.get("pool", [])
                            if sm._direct_capable(b)}
                if int(job.get("n_gpu", 1) or 1) > 1:
                    continue          # multi-GPU jobs only place in strict pass 1
                need = job.get("min_gpu_gb") or cfg["default_job_gb"]
                idle = [s for s in sm.fit_slots(slots, need, None)
                        if s["state"] == "empty" and sm._slot_eta_ok(s, job, calib, secstep)
                        and (prod_pin is None or s.get("remote") in prod_pin)]
                if not idle:
                    continue
                slot = idle[0]
                sm.append_event({"ev": "affinity_relaxed", "id": job["id"],
                                 "want": aff, "ran_on": slot.get("gpu_type"),
                                 "to": f"{slot['remote_name']}:{slot['gpu']}"})
                _place(job, path, slot,
                       {"affinity_relaxed": aff, "ran_on": slot.get("gpu_type")})
    sm.render_status_md()
    try:
        sm._maybe_repro_check()         # daily drift check; never wedge the tick
    except Exception as e:
        sm.append_event({"ev": "repro_error", "err": str(e)})
