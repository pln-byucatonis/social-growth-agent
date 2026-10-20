"""Background loop: run the publisher every minute and refresh from the ops portal on a slower cadence.

Safe in dry-run (the publisher only simulates unless every live gate is open). Run standalone with
`python -m pln_agent worker`, or inside the web process with PLN_WORKER=on (one App Runner service).
"""
from __future__ import annotations

import logging
import threading
import time

from .config import Settings
from .db import DB
from .editorial import generate_pack
from .rights import evaluate
from .workflow import poll_statuses, run_publisher

log = logging.getLogger("pln_agent.worker")


PLAN_PER_TICK = 10  # bounded work per scheduler call; a backlog drains over successive ticks
_tick_lock = threading.Lock()


def plan_new_clear_assets(db: DB, settings: Settings, limit: int = PLAN_PER_TICK) -> int:
    """Generate posts for rights-clear assets that have none yet (never re-plans reviewed work).
    Highest-scoring clips first, at most `limit` per call."""
    planned = {r["asset_id"] for r in db.query("SELECT DISTINCT asset_id FROM variants")}
    todo = [a for a in db.all("assets") if a["asset_id"] not in planned and evaluate(a).ok]
    todo.sort(key=lambda a: -max((m.get("intensity", 0) for m in (a.get("moments") or [])), default=0))
    for a in todo[:limit]:
        generate_pack(db, a["asset_id"], settings, actor="worker")
    return min(len(todo), limit)


def tick(db: DB, settings: Settings, do_ops: bool) -> dict:
    """One pass. Overlapping calls (scheduler fires every minute) are skipped, not queued."""
    if not _tick_lock.acquire(blocking=False):
        return {"skipped": "previous tick still running", "planned": 0, "publisher": []}
    try:
        return _tick(db, settings, do_ops)
    finally:
        _tick_lock.release()


def _tick(db: DB, settings: Settings, do_ops: bool) -> dict:
    out = {"publisher": run_publisher(db, settings), "status": poll_statuses(db, settings)}
    if do_ops and settings.ops_database_url:
        from .ops_sources import import_ops_accounts, import_ops_clips
        ops = DB(settings.ops_database_url, init=False)
        out["ops_accounts"] = import_ops_accounts(db, ops, "worker")
        out["ops_clips"] = import_ops_clips(db, ops, settings.ops_assets_bucket, limit=200, min_score=60, actor="worker")
        out["intelligence"] = refresh_intelligence(db, settings, ops)
    out["planned"] = plan_new_clear_assets(db, settings)
    return out


def refresh_intelligence(db: DB, settings: Settings, ops: DB | None) -> dict:
    """Phase 1 loop: real performance in → creative evidence → cadence → knowledge review.
    Each step is isolated so one failure (e.g. a platform page timing out) doesn't stop the rest."""
    from . import audience, cadence, knowledge, scout
    steps = [("knowledge_seed", lambda: knowledge.seed(db, "worker")),
             ("social", lambda: audience.import_ops_social(db, ops, settings.timezone, "worker") if ops else None),
             ("pln_evidence", lambda: len(knowledge.derive_pln_evidence(db)["written"])),
             ("scout", lambda: scout.scan(db, ops, actor="worker") if ops else None),
             ("cadence", lambda: len(cadence.recommend_all(db))),
             ("knowledge_review", lambda: {k: v for k, v in knowledge.review_sources(db).items() if k != "ok"})]
    out = {}
    for name, fn in steps:
        try:
            r = fn()
            out[name] = {k: v for k, v in r.items() if k in ("accounts", "posts", "account_rows", "post_rows")} \
                if name == "social" and isinstance(r, dict) else r
        except Exception as e:  # noqa: BLE001 — logged and reported, loop continues
            log.exception("intelligence step %s failed", name)
            out[name] = {"error": str(e)[:300]}
    return out


def run(db: DB, settings: Settings, interval_s: int = 60, ops_every_s: int = 6 * 3600, stop: threading.Event | None = None):
    last_ops = 0.0
    while not (stop and stop.is_set()):
        try:
            due = time.time() - last_ops >= ops_every_s
            tick(db, settings, due)
            if due:
                last_ops = time.time()
        except Exception:  # keep the loop alive; errors land in the service log
            log.exception("worker tick failed")
        (stop.wait(interval_s) if stop else time.sleep(interval_s))


def start_background(db: DB, settings: Settings) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run, args=(db, settings), kwargs={"stop": stop}, daemon=True, name="pln-worker").start()
    return stop
