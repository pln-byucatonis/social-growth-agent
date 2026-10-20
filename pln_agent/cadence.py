"""Adaptive cadence: starting schedules are hypotheses; measured incremental growth decides.

For each account we bucket the measured history into ISO weeks (posts published, net follower change,
views and engagements of that week's posts) and estimate:
* incremental followers per additional post — least-squares slope of weekly net follower change on weekly
  post count (and the same on the busier vs quieter half, to spot diminishing returns);
* engagement dilution — does engagement per 1k views fall in busier weeks?
* production capacity — distinct approved/in-review creative the agent can supply per week.

The recommendation moves at most `max_step_per_week` from the current level, never above
`ceiling_multiplier × start_max`, and never above supply. With too few weeks it says "collect a baseline
at the starting cadence" rather than guessing. Week-to-week noise (events, content quality, seasonality)
is real: confidence stays "low" until there are ≥8 weeks and a decent fit.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone

from .config import CONFIG_DIR
from .db import DB, now_iso


def config() -> dict:
    return json.loads((CONFIG_DIR / "cadence.json").read_text())


def starting(acc: dict, cfg: dict | None = None) -> tuple[float, float, str]:
    cfg = cfg or config()
    b = cfg["brands"].get(acc["brand_id"]) or cfg["brands"].get(acc.get("canonical_brand_id", ""), {})
    rng = b.get(acc["platform"]) or b.get("*") or cfg["default"]
    return float(rng[0]), float(rng[1]), cfg.get("source", "config/cadence.json")


def _fit(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    if len(xs) < 3 or len(set(xs)) < 2:
        return None, None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy * sxy) / (sxx * syy) if syy else 0.0
    return slope, r2


def weekly_history(db: DB, account_id: str) -> list[dict]:
    from .audience import _account_series, post_outcomes
    followers = dict(_account_series(db, account_id, "followers"))
    weeks: dict[str, dict] = {}
    for p in post_outcomes(db, [account_id], min_age_h=0):
        if not p["published_at"]:
            continue
        d = date.fromisoformat(str(p["published_at"])[:10])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"week": wk, "posts": 0, "views": 0.0, "engagements": 0.0})
        w["posts"] += 1
        w["views"] += p["outcomes"]["views"] or 0
        w["engagements"] += (p["metrics"].get("engagements") or 0)
    # weeks with follower snapshots but no posts still count (0 posts is data)
    for d in followers:
        dd = date.fromisoformat(d)
        wk = (dd - timedelta(days=dd.weekday())).isoformat()
        weeks.setdefault(wk, {"week": wk, "posts": 0, "views": 0.0, "engagements": 0.0})
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        start = date.fromisoformat(wk)
        days = [(start + timedelta(days=i)).isoformat() for i in range(-1, 7)]
        vals = [followers[d] for d in days if d in followers]
        if len(vals) >= 2:  # need a snapshot at (or just before) the start and near the end of the week
            first = next(followers[d] for d in days if d in followers)
            last = next(followers[d] for d in reversed(days) if d in followers)
            w["net_followers"] = last - first
        else:
            w["net_followers"] = None
        w["eng_per_1k"] = (w["engagements"] / w["views"] * 1000) if w["views"] else None
        out.append(w)
    # drop incomplete edge weeks without follower coverage
    return [w for w in out if w["net_followers"] is not None]


def supply_per_week(db: DB, account_id: str) -> int:
    n = db.one("SELECT COUNT(*) AS n FROM variants WHERE account_id=? AND state IN ('approved','in_review','scheduled')",
               [account_id])["n"]
    return int(n)  # drafts on hand; a real production rate needs a few weeks of history


def recommend(db: DB, account_id: str, persist: bool = True) -> dict:
    cfg = config()
    rules = cfg["rules"]
    acc = db.get("accounts", "account_id", account_id)
    lo, hi, src = starting(acc, cfg)
    hist = weekly_history(db, account_id)
    supply = supply_per_week(db, account_id)
    observed = statistics.median([w["posts"] for w in hist[-4:]]) if hist else None
    ev = {"weeks": len(hist), "history": hist[-12:], "start_range": [lo, hi], "start_source": src,
          "observed_posts_per_week": observed, "drafts_on_hand": supply}
    if len(hist) < rules["min_weeks_for_recommendation"]:
        target = hi if observed is None else min(max(observed, lo), hi)
        rec = {"action": "collect_baseline", "recommended_per_week": target, "confidence": "low",
               "rationale": (f"Only {len(hist)} measured week(s); need {rules['min_weeks_for_recommendation']}. "
                             f"Run the starting cadence ({lo:g}–{hi:g}/week, a hypothesis from {src}) and keep measuring.")}
    else:
        xs = [w["posts"] for w in hist]
        ys = [w["net_followers"] for w in hist]
        slope, r2 = _fit(xs, ys)
        med = statistics.median(xs)
        hi_half = [w for w in hist if w["posts"] > med]
        lo_half = [w for w in hist if w["posts"] <= med]
        s_hi, _ = _fit([w["posts"] for w in hi_half], [w["net_followers"] for w in hi_half]) if len(hi_half) >= 3 else (None, None)
        eng_hi = [w["eng_per_1k"] for w in hi_half if w["eng_per_1k"] is not None]
        eng_lo = [w["eng_per_1k"] for w in lo_half if w["eng_per_1k"] is not None]
        dilution = (1 - statistics.median(eng_hi) / statistics.median(eng_lo)) if eng_hi and eng_lo and statistics.median(eng_lo) else None
        ev.update({"followers_per_extra_post": slope, "r2": r2, "slope_busier_weeks": s_hi, "engagement_dilution": dilution})
        current = observed if observed is not None else lo
        step = rules["max_step_per_week"]
        ceiling = hi * rules["ceiling_multiplier_of_start_max"]
        conf = "medium" if len(hist) >= 8 and (r2 or 0) >= 0.3 else "low"
        if slope is None:
            action, target = "hold", current
            why = "Weekly volume hasn't varied enough to estimate the effect of an extra post; hold and vary it deliberately (frequency experiment)."
        elif slope > 0 and (dilution is None or dilution < rules["engagement_dilution_threshold"]) and (s_hi is None or s_hi > 0):
            action, target = "increase", min(current + step, ceiling)
            why = (f"Each extra post/week has gone with ≈{slope:+.1f} net followers (r²={r2:.2f}, {len(hist)} weeks)"
                   + (f"; engagement per 1k views {dilution:+.0%} lower in busier weeks" if dilution is not None else "")
                   + ". Add posts, but keep checking the busier-week slope for diminishing returns.")
        elif slope > 0 and s_hi is not None and s_hi <= 0:
            action, target = "hold", current
            why = (f"Extra posts help overall (≈{slope:+.1f}/post) but not in the busiest weeks (slope {s_hi:+.1f}): diminishing "
                   "returns. Hold volume and put the effort into stronger creative.")
        elif dilution is not None and dilution >= rules["engagement_dilution_threshold"]:
            action, target = "decrease", max(current - step, lo / 2)
            why = (f"Busier weeks cut engagement per 1k views by {dilution:.0%} without measured follower gains "
                   f"(≈{slope:+.1f}/post). Post less and pick stronger clips.")
        else:
            action, target = "decrease" if slope < 0 else "hold", max(current - step, lo / 2) if slope < 0 else current
            why = f"No measured follower gain from extra posts (≈{slope:+.1f}/post, r²={r2:.2f}). Don't add volume."
        rec = {"action": action, "recommended_per_week": round(target, 1), "confidence": conf, "rationale": why}
    if rec["recommended_per_week"] and supply < rec["recommended_per_week"] and rec["action"] != "collect_baseline":
        rec["rationale"] += f" Production-limited: only {supply} draft(s) on hand."
        rec["action"] = "production_limited"
        rec["recommended_per_week"] = float(supply)
    out = {"account_id": account_id, "start_min": lo, "start_max": hi, "start_source": src, **rec, "evidence": ev,
           "updated_at": now_iso()}
    if persist:
        db.insert("cadence", out, upsert_key="account_id")
    return out


def weekly_target(db: DB, account_id: str) -> int | None:
    """What the calendar planner should aim for this week (None = no cadence record yet)."""
    r = db.get("cadence", "account_id", account_id)
    if not r or r.get("recommended_per_week") is None:
        return None
    return int(math.floor(r["recommended_per_week"] + 1e-9))


def recommend_all(db: DB) -> list[dict]:
    return [recommend(db, a["account_id"]) for a in db.all("accounts")
            if a["platform"] != "twitch" and not a.get("hard_block_reason")]
