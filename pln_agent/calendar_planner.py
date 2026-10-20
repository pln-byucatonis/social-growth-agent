"""Seven-day, supply-aware calendar across all accounts.

Slots are *test hypotheses* (config/slots.json), not proven best times. The planner never invents posts to
fill a quota: an empty slot is reported as a supply gap. Coordination rules:
  * canonical brand post before any PLN amplification of the same asset (≥ min_hours_after_canonical)
  * same asset on two accounts ≥ same_asset_min_hours_between_accounts apart; never same platform same day
  * per-account daily cap, series-per-week cap, legacy bridge ≤ N/week, LinkedIn weekdays only
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import CONFIG_DIR, Settings
from .db import DB, now_iso
from .scoring import allocation

DEFAULT_SLOTS = {"tiktok": ["12:15", "19:30"], "instagram": ["11:30", "18:30"], "facebook": ["13:00"],
                 "linkedin": ["08:30"], "youtube": ["16:00"], "x": ["12:00", "20:15"], "twitch": []}


def slot_hypotheses() -> dict:
    p = CONFIG_DIR / "slots.json"
    return json.loads(p.read_text())["slots"] if p.exists() else DEFAULT_SLOTS


def plan_week(db: DB, settings: Settings, start: date, days: int = 7, states=("in_review", "approved"),
              persist: bool = True) -> dict:
    tz = ZoneInfo(settings.timezone)
    alloc = allocation()
    caps = alloc["max_posts_per_account_per_day"]
    amp = alloc["pln_amplification"]
    gap_h = alloc["same_asset_min_hours_between_accounts"]
    bridge_cap = alloc["legacy_bridge_max_posts_per_week"]
    series_cap = alloc["series_max_per_account_per_week"]
    slots = slot_hypotheses()

    qs = ",".join("?" for _ in states)
    variants = db.all("variants", f"state IN ({qs})", list(states))
    order = {"canonical": 0, "legacy_inplace": 0, "pln_b2b": 1, "pln_amplification": 2, "legacy_bridge": 3}
    variants.sort(key=lambda v: (order.get(v["route_role"], 9), -(v["score"] or 0)))

    free = {}  # (account, datetime) available slot list per account
    for v in variants:
        acc = v["account_id"]
        if acc in free:
            continue
        lst = []
        for d in range(days):
            day = start + timedelta(days=d)
            for hhmm in slots.get(v["platform"], []):
                if v["platform"] == "linkedin" and day.weekday() >= 5:
                    continue
                h, m = map(int, hhmm.split(":"))
                lst.append(datetime.combine(day, time(h, m), tz))
        free[acc] = lst

    placed: list[dict] = []
    per_day: dict[tuple[str, date], int] = {}
    series_week: dict[tuple[str, str], int] = {}
    bridge_week: dict[str, int] = {}
    asset_times: dict[str, list[tuple[datetime, str, str, str]]] = {}  # asset → (time, account, platform, role)
    unplaced = []
    for v in variants:
        acc, plat = v["account_id"], v["platform"]
        if not slots.get(plat):
            unplaced.append({"variant_id": v["variant_id"], "reason": f"{plat}: no recurring slots (live/schedule-only)"})
            continue
        prior = asset_times.get(v["asset_id"], [])
        canon_times = [t for t, _, _, r in prior if r in ("canonical", "legacy_inplace")]
        chosen = None
        for t in free[acc]:
            if per_day.get((acc, t.date()), 0) >= caps.get(plat, 1):
                continue
            if any(abs((t - pt).total_seconds()) < gap_h * 3600 for pt, *_ in prior):
                continue
            if any(pp == plat and pt.date() == t.date() for pt, _, pp, _ in prior):
                continue
            if v["route_role"] in ("pln_amplification", "legacy_bridge") and canon_times and \
                    t < min(canon_times) + timedelta(hours=amp["min_hours_after_canonical"]):
                continue
            if v["route_role"] == "pln_amplification":
                n_amp = sum(1 for p in placed if p["route_role"] == "pln_amplification" and p["platform"] == plat
                            and p["slot"].date() == t.date())
                if n_amp >= amp["max_per_day_per_platform"]:
                    continue
            if v["route_role"] == "legacy_bridge" and bridge_week.get(acc, 0) >= bridge_cap:
                continue
            if v["series"] and series_week.get((acc, v["series"]), 0) >= series_cap:
                continue
            chosen = t
            break
        if not chosen:
            unplaced.append({"variant_id": v["variant_id"], "reason": "no slot satisfies spacing/caps this week"})
            continue
        free[acc].remove(chosen)
        per_day[(acc, chosen.date())] = per_day.get((acc, chosen.date()), 0) + 1
        if v["series"]:
            series_week[(acc, v["series"])] = series_week.get((acc, v["series"]), 0) + 1
        if v["route_role"] == "legacy_bridge":
            bridge_week[acc] = bridge_week.get(acc, 0) + 1
        asset_times.setdefault(v["asset_id"], []).append((chosen, acc, plat, v["route_role"]))
        placed.append({"variant_id": v["variant_id"], "account_id": acc, "platform": plat, "slot": chosen,
                       "route_role": v["route_role"], "asset_id": v["asset_id"], "hook": v["hook"], "state": v["state"],
                       "score": v["score"]})

    open_slots = sum(len(x) for x in free.values())
    if persist:
        for p in placed:
            if not db.one("SELECT schedule_id FROM schedules WHERE variant_id=? AND state IN ('planned','scheduled')", [p["variant_id"]]):
                db.insert("schedules", {"schedule_id": f"plan-{p['variant_id']}", "variant_id": p["variant_id"],
                                        "account_id": p["account_id"], "slot_at": p["slot"].isoformat(), "state": "planned",
                                        "reason": "calendar plan (needs approval before it can be scheduled)",
                                        "created_at": now_iso()}, upsert_key="schedule_id")
    return {"start": start.isoformat(), "days": days, "timezone": settings.timezone, "placed": placed,
            "unplaced": unplaced, "open_slots": open_slots,
            "supply_note": (f"{open_slots} hypothesis slots left open: not enough distinct licensed, approved-ready "
                            "assets to fill them. Ingest more footage rather than reposting.") if open_slots else ""}


def render_calendar_text(plan: dict) -> str:
    lines = [f"7-DAY CALENDAR from {plan['start']} ({plan['timezone']}) — slots are test hypotheses", ""]
    by_day: dict[str, list[dict]] = {}
    for p in plan["placed"]:
        by_day.setdefault(p["slot"].strftime("%a %Y-%m-%d"), []).append(p)
    for day in sorted(by_day, key=lambda d: d[4:]):
        lines.append(day)
        for p in sorted(by_day[day], key=lambda x: x["slot"]):
            lines.append(f"  {p['slot'].strftime('%H:%M')}  {p['platform']:<9} {p['account_id']:<48} "
                         f"[{p['route_role']}] {p['state']:<9} “{p['hook'][:58]}”")
    if plan["unplaced"]:
        lines += ["", "Not placed:"] + [f"  {u['variant_id']}: {u['reason']}" for u in plan["unplaced"]]
    if plan["supply_note"]:
        lines += ["", plan["supply_note"]]
    return "\n".join(lines)
