"""Audience Intelligence: real performance by account, brand, platform, format and content type.

Data sources (official APIs only):
* The ops portal already collects platform data through the Instagram/YouTube APIs into
  `social_account_metric_daily`, `social_post` and `social_post_metric_daily` (read-only here).
* Accounts connected through the agent's OAuth flow can be read by the platform collectors in
  `insights.py` (Phase 3 wiring; same `metrics` rows).

Every KPI carries a `basis`:
* `measured`    — a number the platform reported (or a ratio of two reported numbers on the same posts)
* `derived`     — computed from measured numbers with an attribution assumption, stated in `how`
* `unavailable` — no current integration exposes it; `how` says what would
Unreported fields are never zero-filled: a field that is 0 on every row for an account is treated as
"not reported by the platform" (Instagram stopped reporting impressions, for example) and skipped.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB, now_iso
from .metrics import checkpoint_for

OPS_ACCOUNT_FIELDS = {"followers": "followers", "reach": "reach", "profile_views": "profile_visits",
                      "impressions": "impressions", "engagements": "engagements"}
OPS_ACCOUNT_EXTRA = {"newFollowers": "follows", "mediaCount": "posts_total", "website_clicks": "clicks"}
OPS_POST_FIELDS = {"views": "views", "impressions": "impressions", "engagements": "engagements", "reactions": "likes",
                   "comments": "comments", "shares": "shares", "link_clicks": "clicks"}
OPS_POST_EXTRA = {"reach": "reach", "saved": "saves", "minutes_watched": "watch_minutes",
                  "avg_watch_time_s": "avg_watch_time_s", "ig_reels_avg_watch_time": "avg_watch_time_s"}

KPI_LABELS = {
    "non_follower_reach": "Non-follower reach",
    "follows_per_1k_views": "Followers gained per 1,000 views",
    "avg_watch_time_s": "Average watch time (s)",
    "shares_per_1k_views": "Shares per 1,000 views",
    "returning_viewers": "Returning viewers",
    "profile_visits_per_1k_views": "Profile visits per 1,000 views",
    "outbound_clicks_per_1k_views": "Outbound clicks per 1,000 views",
    "saves_per_1k_views": "Saves per 1,000 views",
    "engagements_per_1k_views": "Engagements per 1,000 views",
}

# What each platform's official API can expose for the six categories, and what that takes.
MEASURABILITY = {
    "instagram": {
        "non_follower_reach": "Instagram Graph API media/account insights with follow_type breakdown (instagram_manage_insights)",
        "follows_per_1k_views": "account 'follows' insight per day (instagram_manage_insights); per-post follows on Reels insights",
        "avg_watch_time_s": "Reels insight ig_reels_avg_watch_time",
        "shares_per_1k_views": "media insights shares + views",
        "returning_viewers": "not exposed by the Instagram API",
        "profile_visits_per_1k_views": "account insight profile_views",
        "outbound_clicks_per_1k_views": "account insight website_clicks / profile_links_taps",
    },
    "facebook": {
        "non_follower_reach": "Page post insights (non-follower reach breakdown where offered)",
        "follows_per_1k_views": "page_daily_follows_unique",
        "avg_watch_time_s": "post_video_avg_time_watched",
        "shares_per_1k_views": "post shares + post_video_views",
        "returning_viewers": "Page insights returning viewers (Professional Dashboard / API where offered)",
        "profile_visits_per_1k_views": "page_views_total",
        "outbound_clicks_per_1k_views": "post_clicks_by_type (link clicks)",
    },
    "youtube": {
        "non_follower_reach": "YouTube Analytics subscribedStatus dimension (views from non-subscribers)",
        "follows_per_1k_views": "YouTube Analytics subscribersGained per video",
        "avg_watch_time_s": "averageViewDuration",
        "shares_per_1k_views": "shares + views",
        "returning_viewers": "Studio returning viewers (not in the public Analytics API)",
        "profile_visits_per_1k_views": "not exposed",
        "outbound_clicks_per_1k_views": "cardClicks / endScreen clicks",
    },
    "tiktok": {
        "non_follower_reach": "not exposed by the Display/Content Posting APIs (TikTok Business API for ads only)",
        "follows_per_1k_views": "account follower_count over time (user.info.stats); per-video follows not exposed",
        "avg_watch_time_s": "not exposed by the Display API",
        "shares_per_1k_views": "video.list share_count + view_count",
        "returning_viewers": "not exposed",
        "profile_visits_per_1k_views": "not exposed",
        "outbound_clicks_per_1k_views": "not exposed",
    },
    "x": {"non_follower_reach": "not exposed", "follows_per_1k_views": "followers_count over time",
          "avg_watch_time_s": "not exposed on standard tiers", "shares_per_1k_views": "public_metrics retweets + impressions",
          "returning_viewers": "not exposed", "profile_visits_per_1k_views": "non_public_metrics user_profile_clicks (own posts)",
          "outbound_clicks_per_1k_views": "non_public_metrics url_link_clicks (own posts)"},
    "linkedin": {"non_follower_reach": "not exposed", "follows_per_1k_views": "organizationalEntityFollowerStatistics",
                 "avg_watch_time_s": "videoAnalytics (where available)", "shares_per_1k_views": "shareStatistics shareCount",
                 "returning_viewers": "not exposed", "profile_visits_per_1k_views": "organizationPageStatistics views",
                 "outbound_clicks_per_1k_views": "shareStatistics clickCount"},
}

OVERLAP_QUESTIONS = [
    {"question": "Are PLN viewers also following CarJitsu?", "measurable": False,
     "why": "No platform API exposes follower identities or cross-account follower overlap.",
     "proxies": ["audience demographics/geography per account (Instagram follower_demographics) — similarity, not overlap",
                 "collab posts (shared to both accounts) — reach on each side",
                 "profile visits / follows on the receiving account in the 48h after a cross-promo post (inferred)"]},
    {"question": "Are CarJitsu fans discovering Putt Tour?", "measurable": False,
     "why": "Same: identities aren't exposed.",
     "proxies": ["UTM-tagged link-in-bio clicks from CarJitsu to Putt Tour", "Putt Tour follows after CarJitsu cross-promos (inferred)"]},
    {"question": "Is WPL's existing audience moving toward Putt Tour?", "measurable": False,
     "why": "We can't see who unfollows WPL and follows Putt Tour.",
     "proxies": ["WPL vs Putt Tour follower trends during bridge windows (measured counts, inferred movement)",
                 "link clicks / profile visits from WPL bridge posts (measured where the API exposes them)"]},
    {"question": "Which sports generate entirely new audiences for PLN?", "measurable": "partly",
     "why": "Non-follower reach by post is measurable on Instagram/YouTube once insights scopes are connected; 'new to PLN overall' is not.",
     "proxies": ["non-follower reach share by sport (Instagram follow_type breakdown, YouTube subscribedStatus)",
                 "follows per 1k views by sport"]},
]


def _extra(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).replace("Z", "+00:00").replace(" ", "T", 1)
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def import_ops_social(db: DB, ops: DB, tz: str = "America/New_York", actor: str = "ops-social") -> dict:
    """Mirror the ops portal's platform data into `metrics` + the Creative Intelligence DB. Idempotent."""
    from .creative_db import upsert_ingested_post
    accounts = {a["ops_account_id"]: a for a in db.all("accounts") if a.get("ops_account_id")}
    report = {"accounts": 0, "account_rows": 0, "posts": 0, "post_rows": 0, "unmapped_ops_accounts": [],
              "not_reported": []}
    # account daily
    rows = ops.query("SELECT account_id, metric_date, followers, reach, profile_views, impressions, engagements, "
                     "platform_extra, source FROM social_account_metric_daily ORDER BY account_id, metric_date")
    nonzero: dict[tuple[int, str], bool] = {}
    for r in rows:
        ex = _extra(r["platform_extra"])
        for f in OPS_ACCOUNT_FIELDS:
            nonzero[(r["account_id"], f)] = nonzero.get((r["account_id"], f), False) or bool(_num(r[f]))
        for f in OPS_ACCOUNT_EXTRA:
            nonzero[(r["account_id"], f)] = nonzero.get((r["account_id"], f), False) or bool(_num(ex.get(f)))
    out_rows, seen_acc = [], set()
    for r in rows:
        acc = accounts.get(r["account_id"])
        if not acc:
            if r["account_id"] not in report["unmapped_ops_accounts"]:
                report["unmapped_ops_accounts"].append(r["account_id"])
            continue
        seen_acc.add(acc["account_id"])
        d = str(r["metric_date"])[:10]
        ex = _extra(r["platform_extra"])
        vals = [(f, fam, _num(r[f])) for f, fam in OPS_ACCOUNT_FIELDS.items()]
        vals += [(f, fam, _num(ex.get(f))) for f, fam in OPS_ACCOUNT_EXTRA.items()]
        for f, fam, v in vals:
            if v is None:
                continue
            if not nonzero.get((r["account_id"], f)) and f != "followers":
                continue  # all-zero series = not reported by the platform, not a measured zero
            out_rows.append({"account_id": acc["account_id"], "variant_id": None, "platform_post_id": None,
                             "platform": acc["platform"], "metric": f, "canonical_metric": fam, "value": v,
                             "checkpoint": "snapshot", "period_start": d, "period_end": d, "source": "ops_social",
                             "captured_at": now_iso(), "metric_key": f"ops:acct:{r['account_id']}:{d}:{f}"})
    for (aid, f), nz in nonzero.items():
        if not nz and f != "followers" and aid in accounts:
            report["not_reported"].append(f"{accounts[aid]['account_id']}: {f}")
    report["account_rows"] = db.insert_many("metrics", out_rows, upsert_key="metric_key")
    report["accounts"] = len(seen_acc)

    # posts + post daily
    posts = ops.query("SELECT id, account_id, platform_post_id, published_at, text, permalink, post_type, media, origin "
                      "FROM social_post ORDER BY id")
    by_ops_post = {}
    for p in posts:
        acc = accounts.get(p["account_id"])
        if not acc:
            continue
        rec = upsert_ingested_post(db, acc, p, tz)
        by_ops_post[p["id"]] = (acc, rec)
        report["posts"] += 1
    prow = ops.query("SELECT post_id, metric_date, impressions, engagements, reactions, comments, shares, link_clicks, "
                     "views, platform_extra, collected_at FROM social_post_metric_daily ORDER BY post_id, metric_date")
    pnz: dict[tuple[int, str], bool] = {}
    for r in prow:
        ex = _extra(r["platform_extra"])
        for f in OPS_POST_FIELDS:
            pnz[(r["post_id"], f)] = pnz.get((r["post_id"], f), False) or bool(_num(r[f]))
    out_rows = []
    for r in prow:
        if r["post_id"] not in by_ops_post:
            continue
        acc, rec = by_ops_post[r["post_id"]]
        ex = _extra(r["platform_extra"])
        pub, cap = _dt(rec["published_at"]), _dt(r["collected_at"]) or _dt(str(r["metric_date"]))
        ckpt = checkpoint_for(pub, cap) if pub and cap else "snapshot"
        d = str(r["metric_date"])[:10]
        vals = [(f, fam, _num(r[f])) for f, fam in OPS_POST_FIELDS.items() if pnz.get((r["post_id"], f))
                or f in ("comments", "shares")]  # 0 comments/shares on a post with views is a real zero
        vals += [(f, fam, _num(ex.get(f))) for f, fam in OPS_POST_EXTRA.items()]
        for f, fam, v in vals:
            if v is None:
                continue
            out_rows.append({"account_id": acc["account_id"], "variant_id": rec.get("variant_id") or None,
                             "platform_post_id": rec["platform_post_id"], "platform": acc["platform"], "metric": f,
                             "canonical_metric": fam, "value": v, "checkpoint": ckpt, "period_start": d,
                             "period_end": d, "source": "ops_social",
                             "captured_at": cap.replace(microsecond=0).isoformat() if cap else now_iso(),
                             "metric_key": f"ops:post:{r['post_id']}:{d}:{f}"})
    report["post_rows"] = db.insert_many("metrics", out_rows, upsert_key="metric_key")
    db.audit(actor, "ops_social_imported", "metrics", "", report)
    return report


