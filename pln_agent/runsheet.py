"""Run sheet: one day's posts, by time, so a reviewer can lock tomorrow in the night before.

Two kinds of rows:
* scheduled — already approved and queued, with the publish job's state (and whether it will go out live
  or simulated);
* proposed — what the calendar planner would post that day from drafts in review / approved, at each
  account's best learned window (or its starting slot), with why that time was picked.

Locking a proposed row approves it (when the reviewer may) and schedules it at the proposed time; content that
needs a second approver stays in review and says so. Unscheduling puts a post back to approved.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .accounts import publish_eligibility
from .config import Settings
from .db import DB, now_iso


def _local(ts: str, tz: ZoneInfo) -> datetime:
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(tz)


def focus_accounts(db: DB) -> dict:
    """Worked accounts (config/focus.json) split into connected (the run sheet proposes for these) and not yet."""
    import json
    from .config import CONFIG_DIR
    try:
        cfg = json.loads((CONFIG_DIR / "focus.json").read_text())
    except FileNotFoundError:
        return {}
    accs = {a["account_id"]: a for a in db.all("accounts")}
    worked = [accs[a] for a in cfg.get("worked") or [] if a in accs]
    return {"connected": [a["account_id"] for a in worked if a.get("verification_status") == "oauth_connected"],
            "waiting": [a["account_id"] for a in worked if a.get("verification_status") != "oauth_connected"]}


def day_sheet(db: DB, settings: Settings, day: date, focus: bool = True) -> dict:
    """focus: the planner proposes only for the worked accounts that are connected (config/focus.json); scheduled
    posts and team-challenge pins always show."""
    from .calendar_planner import plan_week, slot_hypotheses
    from .publishing import account_slots
    from .workflow import kill_switch_on
    tz = ZoneInfo(settings.timezone)
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    accs = {a["account_id"]: a for a in db.all("accounts")}
    ks = kill_switch_on(db)
    rows = []
    taken: dict[str, list[datetime]] = {}
    for s in db.all("schedules", "state IN ('scheduled','done','missed') AND slot_at >= ? AND slot_at < ?",
                    [start.replace(microsecond=0).isoformat(), end.replace(microsecond=0).isoformat()]):
        v = db.get("variants", "variant_id", s["variant_id"]) or {}
        job = (db.query("SELECT state, mode, last_error FROM publish_jobs WHERE variant_id=? ORDER BY created_at DESC", [s["variant_id"]]) or [{}])[0]
        acc = accs.get(s["account_id"], {})
        live, why_not = publish_eligibility(acc, settings, ks) if acc else (False, ["unknown account"])
        t = _local(s["slot_at"], tz)
        taken.setdefault(s["account_id"], []).append(t)
        rows.append({"kind": "scheduled", "time": t, "variant_id": s["variant_id"], "account_id": s["account_id"],
                     "handle": acc.get("username_original") or s["account_id"], "platform": acc.get("platform", ""),
                     "brand_id": acc.get("brand_id", ""), "hook": v.get("hook", ""), "state": v.get("state", ""),
                     "job": job.get("state"), "mode": "live" if live else "simulated", "why_not_live": why_not,
                     "error": job.get("last_error"), "qc": (v.get("render_qc") or {}).get("passed")})
    # posts a team challenge's schedule task pinned to this day (you lock them like any proposal)
    from .team_tasks import pins_for_day
    pinned = set()
    for p in pins_for_day(db, day.isoformat(), settings.timezone):
        v = db.get("variants", "variant_id", p["variant_id"]) or {}
        if v.get("state") in ("scheduled", "publishing", "published", "simulated"):
            continue
        acc = accs.get(p["account_id"], {})
        live, why_not = publish_eligibility(acc, settings, ks) if acc else (False, [])
        t = _local(p["slot_at"], tz)
        pinned.add(p["variant_id"])
        taken.setdefault(p["account_id"], []).append(t)
        flags = (v.get("risk_flags") or {})
        rows.append({"kind": "proposed", "pinned": True, "task_id": p["task_id"], "thread_id": p["thread_id"], "time": t,
                     "variant_id": p["variant_id"], "account_id": p["account_id"], "handle": acc.get("username_original") or p["account_id"],
                     "platform": acc.get("platform", ""), "brand_id": acc.get("brand_id", ""), "hook": v.get("hook", ""),
                     "state": v.get("state", ""), "route_role": v.get("route_role", ""),
                     "why_time": f"Team challenge, post {p['n']}" + (f" — {p['moved']}" if p.get("moved") else ""),
                     "mode": "live" if live else "simulated", "why_not_live": why_not, "two_person": bool(flags.get("two_person_signoff")),
                     "qc": (v.get("render_qc") or {}).get("passed"), "render": v.get("render_status")})
    fa = focus_accounts(db) if focus else {}
    keep = set(fa.get("connected") or []) if fa else None
    if keep is not None:
        # scheduled posts and a team challenge's pins are commitments already made: they always show
        rows = [r for r in rows if r["kind"] == "scheduled" or r.get("pinned") or r["account_id"] in keep]
    plan = plan_week(db, settings, day, days=1, states=("in_review", "approved"), persist=False)
    hyp = slot_hypotheses()
    for p in plan["placed"]:
        t = p["slot"].astimezone(tz)
        if p["variant_id"] in pinned or (keep is not None and p["account_id"] not in keep):
            continue
        if any(abs((t - x).total_seconds()) < 3600 for x in taken.get(p["account_id"], [])):
            continue  # an already-scheduled post holds that hour
        v = db.get("variants", "variant_id", p["variant_id"]) or {}
        acc = accs.get(p["account_id"], {})
        _, why = account_slots(db, p["account_id"], p["platform"], hyp)
        live, why_not = publish_eligibility(acc, settings, ks) if acc else (False, [])
        flags = (v.get("risk_flags") or {})
        rows.append({"kind": "proposed", "time": t, "variant_id": p["variant_id"], "account_id": p["account_id"],
                     "handle": acc.get("username_original") or p["account_id"], "platform": p["platform"],
                     "brand_id": acc.get("brand_id", ""), "hook": p["hook"], "state": p["state"], "score": p.get("score"),
                     "route_role": p["route_role"], "why_time": why, "mode": "live" if live else "simulated",
                     "why_not_live": why_not, "two_person": bool(flags.get("two_person_signoff")),
                     "qc": (v.get("render_qc") or {}).get("passed"), "render": v.get("render_status")})
    rows.sort(key=lambda r: (r["time"], r["handle"]))
    return {"day": day.isoformat(), "timezone": settings.timezone, "rows": rows, "unplaced": plan["unplaced"][:40], "focus": fa,
            "counts": {"scheduled": sum(r["kind"] == "scheduled" for r in rows), "proposed": sum(r["kind"] == "proposed" for r in rows),
                       "pinned": len(pinned)}}


def lock(db: DB, picks: list[tuple[str, str]], reviewer: str) -> dict:
    """picks: (variant_id, ISO slot). Approve where needed, then schedule at the slot."""
    from .workflow import WorkflowError, approve, schedule
    out = {"scheduled": [], "needs_second_approver": [], "failed": []}
    for vid, slot in picks:
        try:
            v = db.get("variants", "variant_id", vid)
            if v and v["state"] == "in_review":
                r = approve(db, vid, reviewer, note="run sheet")
                if r["state"] != "approved":
                    out["needs_second_approver"].append(vid)
                    continue
            schedule(db, vid, datetime.fromisoformat(slot), reviewer)
            out["scheduled"].append(vid)
        except (WorkflowError, KeyError, ValueError) as e:
            out["failed"].append({"variant_id": vid, "error": str(e)[:200]})
    db.audit(reviewer, "runsheet_locked", "schedules", "", {k: len(v) for k, v in out.items()})
    return out


def unschedule(db: DB, vid: str, actor: str) -> None:
    """Take a queued post off the schedule; it goes back to approved and keeps its approvals."""
    from .workflow import WorkflowError
    v = db.get("variants", "variant_id", vid)
    if not v or v["state"] != "scheduled":
        raise WorkflowError("only a scheduled post can be unscheduled")
    for j in db.all("publish_jobs", "variant_id=? AND state IN ('queued','halted')", [vid]):
        # free the idempotency key (unique) so the same post can be scheduled again later
        db.update("publish_jobs", "job_id", j["job_id"], {"state": "cancelled", "last_error": "unscheduled from run sheet",
                                                          "idempotency_key": f"{j['idempotency_key']}:unscheduled:{j['job_id']}",
                                                          "updated_at": now_iso()})
    for s in db.all("schedules", "variant_id=? AND state='scheduled'", [vid]):
        db.update("schedules", "schedule_id", s["schedule_id"], {"state": "cancelled", "reason": f"unscheduled by {actor}"})
    db.update("variants", "variant_id", vid, {"state": "approved", "updated_at": now_iso()})
    db.audit(actor, "variant_unscheduled", "variants", vid, {})
