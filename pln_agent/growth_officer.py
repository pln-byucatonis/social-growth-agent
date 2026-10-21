"""Chief Growth Officer: one weekly brief that coordinates every other capability.

It reads only what the other agents measured or produced — never invents numbers — and turns it into:
* a brand scoreboard (28-day follows / unfollows / net, accounts reached, non-follower share, net follows
  per 1,000 accounts reached — the cross-platform conversion measure), with each brand's priority tier;
* a creative-effort reallocation proposal: tier floors first, then toward brands that convert reach into
  followers better *and* have footage to make more, moving at most `max_shift_per_week`. Applied only when
  a person approves it;
* supply (drafts by state, unused Scout moments ≥ 70), posting gaps, cadence actions, planned experiments,
  WPL → Putt Tour migration health, and the strongest current PLN evidence;
* a ranked action list for the week.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from .config import CONFIG_DIR
from .db import DB, now_iso

ALLOC_KEY = "allocation_shares"


def goals() -> dict:
    return json.loads((CONFIG_DIR / "growth_goals.json").read_text())


def tier_of(brand_id: str, g: dict | None = None) -> str:
    g = g or goals()
    for t, spec in g["tiers"].items():
        if brand_id in spec["brands"]:
            return t
    return "P3"


def current_shares(db: DB) -> dict[str, float]:
    from .scoring import allocation
    base = dict(allocation()["creative_effort_share"])
    raw = db.kv_get(ALLOC_KEY)
    if raw:
        base.update(json.loads(raw))
    return base


def _latest(db: DB, account_id: str, fam: str):
    r = db.query("SELECT value FROM metrics WHERE account_id=? AND canonical_metric=? AND platform_post_id IS NULL "
                 "ORDER BY period_end DESC, captured_at DESC LIMIT 1", [account_id, fam])
    return r[0]["value"] if r else None


def scoreboard(db: DB) -> list[dict]:
    brands: dict[str, dict] = {}
    for a in db.all("accounts"):
        b = brands.setdefault(a["brand_id"], {"brand_id": a["brand_id"], "accounts": 0, "follows": 0.0, "unfollows": 0.0,
                                               "reach": 0.0, "non_follower_reach": 0.0, "followers": 0.0, "measured": False,
                                               "last_post": None})
        b["accounts"] += 1
        vals = {f: _latest(db, a["account_id"], f) for f in
                ("follows_window", "unfollows_window", "reach_window", "non_follower_reach", "followers")}
        if vals["reach_window"] is not None:
            b["measured"] = True
        for k, f in (("follows", "follows_window"), ("unfollows", "unfollows_window"), ("reach", "reach_window"),
                     ("non_follower_reach", "non_follower_reach"), ("followers", "followers")):
            b[k] += vals[f] or 0
        lp = db.one("SELECT MAX(published_at) AS t FROM posts WHERE account_id=?", [a["account_id"]])
        if lp and lp["t"] and (b["last_post"] is None or str(lp["t"]) > b["last_post"]):
            b["last_post"] = str(lp["t"])[:10]
    g = goals()
    out = []
    for b in brands.values():
        b["tier"] = tier_of(b["brand_id"], g)
        b["net"] = b["follows"] - b["unfollows"] if b["measured"] else None
        enough = b["reach"] >= g["rules"]["min_reach_for_efficiency"]
        b["net_per_1k_reach"] = round(b["net"] / b["reach"] * 1000, 2) if b["measured"] and enough else None
        b["follows_per_1k_reach"] = round(b["follows"] / b["reach"] * 1000, 2) if b["measured"] and enough else None
        b["non_follower_share"] = round(b["non_follower_reach"] / b["reach"], 3) if b["measured"] and b["reach"] else None
        b["efficiency_note"] = ("" if not b["measured"] else "" if enough else
                                f"reach {b['reach']:,.0f} < {g['rules']['min_reach_for_efficiency']:,} — too small to compare")
        out.append(b)
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    out.sort(key=lambda b: (order.get(b["tier"], 9), -(b["reach"] or 0)))
    return out


def supply(db: DB) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in db.query("SELECT brand_id, state, COUNT(*) AS n FROM variants GROUP BY brand_id, state"):
        out.setdefault(r["brand_id"], {"drafts": {}, "scout_unused": 0})["drafts"][r["state"]] = r["n"]
    for r in db.query("SELECT brand_id, COUNT(*) AS n FROM scout_moments WHERE status='candidate' AND potential >= 70 "
                      "GROUP BY brand_id"):
        out.setdefault(r["brand_id"], {"drafts": {}, "scout_unused": 0})["scout_unused"] = r["n"]
    return out


def propose_allocation(db: DB, board: list[dict], sup: dict) -> dict:
    """Tier floors, then shift toward measured conversion where footage exists. Never more than the weekly cap."""
    g = goals()
    cur = current_shares(db)
    cap = g["rules"]["max_shift_per_week"]
    floors = {b: spec["floor_share"] for t, spec in g["tiers"].items() for b in spec["brands"] if b != "*"}
    measured = {b["brand_id"]: b for b in board if b["follows_per_1k_reach"] is not None}
    reasons, new = [], dict(cur)
    if len(measured) >= 2:
        med = sorted(x["follows_per_1k_reach"] for x in measured.values())[len(measured) // 2]
        for bid, b in measured.items():
            s = sup.get(bid, {})
            stock = sum(v for k, v in s.get("drafts", {}).items() if k in ("in_review", "approved")) + s.get("scout_unused", 0)
            if b["follows_per_1k_reach"] > med and stock >= 10:
                delta = min(cap / 2, cap * (b["follows_per_1k_reach"] / med - 1) / 2) if med else 0
                new[bid] = cur.get(bid, 0.05) + delta
                reasons.append(f"{bid}: {b['follows_per_1k_reach']} follows per 1k reached vs median {med} and "
                               f"{stock} pieces ready → +{delta:.0%}")
            elif b["follows_per_1k_reach"] < med:
                delta = min(cap / 2, cap * (1 - b["follows_per_1k_reach"] / med) / 2) if med else 0
                new[bid] = max(floors.get(bid, 0.0), cur.get(bid, 0.05) - delta)
                reasons.append(f"{bid}: {b['follows_per_1k_reach']} follows per 1k reached vs median {med} → "
                               f"−{cur.get(bid, 0.05) - new[bid]:.0%} (floor {floors.get(bid, 0):.0%})")
    else:
        reasons.append("Fewer than two brands have enough measured reach to compare — keep the current split.")
    for bid, f in floors.items():
        if new.get(bid, 0) < f:
            new[bid] = f
            reasons.append(f"{bid}: raised to its {tier_of(bid, g)} floor {f:.0%}")
    total = sum(new.values()) or 1
    new = {k: round(v / total, 3) for k, v in new.items()}
    changed = {k: (round(cur.get(k, 0), 3), v) for k, v in new.items() if abs(v - cur.get(k, 0)) >= 0.005}
    return {"current": {k: round(v, 3) for k, v in cur.items()}, "proposed": new, "changes": changed, "reasons": reasons}


def generate_brief(db: DB, actor: str = "chief-growth-officer", today: date | None = None, persist: bool = True) -> dict:
    from .experiments import recommendations
    g = goals()
    today = today or datetime.now(timezone.utc).date()
    week = (today - timedelta(days=today.weekday())).isoformat()
    board, sup = scoreboard(db), supply(db)
    alloc = propose_allocation(db, board, sup)
    cadence = db.all("cadence", order="account_id")
    exps = db.all("experiments", "status IN ('planned','running')", order="week_start DESC")[:20]
    evidence = sorted(db.all("knowledge", "level='pln_evidence' AND status='current'"),
                      key=lambda e: ((e.get("evidence") or {}).get("q") or 1))[:8]
    by = {b["brand_id"]: b for b in board}
    wpl, putt = by.get("world_putting_league"), by.get("putt_tour")
    migration = {"wpl": wpl, "putt_tour": putt, "alerts": []}
    if wpl and wpl["measured"] and (wpl["unfollows"] > wpl["follows"]):
        migration["alerts"].append(f"WPL is losing followers ({wpl['follows']:,.0f} follows vs {wpl['unfollows']:,.0f} "
                                   "unfollows, 28 days) — run the bridge posts that point WPL fans to @putttour.")
    actions = []
    gap_days = g["rules"]["posting_gap_days"]
    for b in board:
        if b["tier"] in ("P0", "P1"):
            last = b["last_post"]
            days = (today - date.fromisoformat(last)).days if last else None
            if days is None or days > gap_days:
                ready = sup.get(b["brand_id"], {}).get("drafts", {}).get("in_review", 0)
                actions.append({"priority": "P0", "area": "publishing",
                                "text": f"{b['brand_id']}: " + (f"no post in {days} days" if days is not None else "no posts on record")
                                        + f" — {ready} drafts are waiting for review."})
            if b["net"] is not None and b["net"] < 0:
                actions.append({"priority": "P0" if b["tier"] == "P0" else "P1", "area": "audience",
                                "text": f"{b['brand_id']}: net {b['net']:+,.0f} followers in 28 days "
                                        f"({b['follows']:,.0f} follows / {b['unfollows']:,.0f} unfollows)."})
            stock = sup.get(b["brand_id"], {})
            if b["tier"] == "P0" and stock.get("scout_unused", 0) < 10 and not stock.get("drafts", {}).get("in_review"):
                actions.append({"priority": "P1", "area": "supply",
                                "text": f"{b['brand_id']}: fewer than 10 strong unused moments — tag more footage in ops."})
    for m in migration["alerts"]:
        actions.append({"priority": "P1", "area": "migration", "text": m})
    if alloc["changes"]:
        actions.append({"priority": "P1", "area": "allocation",
                        "text": "Review the creative-effort reallocation: " + "; ".join(
                            f"{k} {a:.0%}→{b:.0%}" for k, (a, b) in alloc["changes"].items())})
    for c in cadence:
        if c["action"] in ("increase", "decrease"):
            actions.append({"priority": "P2", "area": "cadence", "text": f"{c['account_id']}: {c['action']} to "
                                                                       f"{c['recommended_per_week']:g}/week — {c['rationale']}"})
    from .community import faqs as _faqs, summary as _community
    cm = _community(db)
    if cm["open_escalations"]:
        actions.append({"priority": "P0", "area": "community",
                        "text": f"{cm['open_escalations']} escalated comment(s) (safety / legal / threats / sponsors) need a person — see Community."})
    if cm["awaiting_approval"]:
        actions.append({"priority": "P2", "area": "community", "text": f"{cm['awaiting_approval']} drafted comment replies are waiting for approval."})
    for f in _faqs(db)[:3]:
        actions.append({"priority": "P2", "area": "community", "text": f"{f['brand_id']}: {f['idea']}"})
    from .competitive import gaps as _gaps
    for gp in _gaps(db)[:4]:
        if b_tier := tier_of(gp["brand_id"], g):
            actions.append({"priority": "P1" if b_tier == "P0" else "P2", "area": "competitive",
                            "text": f"{gp['account_id']} vs {gp['peers']}: " + "; ".join(gp["notes"]) + "."})
    if not exps:
        actions.append({"priority": "P2", "area": "experiments",
                        "text": "No experiments planned — plan this week's hook / duration tests once posting resumes."})
    order = {"P0": 0, "P1": 1, "P2": 2}
    actions.sort(key=lambda a: order.get(a["priority"], 9))
    brief = {"week_start": week, "generated_at": now_iso(), "scoreboard": board, "supply": sup, "allocation": alloc,
             "cadence": [{k: c[k] for k in ("account_id", "action", "recommended_per_week", "confidence", "rationale")}
                         for c in cadence], "experiments": exps,
             "evidence": [{k: e[k] for k in ("entry_id", "statement", "applicability")} for e in evidence],
             "migration": migration, "actions": actions, "community": cm,
             "other_recommendations": [r for r in recommendations(db) if r["type"] not in ("posting_gap",)][:15]}
    if persist:
        bid = f"brief-{week}-{uuid.uuid4().hex[:6]}"
        db.insert("growth_briefs", {"brief_id": bid, "week_start": week, "created_at": now_iso(), "created_by": actor,
                                    "content": brief, "status": "current"})
        db.execute("UPDATE growth_briefs SET status='superseded' WHERE week_start=? AND brief_id<>?", [week, bid])
        brief["brief_id"] = bid
        db.audit(actor, "growth_brief_generated", "growth_briefs", bid, {"actions": len(actions)})
    return brief


def latest_brief(db: DB) -> dict | None:
    rows = db.all("growth_briefs", "status='current'", order="created_at DESC")
    return {**rows[0]["content"], "brief_id": rows[0]["brief_id"]} if rows else None


def apply_allocation(db: DB, brief_id: str, actor: str) -> dict:
    """A person approved the proposed split: it becomes the effort share the scorer uses."""
    row = db.get("growth_briefs", "brief_id", brief_id)
    if not row:
        raise KeyError(brief_id)
    proposed = row["content"]["allocation"]["proposed"]
    db.kv_set(ALLOC_KEY, json.dumps(proposed, sort_keys=True), actor)
    db.audit(actor, "allocation_applied", "growth_briefs", brief_id, {"shares": proposed})
    return proposed


def due(db: DB, today: date | None = None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    week = (today - timedelta(days=today.weekday())).isoformat()
    return not db.one("SELECT brief_id FROM growth_briefs WHERE week_start=? AND status='current'", [week])