# ---- KPI model ----------------------------------------------------------------------------------

def _k(value, basis, how, n=None):
    return {"value": None if value is None else round(value, 3), "basis": basis, "how": how, "n": n}


def post_outcomes(db: DB, account_ids: list[str] | None = None, min_age_h: float = 72) -> list[dict]:
    """One row per post: creative + latest lifetime metrics captured at least `min_age_h` after publishing
    (falls back to the latest capture, flagged `young`)."""
    where, params = "1=1", []
    if account_ids:
        where = "account_id IN (" + ",".join("?" for _ in account_ids) + ")"
        params = list(account_ids)
    posts = db.all("posts", where, params, order="published_at")
    if not posts:
        return []
    ids = [p["platform_post_id"] for p in posts]
    rows = []
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        rows += db.query("SELECT platform_post_id, canonical_metric, value, captured_at FROM metrics WHERE platform_post_id IN ("
                         + ",".join("?" for _ in part) + ")", part)
    latest: dict[str, dict[str, tuple[str, float]]] = {}
    for r in rows:
        fam = r["canonical_metric"]
        if not fam:
            continue
        cur = latest.setdefault(r["platform_post_id"], {})
        if fam not in cur or str(r["captured_at"]) >= cur[fam][0]:
            cur[fam] = (str(r["captured_at"]), r["value"])
    out = []
    for p in posts:
        m = {k: v for k, (_, v) in latest.get(p["platform_post_id"], {}).items()}
        caps = [c for c, _ in latest.get(p["platform_post_id"], {}).values()]
        pub = _dt(p["published_at"])
        last = _dt(max(caps)) if caps else None
        age_h = (last - pub).total_seconds() / 3600 if pub and last else None
        views = m.get("views") or None
        per1k = (lambda k: (m[k] / views * 1000) if views and m.get(k) is not None else None)
        out.append({**p, "metrics": m, "age_h": age_h, "young": age_h is None or age_h < min_age_h,
                    "outcomes": {"views": views, "reach": m.get("reach"),
                                 "shares_per_1k_views": per1k("shares"), "saves_per_1k_views": per1k("saves"),
                                 "comments_per_1k_views": per1k("comments"), "likes_per_1k_views": per1k("likes"),
                                 "engagements_per_1k_views": per1k("engagements"),
                                 "avg_watch_time_s": m.get("avg_watch_time_s")}})
    return out


