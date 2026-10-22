"""How often and when the benchmark accounts post, which of them are growing, and the cadence and timing tests that
follows from it for our accounts.

Competitive Intelligence snapshots each benchmark (config/competitors.json) through Instagram Business Discovery
every few hours: posts per week, the hours its last posts went out, share of Reels, followers. Growth is measured
over at least `MIN_GROWTH_DAYS` of snapshots (a few hours' difference is noise). The benchmarks are much bigger than
our accounts, so their volume is a direction, never a target: the frequency test moves one step toward it, within
the footage we have (publishing.plan_frequency_test). Their timing transfers better: the timing test runs our
current evening slot against the window where the growing peers post most (a growth_lab `post_time` experiment).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB, now_iso

MIN_GROWTH_DAYS = 7
CLEAR_OF_OURS_H = 2  # a timing arm has to be this far from our existing slots to be a different window


def _dt(s) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def peers(db: DB, now: datetime | None = None, tz: str = "America/New_York") -> list[dict]:
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    out = []
    for h in sorted({r["handle"] for r in db.query("SELECT DISTINCT handle FROM competitor_snapshots")}):
        snaps = db.all("competitor_snapshots", "handle=?", [h], order="captured_at")
        last = snaps[-1]
        prof = last.get("profile") or {}
        base = [s for s in snaps if (_dt(last["captured_at"]) - _dt(s["captured_at"])).days >= MIN_GROWTH_DAYS]
        growth = None
        if base and base[-1]["followers"] and last["followers"]:
            b = base[-1]
            days = (_dt(last["captured_at"]) - _dt(b["captured_at"])).total_seconds() / 86400
            growth = round((last["followers"] - b["followers"]) / b["followers"] / days * 7 * 100, 3)  # % per week
        hours_et = [datetime(now.year, now.month, now.day, h_, tzinfo=timezone.utc).astimezone(zone).hour
                    for h_ in prof.get("common_hours_utc") or []]
        out.append({"handle": h, "followers": last["followers"], "posts_per_week": prof.get("posts_per_week"),
                    "reels_share": prof.get("reels_share"), "hours_local": hours_et, "growth_pct_week": growth,
                    "history_days": round((_dt(last["captured_at"]) - _dt(snaps[0]["captured_at"])).total_seconds() / 86400, 1),
                    "compare_to": last.get("compare_to") or []})
    return out


def _ours(db: DB, account_id: str, now: datetime, tz: str) -> dict:
    since = (now - timedelta(days=28)).isoformat()
    posts = db.all("posts", "account_id=? AND published_at >= ?", [account_id, since])
    zone = ZoneInfo(tz)
    hours = [_dt(p["published_at"]).astimezone(zone).hour for p in posts if p.get("published_at")]
    common = [h for h, _ in sorted(((h, hours.count(h)) for h in set(hours)), key=lambda x: -x[1])[:3]]
    return {"posts_per_week": round(len(posts) / 4, 1), "hours_local": common}


def for_account(db: DB, account_id: str, now: datetime | None = None, tz: str = "America/New_York") -> dict:
    """The benchmarks for this account's brand, the growing ones, and what the cadence and timing tests would try."""
    from .publishing import account_slots
    from .calendar_planner import slot_hypotheses
    now = now or datetime.now(timezone.utc)
    acc = db.get("accounts", "account_id", account_id)
    brand = acc["brand_id"]
    ps = [p for p in peers(db, now, tz) if brand in p["compare_to"] or (brand != "pln" and "pln" in p["compare_to"] and not p["compare_to"])]
    ps = ps or [p for p in peers(db, now, tz) if "pln" in p["compare_to"]]
    measured = [p for p in ps if p["growth_pct_week"] is not None]
    if measured:
        med = statistics.median(p["growth_pct_week"] for p in measured)
        growing = [p for p in measured if p["growth_pct_week"] >= med and p["growth_pct_week"] > 0]
        basis = f"{len(growing)} of {len(measured)} benchmarks growing at or above the group's median over {MIN_GROWTH_DAYS}+ days"
    else:
        growing = ps
        basis = (f"growth not measured yet (needs {MIN_GROWTH_DAYS} days of snapshots; the oldest is "
                 f"{max((p['history_days'] for p in ps), default=0):g} days): using every benchmark for now")
    ours = _ours(db, account_id, now, tz)
    slots, _ = account_slots(db, account_id, acc["platform"], slot_hypotheses())
    our_hours = {int(s[:2]) for s in slots} | set(ours["hours_local"])
    hours = [h for p in growing for h in p["hours_local"]]
    counts = sorted(((h, hours.count(h)) for h in set(hours)), key=lambda x: (-x[1], x[0]))
    window = next((h for h, _ in counts if all(min(abs(h - o), 24 - abs(h - o)) >= CLEAR_OF_OURS_H for o in our_hours)), None)
    evening = max((s for s in slots if int(s[:2]) >= 15), default=slots[-1] if slots else None)
    ppw = [p["posts_per_week"] for p in growing if p["posts_per_week"]]
    return {"account_id": account_id, "brand_id": brand, "ours": ours, "slots": slots, "peers": ps, "growing": [p["handle"] for p in growing],
            "basis": basis, "peer_posts_per_week": statistics.median(ppw) if ppw else None,
            "timing": {"A": evening, "B": f"{window:02d}:30"} if window is not None and evening else None,
            "peer_hours": [h for h, _ in counts[:4]], "at": now_iso()}


def plan_tests(db: DB, account_id: str, actor: str, now: datetime | None = None, tz: str = "America/New_York") -> dict:
    """Start the timing test (growth_lab `post_time`) and plan the three-week frequency test, unless already running."""
    from . import growth_lab as lab
    from .publishing import plan_frequency_test
    r = for_account(db, account_id, now, tz)
    out = {"timing": None, "frequency": None, "notes": []}
    running = {e["variable"] for e in db.all("experiments", "account_id=? AND status IN ('running','planned')", [account_id])}
    if "post_time" in running:
        out["notes"].append("a timing test is already running")
    elif r["timing"]:
        hyp = (f"Posting at {r['timing']['B']} (when the growing benchmarks post: {', '.join('@' + h for h in r['growing'][:4])}) "
               f"reaches more people than our {r['timing']['A']} slot")
        e = lab.create(db, account_id, "post_time", r["timing"], hyp, actor, outcome="reach")
        out["timing"] = e["experiment_id"]
    else:
        out["notes"].append("no peer posting window clear of our existing slots")
    if "frequency" in running:
        out["notes"].append("a frequency test is already planned or running")
    else:
        weeks = plan_frequency_test(db, account_id, actor=actor)
        out["frequency"] = [w["experiment_id"] for w in weeks]
        if r["peer_posts_per_week"]:
            out["notes"].append(f"benchmarks post ~{r['peer_posts_per_week']:g}/week; the test moves one step toward that, "
                                "within the footage we have")
    db.audit(actor, "cadence_tests_planned", "experiments", account_id, out)
    return {**out, "analysis": r}
