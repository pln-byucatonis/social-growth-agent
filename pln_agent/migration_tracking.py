"""Audience Migration tracking: WPL → Putt Tour from the measured numbers, every day.

* Trends — follower series for the legacy (WPL) and canonical (Putt Tour) account on each platform, with
  7- and 28-day change, plus Instagram's 28-day follows / unfollows where collected.
* Alerts — the legacy account shrinking faster than the threshold while the canonical one isn't growing,
  the canonical account shrinking, either account silent too long, or bridge posts overdue.
* Bridge posts — legacy posts that point to Putt Tour (route_role legacy_bridge, or a caption naming
  Putt Tour / the canonical handle). For each one we compare the canonical account's follower change over
  the next 3 days with its typical 3-day change on days without a bridge post. Observational: other things
  happen on those days too, so it's reported as an association with its sample size, never as transfer.
* Recommendation — per platform, which account should be home, from followers, net change, reach per post
  and engagement per 1k views. Follower transfer is never assumed.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone

from .db import DB, now_iso
from .migration import load_config

RULES = {"decline_pct_28d": 0.02, "silent_days": 14, "bridge_window_days": 3, "bridge_every_days": 7}


def _series(db: DB, account_id: str) -> list[tuple[date, float]]:
    from .audience import _account_series
    return [(date.fromisoformat(d), v) for d, v in _account_series(db, account_id, "followers")]


def _change(series: list[tuple[date, float]], days: int, today: date) -> float | None:
    if not series:
        return None
    last_d, last_v = series[-1]
    before = [v for d, v in series if d <= last_d - timedelta(days=days)]
    if not before or (today - last_d).days > 3:
        return None
    return last_v - before[-1]


def _latest_metric(db: DB, account_id: str, fam: str) -> float | None:
    r = db.query("SELECT value FROM metrics WHERE account_id=? AND canonical_metric=? AND platform_post_id IS NULL "
                 "ORDER BY period_end DESC, captured_at DESC", [account_id, fam])[:1]
    return r[0]["value"] if r else None


def _last_post(db: DB, account_id: str) -> str | None:
    r = db.one("SELECT MAX(published_at) AS m FROM posts WHERE account_id=?", [account_id])
    return str(r["m"])[:10] if r and r["m"] else None


def account_trend(db: DB, account_id: str | None, today: date) -> dict | None:
    if not account_id:
        return None
    acc = db.get("accounts", "account_id", account_id) or {}
    s = _series(db, account_id)
    last = _last_post(db, account_id)
    return {"account_id": account_id, "handle": acc.get("username_original") or account_id,
            "followers": s[-1][1] if s else None, "as_of": s[-1][0].isoformat() if s else None,
            "change_7d": _change(s, 7, today), "change_28d": _change(s, 28, today),
            "follows_28d": _latest_metric(db, account_id, "follows_window"),
            "unfollows_28d": _latest_metric(db, account_id, "unfollows_window"),
            "last_post": last, "days_since_post": (today - date.fromisoformat(last)).days if last else None,
            "series": [(d.isoformat(), v) for d, v in s[-90:]]}


def _is_bridge(p: dict, canon_handles: set[str]) -> bool:
    if (p.get("creative") or {}).get("route_role") == "legacy_bridge":
        return True
    cap = (p.get("caption") or "").lower()
    return "putt tour" in cap or "putttour" in cap or any(h and ("@" + h) in cap for h in canon_handles)


def bridge_results(db: DB, legacy_id: str, canon_id: str | None, today: date) -> dict:
    canon = db.get("accounts", "account_id", canon_id) if canon_id else None
    handles = {((canon or {}).get("username_normalized") or "").lower().lstrip("@")}
    posts = db.all("posts", "account_id=?", [legacy_id], order="published_at")
    bridges = [p for p in posts if _is_bridge(p, handles)]
    out = {"bridge_posts": len(bridges), "last_bridge": str(bridges[-1]["published_at"])[:10] if bridges else None, "rows": [],
           "n_with_data": 0, "median_gain_after_bridge": None, "median_gain_other_days": None, "n_baseline": 0}
    if not canon_id:
        out["note"] = "no separate Putt Tour account on this platform — WPL itself carries the rebrand"
        return out
    s = dict(_series(db, canon_id))
    w = RULES["bridge_window_days"]

    def gain(d0: date) -> float | None:
        start = s.get(d0 - timedelta(days=1), s.get(d0))  # snapshot the day before, else the same day
        end = s.get(d0 + timedelta(days=w))
        return (end - start) if start is not None and end is not None else None

    bridge_days = {date.fromisoformat(str(p["published_at"])[:10]) for p in bridges}
    after = [g for d in sorted(bridge_days) if (g := gain(d)) is not None]
    near = {d + timedelta(days=k) for d in bridge_days for k in range(-w, w + 1)}
    baseline = [g for d in sorted(s) if d not in near and (g := gain(d)) is not None]
    for p in bridges[-10:]:
        d = date.fromisoformat(str(p["published_at"])[:10])
        out["rows"].append({"date": d.isoformat(), "caption": (p.get("caption") or "")[:90], "permalink": p.get("permalink"),
                            "canonical_gain_next_3d": gain(d)})
    out.update({"n_with_data": len(after), "median_gain_after_bridge": statistics.median(after) if after else None,
                "median_gain_other_days": statistics.median(baseline) if baseline else None, "n_baseline": len(baseline)})
    if len(after) >= 5 and len(baseline) >= 5:
        from .creative_db import permutation_p
        out["p"] = permutation_p(after, baseline)
    out["note"] = ("association only: other things happen on bridge days too; follower transfer can't be measured"
                   if after else "no follower snapshots around bridge posts yet")
    return out


def recommend(legacy: dict | None, canon: dict | None, option: str) -> dict:
    if not canon:
        if not legacy or legacy["followers"] is None:
            return {"home": "legacy", "confidence": "audit required", "why": "No follower data yet for the WPL account."}
        return {"home": "legacy (rebrand in place)", "confidence": "measured",
                "why": f"No separate Putt Tour account on this platform; WPL has {legacy['followers']:,.0f} followers — "
                       "rebranding it keeps them, a new account would start from zero."}
    if not legacy or legacy["followers"] is None or canon["followers"] is None:
        return {"home": "putt_tour", "confidence": "audit required",
                "why": "Follower data missing for one of the two accounts; keep Putt Tour as home (reversible default)."}
    ratio = legacy["followers"] / max(canon["followers"], 1)
    lnet, cnet = legacy.get("change_28d"), canon.get("change_28d")
    if ratio >= 2 and (lnet is None or lnet >= 0):
        return {"home": "legacy (rebrand in place)", "confidence": "measured",
                "why": f"WPL has {ratio:.1f}× Putt Tour's followers and isn't shrinking — moving home would leave most of the audience behind."}
    why = f"WPL/Putt Tour follower ratio {ratio:.1f}×"
    if lnet is not None and lnet < 0:
        why += f"; WPL {lnet:+,.0f} in 28 days"
    if cnet is not None:
        why += f"; Putt Tour {cnet:+,.0f}"
    return {"home": "putt_tour", "confidence": "measured", "why": why + ". Keep Putt Tour as home and bridge WPL."}


def track(db: DB, today: date | None = None, persist: bool = True, actor: str = "migration-tracker") -> dict:
    from .team_params import get as _param
    today = today or datetime.now(timezone.utc).date()
    cfg = load_config()
    platforms, alerts = [], []
    rules = {k: _param(db, "migration", k, v) for k, v in RULES.items()}
    for platform, d in cfg["platforms"].items():
        canon_id = d["canonical_account_id"] if d["canonical_account_id"] != d["legacy_account_id"] else None
        leg, can = account_trend(db, d["legacy_account_id"], today), account_trend(db, canon_id, today)
        br = bridge_results(db, d["legacy_account_id"], canon_id, today)
        rec = recommend(leg, can, d["option"])
        row = {"platform": platform, "option": d["option"], "legacy": leg, "canonical": can, "bridge": br, "recommendation": rec}
        platforms.append(row)
        for who, t in (("WPL", leg), ("Putt Tour", can)):
            if not t:
                continue
            ch, f = t["change_28d"], t["followers"]
            if ch is not None and f and ch < 0 and -ch / (f - ch) >= rules["decline_pct_28d"]:
                alerts.append({"priority": "P0" if who == "Putt Tour" else "P1", "platform": platform,
                               "text": f"{who} {platform} down {ch:+,.0f} followers in 28 days ({-ch / (f - ch):.1%})."})
            elif t.get("unfollows_28d") and t.get("follows_28d") is not None and t["unfollows_28d"] > t["follows_28d"]:
                alerts.append({"priority": "P1", "platform": platform,
                               "text": f"{who} {platform}: {t['unfollows_28d']:,.0f} unfollows vs {t['follows_28d']:,.0f} follows in 28 days."})
            if t["days_since_post"] is None or t["days_since_post"] > rules["silent_days"]:
                alerts.append({"priority": "P1", "platform": platform,
                               "text": f"{who} {platform} " + (f"silent for {t['days_since_post']} days" if t["days_since_post"] is not None else "has no posts on record")
                                       + " — an inactive account loses reach and followers."})
        if canon_id and d["option"] in ("bridge_to_canonical", "redirect_to_new_account"):
            lb = br.get("last_bridge")
            if not lb or (today - date.fromisoformat(lb)).days > rules["bridge_every_days"]:
                alerts.append({"priority": "P1", "platform": platform,
                               "text": f"No bridge post on WPL {platform} " + (f"since {lb}" if lb else "on record")
                                       + f" — schedule one pointing to {can['handle'] if can else 'Putt Tour'}."})
        if rec["confidence"] == "measured" and not rec["home"].startswith(("putt_tour",)) and d["option"] in ("bridge_to_canonical", "redirect_to_new_account"):
            alerts.append({"priority": "P1", "platform": platform,
                           "text": f"{platform}: the numbers point to '{rec['home']}' but the configured option is {d['option']} — revisit the decision."})
        if rec["confidence"] == "measured" and rec["home"] == "putt_tour" and d["option"] in ("keep_legacy_temporarily", "inplace_rebrand") and canon_id:
            alerts.append({"priority": "P1", "platform": platform,
                           "text": f"{platform}: the numbers point to Putt Tour as home but the configured option is {d['option']} — revisit the decision."})
    order = {"P0": 0, "P1": 1, "P2": 2}
    alerts.sort(key=lambda a: order.get(a["priority"], 9))
    out = {"generated_at": now_iso(), "platforms": platforms, "alerts": alerts, "rules": rules}
    if persist:
        db.kv_set("migration:tracking", json.dumps(out, default=str), actor)
    return out


def latest(db: DB) -> dict | None:
    raw = db.kv_get("migration:tracking")
    return json.loads(raw) if raw else None
