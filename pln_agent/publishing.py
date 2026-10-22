"""Publishing Strategist: when to post and how often — learned per account, never a universal rule.

* `learn_slots` compares each two-hour posting window (local time) against the account's other posts on a
  per-1k-views rate (engagements, falling back to shares), with the same evidence rule as the Creative
  Intelligence DB (sample size, permutation test, FDR, time stability). Windows that are conclusively or
  suggestively better become the account's slots; the platform starting hypotheses fill the rest, and one
  slot per day is reserved for an untested window (exploration) so the account keeps learning.
* `plan_frequency_test` turns the brief's frequency design (week A at the starting cadence, week B one
  step up, week C back with different spacing) into experiment rows; while a test week is running its arm
  sets the account's weekly target. Evaluation lives in the Growth Experimentation loop.
"""
from __future__ import annotations

import json
import statistics
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB, now_iso

WINDOW_H = 2
OUTCOMES = ("engagements_per_1k_views", "shares_per_1k_views")


def _window(h: int) -> str:
    s = (h // WINDOW_H) * WINDOW_H
    return f"{s:02d}:00–{s + WINDOW_H:02d}:00"


def learn_slots(db: DB, account_id: str, tz: str = "America/New_York", persist: bool = True,
                outcome: str | None = None) -> dict:
    from .audience import post_outcomes
    from .creative_db import MIN_N, bh_adjust, evidence_class, permutation_p
    posts = [p for p in post_outcomes(db, [account_id]) if not p["young"] and p["published_at"]]
    outcome = outcome or next((o for o in OUTCOMES if sum(1 for p in posts if p["outcomes"].get(o) is not None) >= 2 * MIN_N), None)
    acc = db.get("accounts", "account_id", account_id)
    result = {"account_id": account_id, "platform": acc["platform"] if acc else "", "outcome": outcome,
              "posts": len(posts), "windows": [], "learned_slots": [], "updated_at": now_iso()}
    if outcome:
        groups: dict[str, list[float]] = {}
        for p in posts:
            v = p["outcomes"].get(outcome)
            if v is None:
                continue
            d = datetime.fromisoformat(str(p["published_at"]).replace("Z", "+00:00"))
            d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo(tz))
            groups.setdefault(_window(d.hour), []).append(v)
        rows = []
        for w, xs in groups.items():
            rest = [x for k, ys in groups.items() if k != w for x in ys]
            p = permutation_p(xs, rest) if min(len(xs), len(rest)) >= MIN_N else None
            rows.append({"window": w, "n": len(xs), "median": statistics.median(xs),
                         "median_rest": statistics.median(rest) if rest else None, "p": p})
        testable = [r for r in rows if r["p"] is not None]
        for r, q in zip(testable, bh_adjust([r["p"] for r in testable]) if testable else []):
            r["q"] = q
        for r in rows:
            r.setdefault("q", None)
            r["lift"] = (r["median"] / r["median_rest"] - 1) if r["median_rest"] else None
            r["evidence"] = evidence_class(r["n"], len(posts) - r["n"], r["q"])
        rows.sort(key=lambda r: r["window"])
        result["windows"] = rows
        best = [r for r in rows if r["evidence"] in ("conclusive", "suggestive") and (r["lift"] or 0) > 0]
        # post at the middle of each winning window
        result["learned_slots"] = [f"{int(r['window'][:2]) + WINDOW_H // 2:02d}:00" for r in sorted(best, key=lambda r: -r["lift"])]
    if persist:
        db.insert("settings_kv", {"key": f"slots:{account_id}", "value": json.dumps(result), "updated_at": now_iso(),
                                  "updated_by": "publishing-strategist"}, upsert_key="key")
    return result


def account_slots(db: DB, account_id: str, platform: str, hypotheses: dict) -> tuple[list[str], str]:
    """Slots the planner should use for this account, and why."""
    base = list(hypotheses.get(platform, []))
    raw = db.kv_get(f"slots:{account_id}")
    learned = (json.loads(raw).get("learned_slots") if raw else None) or []
    if not learned:
        return base, "Starting time — not enough posts yet to learn this account's best window"
    slots = list(dict.fromkeys(learned + base))[:max(len(base), len(learned))]
    from .team_params import get as _param
    explore = next((f"{h:02d}:00" for h in (9, 15, 21, 12, 18) if f"{h:02d}:00" not in slots), None) \
        if _param(db, "publishing", "exploration_slot", True) else None
    if explore:
        slots.append(explore)
    return slots, (f"Best measured window {', '.join(learned)}, plus starting times; "
                   + (f"exploration slot {explore} keeps testing a new time" if explore else "no exploration slot (manager setting)"))


def plan_frequency_test(db: DB, account_id: str, start: date | None = None, actor: str = "publishing-strategist") -> list[dict]:
    """Three consecutive weeks: A = starting max, B = one step up (if footage allows), C = A again, re-spaced."""
    from .cadence import config, starting, supply_per_week
    acc = db.get("accounts", "account_id", account_id)
    lo, hi, _ = starting(acc)
    step = config()["rules"]["max_step_per_week"]
    start = start or (datetime.now(timezone.utc).date() + timedelta(days=7 - datetime.now(timezone.utc).weekday()))
    supply = supply_per_week(db, account_id)
    arms = [("A", hi, "starting cadence"), ("B", min(hi + step, max(hi, supply)), "one step up"),
            ("C", hi, "starting cadence, different spacing (alternate the posting windows)")]
    rows = []
    for i, (arm, n, design) in enumerate(arms):
        e = {"experiment_id": f"freq-{uuid.uuid4().hex[:8]}", "week_start": (start + timedelta(weeks=i)).isoformat(),
             "platform": acc["platform"], "account_id": account_id, "variable": "frequency", "control": f"{hi:g}/week",
             "treatment": f"{n:g}/week", "hypothesis": f"Week {arm}: {n:g} posts/week ({design}) — does the extra volume add "
                                                     "followers per post without lowering engagement per 1k views?",
             "success_metric": "net follows per week; engagement per 1k views", "status": "planned",
             "result": {"arm": arm, "posts_per_week": n, "design": design, "series": f"freq-{account_id}-{start.isoformat()}"}}
        db.insert("experiments", e)
        rows.append(e)
    db.audit(actor, "frequency_test_planned", "experiments", account_id, {"weeks": [r["week_start"] for r in rows]})
    return rows


def frequency_arm(db: DB, account_id: str, today: date | None = None) -> float | None:
    today = today or datetime.now(timezone.utc).date()
    wk = (today - timedelta(days=today.weekday())).isoformat()
    week = db.memo(f"freq:{wk}", lambda: db.query(
        "SELECT account_id, result FROM experiments WHERE variable='frequency' AND week_start=? "
        "AND status IN ('planned','running')", [wk]))  # every account's arm in one read per page
    r = next((x for x in week if x["account_id"] == account_id), None)
    if not r:
        return None
    res = r["result"] if isinstance(r["result"], dict) else json.loads(r["result"] or "{}")
    return float(res.get("posts_per_week")) if res.get("posts_per_week") is not None else None


def learn_all(db: DB, tz: str) -> int:
    n = 0
    for a in db.query("SELECT DISTINCT account_id FROM posts"):
        learn_slots(db, a["account_id"], tz)
        n += 1
    return n
