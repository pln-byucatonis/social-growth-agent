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


def plan_new_clear_assets(db: DB, settings: Settings) -> int:
    """Generate posts for rights-clear assets that have none yet (never re-plans reviewed work)."""
    n = 0
    for a in db.all("assets"):
        if evaluate(a).ok and not db.one("SELECT 1 AS x FROM variants WHERE asset_id=? LIMIT 1", [a["asset_id"]]):
            generate_pack(db, a["asset_id"], settings, actor="worker")
            n += 1
    return n


def tick(db: DB, settings: Settings, do_ops: bool) -> dict:
    out = {"publisher": run_publisher(db, settings), "status": poll_statuses(db, settings)}
    if do_ops and settings.ops_database_url:
        from .ops_sources import import_ops_accounts, import_ops_clips
        ops = DB(settings.ops_database_url, init=False)
        out["ops_accounts"] = import_ops_accounts(db, ops, "worker")
        out["ops_clips"] = import_ops_clips(db, ops, settings.ops_assets_bucket, limit=200, min_score=60, actor="worker")
    out["planned"] = plan_new_clear_assets(db, settings)
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