def _account_series(db: DB, account_id: str, fam: str) -> list[tuple[str, float]]:
    rows = db.query("SELECT period_start, value FROM metrics WHERE account_id=? AND canonical_metric=? AND "
                    "platform_post_id IS NULL AND checkpoint='snapshot' ORDER BY period_start", [account_id, fam])
    seen: dict[str, float] = {}
    for r in rows:
        if r["period_start"]:
            seen[str(r["period_start"])[:10]] = r["value"]
    return sorted(seen.items())


def account_kpis(db: DB, account_id: str, days: int = 28, end: date | None = None) -> dict:
    acc = db.get("accounts", "account_id", account_id)
    followers = _account_series(db, account_id, "followers")
    if end is None:
        end = date.fromisoformat(followers[-1][0]) if followers else datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    win = [(d, v) for d, v in followers if start.isoformat() <= d <= end.isoformat()]
    posts = [p for p in post_outcomes(db, [account_id])
             if p["published_at"] and start.isoformat() <= str(p["published_at"])[:10] <= end.isoformat()]
    measured = [p for p in posts if p["outcomes"]["views"]]
    views = sum(p["outcomes"]["views"] for p in measured)
    tot = lambda k: sum(p["metrics"].get(k, 0) or 0 for p in measured)
    has = lambda k: any(k in p["metrics"] for p in measured)
    k = {}
    k["non_follower_reach"] = _k(None, "unavailable", MEASURABILITY.get(acc["platform"], {}).get("non_follower_reach", "not exposed"))
    follows_daily = _account_series(db, account_id, "follows")
    fw = [v for d, v in follows_daily if start.isoformat() <= d <= end.isoformat()]
    if fw and views:
        k["follows_per_1k_views"] = _k(sum(fw) / views * 1000, "derived",
                                       "platform-reported daily follows ÷ views of posts published in the window "
                                       "(follows can come from older posts or the profile)", len(measured))
    elif len(win) >= 2 and views:
        net = win[-1][1] - win[0][1]
        k["follows_per_1k_views"] = _k(net / views * 1000, "derived",
                                       f"net follower change {net:+.0f} ({win[0][0]}→{win[-1][0]}) ÷ views of posts "
                                       "published in the window; account-level, not per-post attribution", len(measured))
    else:
        k["follows_per_1k_views"] = _k(None, "unavailable", "needs ≥2 daily follower snapshots and measured post views")
    k["avg_watch_time_s"] = (_k(statistics.median([p["outcomes"]["avg_watch_time_s"] for p in measured
                                                   if p["outcomes"]["avg_watch_time_s"] is not None]), "measured",
                                "median per-post average watch time", len(measured)) if has("avg_watch_time_s") else
                             _k(None, "unavailable", MEASURABILITY.get(acc["platform"], {}).get("avg_watch_time_s", "not exposed")))
    for key, fam in (("shares_per_1k_views", "shares"), ("saves_per_1k_views", "saves"),
                     ("engagements_per_1k_views", "engagements")):
        k[key] = (_k(tot(fam) / views * 1000, "measured", f"Σ{fam} ÷ Σviews over {len(measured)} posts", len(measured))
                  if views and has(fam) else _k(None, "unavailable", f"no {fam} + views reported for posts in window"))
    k["returning_viewers"] = _k(None, "unavailable", MEASURABILITY.get(acc["platform"], {}).get("returning_viewers", "not exposed"))
    pv = [v for d, v in _account_series(db, account_id, "profile_visits") if start.isoformat() <= d <= end.isoformat()]
    k["profile_visits_per_1k_views"] = (_k(sum(pv) / views * 1000, "derived",
                                           "account profile visits ÷ views of posts published in the window", len(measured))
                                        if pv and views else _k(None, "unavailable", "no profile-visit series"))
    k["outbound_clicks_per_1k_views"] = (_k(tot("clicks") / views * 1000, "measured", "Σclicks ÷ Σviews", len(measured))
                                         if views and tot("clicks") else
                                         _k(None, "unavailable", MEASURABILITY.get(acc["platform"], {}).get("outbound_clicks_per_1k_views", "not exposed")))
    return {"account_id": account_id, "brand_id": acc["brand_id"], "platform": acc["platform"],
            "handle": acc["username_original"], "window": [start.isoformat(), end.isoformat()],
            "followers_start": win[0][1] if win else None, "followers_end": win[-1][1] if win else None,
            "net_follower_change": (win[-1][1] - win[0][1]) if len(win) >= 2 else None,
            "posts": len(posts), "posts_measured": len(measured), "views": views or None, "kpis": k}


