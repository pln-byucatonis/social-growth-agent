"""Weekly experiment planner + explainable strategy recommendations.

Max two experiments per platform per week, each with a control. Recommendations cite their evidence; when
there isn't enough data the recommendation is to collect it, not to guess.
"""
from __future__ import annotations

import statistics
import uuid
from datetime import date, datetime, timezone

from .calendar_planner import slot_hypotheses
from .db import DB
from .metrics import NORTH_STAR, post_performance

MAX_PER_PLATFORM = 2


def plan_experiments(db: DB, week_start: date, persist: bool = True) -> list[dict]:
    exps = []
    variants = [db.decode("variants", r) for r in db.query(
        "SELECT variant_id, platform, account_id, series, score, hook_candidates FROM variants "
        "WHERE state IN ('in_review','approved','scheduled')")]
    by_platform: dict[str, list[dict]] = {}
    for v in variants:
        by_platform.setdefault(v["platform"], []).append(v)
    slots = slot_hypotheses()
    for platform, vs in sorted(by_platform.items()):
        ns = NORTH_STAR.get(platform, {}).get("label", "north-star")
        planned = []
        with_alts = [v for v in vs if len(v.get("hook_candidates") or []) >= 2]
        if len(vs) >= 2 and with_alts:
            v = max(with_alts, key=lambda x: x["score"] or 0)
            c, t = v["hook_candidates"][0], v["hook_candidates"][1]
            planned.append({"variable": "hook_style", "account_id": v["account_id"], "control": f"{c['style']}: {c['text']}",
                            "treatment": f"{t['style']}: {t['text']}",
                            "hypothesis": f"'{t['style']}' hook beats '{c['style']}' on {ns} for {v['series'] or 'this series'}",
                            "design": "alternate styles across consecutive posts in the same series; hold edit length and slot constant"})
        if len(slots.get(platform, [])) >= 2 and len(vs) >= 2:
            a, b = slots[platform][:2]
            planned.append({"variable": "time_slot", "account_id": vs[0]["account_id"], "control": a, "treatment": b,
                            "hypothesis": f"{b} slot outperforms {a} on {ns} (same series, similar assets)",
                            "design": "alternate slots day by day; compare 24h checkpoint only"})
        if not planned:
            exps.append({"platform": platform, "status": "not_run",
                         "reason": f"insufficient supply ({len(vs)} ready variants) for a controlled test"})
            continue
        for p in planned[:MAX_PER_PLATFORM]:
            e = {"experiment_id": f"exp-{uuid.uuid4().hex[:8]}", "week_start": week_start.isoformat(), "platform": platform,
                 "account_id": p["account_id"], "variable": p["variable"], "control": p["control"], "treatment": p["treatment"],
                 "hypothesis": p["hypothesis"], "success_metric": NORTH_STAR.get(platform, {}).get("metric", ""),
                 "status": "planned", "result": {"design": p["design"], "min_sample": "≥3 posts per arm before reading"}}
            if persist:
                db.insert("experiments", e)
            exps.append(e)
    return exps


def _series_perf(db: DB) -> dict[tuple[str, str], list[tuple[str, float]]]:
    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for v in db.all("variants", "state='published'", order="updated_at"):
        ns = NORTH_STAR.get(v["platform"], {}).get("metric")
        val = post_performance(db, v["variant_id"]).get(ns)
        if val is not None:
            out.setdefault((v["account_id"], v["series"]), []).append((v["updated_at"], val))
    return out


def recommendations(db: DB, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    recs = []
    if not db.one("SELECT id FROM metrics LIMIT 1"):
        recs.append({"type": "baseline", "priority": "P0",
                     "text": "No analytics imported. Export 30–90 days of native analytics for PLN, CarJitsu, Putt Tour and the "
                             "4 WPL accounts and run `pln metrics import <csv>` before judging any creative or migration choice.",
                     "evidence": "metrics table is empty"})
    for (acc, series), pts in _series_perf(db).items():
        if len(pts) >= 4:
            vals = [v for _, v in pts]
            prior, last = statistics.median(vals[:-3]) if len(vals) > 3 else vals[0], statistics.median(vals[-3:])
            if prior and last < prior * 0.75:
                recs.append({"type": "fatigue", "priority": "P1",
                             "text": f"'{series}' on {acc} is fatiguing: last-3 median {last:.2f} vs prior {prior:.2f}. "
                                     "Rest the series for a week or change the opening beat.",
                             "evidence": f"{len(vals)} measured posts"})
            elif prior and last > prior * 1.25:
                recs.append({"type": "scale", "priority": "P1",
                             "text": f"'{series}' on {acc} is improving ({last:.2f} vs {prior:.2f}); allocate more footage to it.",
                             "evidence": f"{len(vals)} measured posts"})
    nowiso = now.replace(microsecond=0).isoformat()
    for s in db.all("schedules", "state='planned' AND slot_at < ?", [nowiso]):
        recs.append({"type": "missed_post", "priority": "P1",
                     "text": f"Planned slot {s['slot_at']} for {s['variant_id']} passed without approval/scheduling.",
                     "evidence": "schedules.state=planned in the past"})
    for s in db.all("schedules", "state='missed'"):
        recs.append({"type": "missed_post", "priority": "P1", "text": f"{s['variant_id']} was blocked at publish time; see job error.",
                     "evidence": "schedules.state=missed"})
    partner: dict[str, int] = {}
    blocked: dict[str, tuple[list[str], int]] = {}
    for v in (db.decode("variants", r) for r in db.query(
            "SELECT asset_id, risk_flags FROM variants WHERE state IN ('in_review','needs_fix')")):
        rf = v.get("risk_flags") or {}
        if "paid_partnership" in rf.get("review_flags", []):
            partner[v["asset_id"]] = partner.get(v["asset_id"], 0) + 1
        if rf.get("blockers"):
            blocked[v["asset_id"]] = (rf["blockers"], blocked.get(v["asset_id"], ([], 0))[1] + 1)
    for aid, n in partner.items():
        recs.append({"type": "partnership_approval", "priority": "P0",
                     "text": f"{aid}: {n} pending variants carry a paid partnership — two-person signoff + platform disclosure toggle required.",
                     "evidence": "rights.review_flags"})
    for aid, (rb, n) in blocked.items():
        recs.append({"type": "rights_blocker", "priority": "P0", "text": f"{aid} ({n} variants blocked): {'; '.join(rb)}",
                     "evidence": "rights gate"})
    # supply: P0 brands with few ready assets
    for brand in ("pln", "carjitsu", "putt_tour"):
        n = db.one("SELECT COUNT(*) AS n FROM assets WHERE brand_id=? AND rights_verified=? AND media_present=?", [brand, True, True])["n"]
        if n < 5:
            recs.append({"type": "supply", "priority": "P0",
                         "text": f"{brand}: only {n} licensed assets with media on hand. Native short-video growth needs a "
                                 "steady clip supply: request ≥10 annotated moments/week from production.",
                         "evidence": "assets table"})
    return recs
