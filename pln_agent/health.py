"""Pipeline health: is the agent actually producing posts? Checked every hour; problems show on every page.

In November the agent went three weeks making no new drafts (the planner retried the same clips every minute, then
a release undid the fix) and nobody noticed, because every page looked normal. These checks look at what comes out
of the pipeline, not at whether the code runs:

* new drafts: were any drafts made for the accounts the team works in the last day?
* tomorrow's run sheet: is there enough to approve for tomorrow?
* waiting for approval: are finished posts sitting unapproved?
* published: did anything go out (or get rehearsed, while publishing is simulated) in the last two days?
* failed posts: did a post fail to publish?
* drafting loop: is the planner looking at clips over and over without making anything?
* editor: is the editor paused (model allowance used up) or falling behind?

The result is saved (settings_kv `pipeline_health`) so pages read it without recomputing, and sent to CloudWatch
(namespace PLN/SocialAgent) so an alarm can email someone.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .db import DB, now_iso

KEY = "pipeline_health"
EVERY_MIN = 60            # how often the checks run
MIN_TOMORROW = 4          # fewer posts than this on tomorrow's run sheet is a thin day
WAITING_HOURS = 48        # finished posts waiting longer than this, with nothing approved meanwhile
QUIET_HOURS = 48          # nothing published (or rehearsed) for this long
LOOP_PER_HOUR = 300       # the planner looking at this many clips in an hour and making nothing is a loop
EDITOR_BEHIND_HOURS = 12  # drafts waiting for the editor longer than this


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _worked(db: DB) -> list[str]:
    from .accounts import focus
    return list(focus().get("worked") or [])


def _accounts(ids: list[str]) -> tuple[str, list]:
    """SQL limiting to the worked accounts; with no focus list (PLN_FOCUS_DISABLED), every account."""
    return ("AND account_id IN (" + ",".join("?" for _ in ids) + ")", list(ids)) if ids else ("", [])


def checks(db: DB, settings, now: datetime | None = None) -> list[dict]:
    """Every check, ok or not: {key, ok, level ('bad'|'warn'), title, detail, link, value}."""
    now = now or datetime.now(timezone.utc)
    worked = _worked(db)
    acc_in, acc_p = _accounts(worked)
    out = []

    def add(key, ok, level, title, detail, link, value):
        out.append({"key": key, "ok": bool(ok), "level": level, "title": title, "detail": detail, "link": link,
                    "value": value})

    day = _iso(now - timedelta(hours=24))
    n = (db.one(f"SELECT COUNT(*) AS n FROM variants WHERE created_at >= ? {acc_in}", [day] + acc_p)
         or {}).get("n", 0)
    add("new_drafts", n > 0, "bad", "No new drafts in 24 hours" if not n else f"{n} new drafts in 24 hours",
        "The agent made nothing for the accounts the team works. Usually: no new clips coming in, every clip already "
        "drafted, or the planner stuck." if not n else "", "/queue", n)

    try:
        from .runsheet import day_sheet
        from zoneinfo import ZoneInfo
        tomorrow = now.astimezone(ZoneInfo(settings.timezone)).date() + timedelta(days=1)
        rows = day_sheet(db, settings, tomorrow)["rows"]
        t = len(rows)
        add("runsheet_tomorrow", t >= MIN_TOMORROW, "warn",
            f"Tomorrow's run sheet has {t} post{'s' if t != 1 else ''}",
            f"Fewer than {MIN_TOMORROW}: not enough finished posts to fill the day." if t < MIN_TOMORROW else "",
            "/runsheet?day=tomorrow", t)
    except Exception as e:  # noqa: BLE001 — a check that can't run is itself a finding
        add("runsheet_tomorrow", False, "warn", "Couldn't read tomorrow's run sheet", str(e)[:200], "/runsheet", None)

    waiting = db.query(f"SELECT updated_at FROM variants WHERE state='in_review' {acc_in} "
                       "ORDER BY updated_at", acc_p)
    since = _iso(now - timedelta(hours=WAITING_HOURS))
    approved = db.one("SELECT MAX(at) AS at FROM approvals WHERE decision='approve'") or {}
    stale = bool(waiting) and waiting[0]["updated_at"] < since and (approved.get("at") or "") < since
    last = (approved.get("at") or "")[:10] or "never"
    add("waiting_approval", not stale, "warn",
        f"{len(waiting)} post{'s' if len(waiting) != 1 else ''} waiting for approval" + (f", none approved since {last}" if stale else ""),
        "Finished posts are waiting on the run sheet and nothing has been approved for two days." if stale else "",
        "/runsheet", len(waiting))

    quiet = _iso(now - timedelta(hours=QUIET_HOURS))
    p = (db.one("SELECT COUNT(*) AS n FROM publish_jobs WHERE state IN ('published','simulated') AND updated_at >= ?",
                [quiet]) or {}).get("n", 0)
    add("published", p > 0, "bad", "Nothing published in 2 days" if not p else f"{p} posts out in 2 days",
        "No post went out (or was rehearsed) in the last 48 hours." if not p else "", "/runsheet", p)

    f = db.query("SELECT variant_id, last_error FROM publish_jobs WHERE state='failed' AND updated_at >= ?", [day])
    add("failed_posts", not f, "bad", f"{len(f)} post{'s' if len(f) != 1 else ''} failed to publish" if f else "No failed posts",
        "; ".join(f"{x['variant_id']}: {(x['last_error'] or '')[:80]}" for x in f[:3]), "/runsheet", len(f))

    hour = _iso(now - timedelta(hours=1))
    looks = (db.one("SELECT COUNT(*) AS n FROM editorial_packs WHERE created_at >= ?", [hour]) or {}).get("n", 0)
    made = (db.one("SELECT COUNT(*) AS n FROM variants WHERE created_at >= ?", [hour]) or {}).get("n", 0)
    loop = looks >= LOOP_PER_HOUR and not made
    add("drafting_loop", not loop, "bad", f"Drafting loop: {looks} clips looked at in an hour, nothing made" if loop else "Drafting not looping",
        "The planner keeps re-reading the same clips. Check the latest release kept the planner fix." if loop else "",
        "/audit", looks)

    from .recut import PAUSE_KEY
    until = db.kv_get(PAUSE_KEY, "") or ""
    paused = bool(until) and until > _iso(now)
    behind = _iso(now - timedelta(hours=EDITOR_BEHIND_HOURS))
    old = (db.one("SELECT COUNT(*) AS n FROM variants WHERE state='recut' AND updated_at < ?", [behind]) or {}).get("n", 0)
    add("editor", not paused and not old, "warn",
        f"Editor paused until {until[11:16]} UTC (model allowance used up)" if paused else
        (f"{old} drafts waiting for the editor over {EDITOR_BEHIND_HOURS} h" if old else "Editor keeping up"),
        "Reels wait for the editor and critic before they reach Approvals." if (paused or old) else "", "/queue", old)
    return out


def problems(result: dict | None) -> list[dict]:
    return [c for c in (result or {}).get("checks") or [] if not c["ok"]]


def run(db: DB, settings, now: datetime | None = None, actor: str = "worker") -> dict:
    res = {"at": now_iso(), "checks": checks(db, settings, now)}
    db.kv_set(KEY, json.dumps(res, default=str), actor)
    res["cloudwatch"] = _send_metrics(res)
    return res


def run_due(db: DB, settings, actor: str = "worker") -> dict | str:
    """Once an hour (the worker ticks every minute)."""
    last = latest(db)
    if last and last.get("at", "") > _iso(datetime.now(timezone.utc) - timedelta(minutes=EVERY_MIN)):
        return "not due"
    return run(db, settings, actor=actor)


def latest(db: DB) -> dict | None:
    raw = db.kv_get(KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


METRICS = {"new_drafts": "NewDrafts24h", "runsheet_tomorrow": "RunSheetTomorrow", "waiting_approval": "WaitingApproval",
           "published": "Published48h", "failed_posts": "FailedPosts24h"}


def _send_metrics(res: dict) -> str:
    """CloudWatch PLN/SocialAgent, for an alarm that emails. Skipped quietly off AWS."""
    import os
    if os.environ.get("PLN_CLOUDWATCH", "on") == "off" or not os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION")):
        return "skipped"
    try:
        import boto3
        data = [{"MetricName": METRICS[c["key"]], "Value": float(c["value"] or 0), "Unit": "Count"}
                for c in res["checks"] if c["key"] in METRICS and c["value"] is not None]
        data.append({"MetricName": "HealthProblems", "Value": float(len(problems(res))), "Unit": "Count"})
        boto3.client("cloudwatch").put_metric_data(Namespace="PLN/SocialAgent", MetricData=data)
        return "sent"
    except Exception as e:  # noqa: BLE001 — the page still shows the problems
        return f"failed: {str(e)[:120]}"