def breakdown(db: DB, by: str = "brand_id", account_ids: list[str] | None = None, min_age_h: float = 72) -> list[dict]:
    """Per-post outcome medians grouped by brand_id | platform | post_type | content_type (all measured)."""
    groups: dict[str, list[dict]] = {}
    for p in post_outcomes(db, account_ids, min_age_h):
        if p["young"] or not p["outcomes"]["views"]:
            continue
        key = (p.get("creative") or {}).get(by) if by == "content_type" else p.get(by)
        groups.setdefault(key or "unknown", []).append(p)
    out = []
    for key, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        med = lambda k: statistics.median(v for v in (p["outcomes"][k] for p in ps) if v is not None) \
            if any(p["outcomes"][k] is not None for p in ps) else None
        out.append({"group": key, "posts": len(ps), "median_views": med("views"),
                    "median_shares_per_1k_views": med("shares_per_1k_views"),
                    "median_saves_per_1k_views": med("saves_per_1k_views"),
                    "median_engagements_per_1k_views": med("engagements_per_1k_views")})
    return out


def portfolio(db: DB, days: int = 28) -> list[dict]:
    """KPIs for every account that has any measured data."""
    ids = [r["account_id"] for r in db.query("SELECT DISTINCT account_id FROM metrics")]
    return [account_kpis(db, a, days) for a in sorted(ids) if db.get("accounts", "account_id", a)]


def follower_trend(db: DB, account_id: str) -> list[tuple[str, float]]:
    return _account_series(db, account_id, "followers")


def local_parts(published_at, tz: str) -> tuple[int | None, int | None]:
    d = _dt(published_at)
    if not d:
        return None, None
    d = d.astimezone(ZoneInfo(tz))
    return d.hour, d.weekday()
