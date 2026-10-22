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
    from .community import send_approved
    out = {"publisher": run_publisher(db, settings), "status": poll_statuses(db, settings),
           "replies": len(send_approved(db, settings, actor="worker"))}
    if do_ops and settings.ops_database_url:
        from .ops_sources import import_ops_accounts, import_ops_clips
        ops = DB(settings.ops_database_url, init=False)
        out["ops_accounts"] = import_ops_accounts(db, ops, "worker")
        out["ops_clips"] = import_ops_clips(db, ops, settings.ops_assets_bucket, limit=200, min_score=60, actor="worker")
        out["intelligence"] = start_intelligence(settings)
    try:  # follower history can't be fetched after the fact: one snapshot a day for the priority accounts
        from .insights import snapshot_followers_due
        out["followers"] = snapshot_followers_due(db)
    except Exception as e:  # noqa: BLE001 — never let it stop the publisher loop
        out["followers"] = {"error": str(e)[:200]}
    try:  # Monday brief: YouTube feeds + market prices once a day, the brief on Monday morning
        from . import team, trend_brief
        out["trend_brief"] = trend_brief.run_due(db, llm=team._llm)
    except Exception as e:  # noqa: BLE001
        out["trend_brief"] = {"error": str(e)[:200]}
    out["planned"] = plan_new_clear_assets(db, settings)
    out["video"] = start_video_poll(settings)
    from .team_tasks import start as start_team_tasks
    out["team_tasks"] = start_team_tasks(settings)
    return out


_video_lock = threading.Lock()


def start_video_poll(settings: Settings) -> str:
    """Pick up finished ops verticals and render their cuts, off the request path (FFmpeg is slow)."""
    if _video_lock.locked():
        return "already running"

    def work():
        with _video_lock:
            try:
                from .ops_vertical import poll
                own = DB(settings.database_url, init=False)
                import os
                # with a task runner, renders happen there (App Runner throttles CPU between requests)
                r = poll(own, settings.ops_assets_bucket, settings, render=not os.environ.get("PLN_TASK_RUNNER_PROJECT"))
                if r["ready"]:
                    log.info("ops verticals ready: %s", r["ready"])
            except Exception:  # noqa: BLE001
                log.exception("ops vertical poll failed")

    threading.Thread(target=work, daemon=True, name="pln-video").start()
    return "started"


_intel_lock = threading.Lock()


def start_intelligence(settings: Settings) -> str:
    """Run the intelligence refresh in its own thread + connections: it's minutes of work (and CPU-bound
    statistics) that must not hold the scheduler's request open or block the publisher tick."""
    if _intel_lock.locked():
        return "already running"

    def work():
        with _intel_lock:
            own = DB(settings.database_url, init=False)
            ops = DB(settings.ops_database_url, init=False) if settings.ops_database_url else None
            res = refresh_intelligence(own, settings, ops)
            log.info("intelligence refresh: %s", res)

    threading.Thread(target=work, daemon=True, name="pln-intelligence").start()
    return "started"


def refresh_intelligence(db: DB, settings: Settings, ops: DB | None) -> dict:
    """Phase 1 loop: real performance in → creative evidence → cadence → knowledge review.
    Each step is isolated so one failure (e.g. a platform page timing out) doesn't stop the rest."""
    from . import audience, cadence, community, competitive, growth_lab, growth_officer, migration_tracking, insights, knowledge, publishing, scout, trend_scout
    def retire_verticals():  # one-off: move assets off the retired VerticalFn renditions (runs off the request path)
        if not ops or db.kv_get("migr:verticalfn_retired") == "done":
            return "done"
        from .ops_sources import repoint_retired_verticals
        r = repoint_retired_verticals(db, ops, settings.ops_assets_bucket, settings, "worker")
        db.kv_set("migr:verticalfn_retired", "done", "worker")
        return r

    steps = [("retired_verticals", retire_verticals),
             ("knowledge_seed", lambda: knowledge.seed(db, "worker")),
             ("social", lambda: audience.import_ops_social(db, ops, settings.timezone, "worker") if ops else None),
             ("instagram", lambda: insights.collect_instagram(db, actor="worker")),
             ("youtube", lambda: insights.collect_youtube(db, settings.timezone, actor="worker")),
             ("comments", lambda: {k: v for k, v in community.refresh(db, settings, actor="worker").items() if k in ("new", "errors")}),
             ("competitors", lambda: {"captured": len((r := competitive.snapshot(db, actor="worker"))["captured"]), "errors": r["errors"]}),
             ("pln_evidence", lambda: len(knowledge.derive_pln_evidence(db)["written"])),
             ("scout", lambda: scout.scan(db, ops, actor="worker") if ops else None),
             ("trends", lambda: trend_scout.refresh(db, actor="worker")),
             ("cadence", lambda: len(cadence.recommend_all(db))),
             ("slots", lambda: publishing.learn_all(db, settings.timezone)),
             ("experiments", lambda: growth_lab.refresh(db, "worker")),
             ("migration", lambda: len(migration_tracking.track(db, actor="worker")["alerts"])),
             ("growth_brief", lambda: growth_officer.generate_brief(db, "worker")["brief_id"] if growth_officer.due(db) else "current"),
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
