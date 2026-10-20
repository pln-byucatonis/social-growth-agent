"""Analytics ingestion + baselines. Manual CSV import first; official API connectors later.

Platform-native metric names are stored verbatim. A *family* mapping exists only so reports can
line up comparable concepts — we never add Instagram "views" to TikTok "video_views" as if equal.
Missing metrics are reported as `None` / "unknown", never zero-filled or estimated.
"""
from __future__ import annotations

import csv
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from .db import DB, now_iso

# platform -> native metric -> family
METRIC_FAMILIES: dict[str, dict[str, str]] = {
    "instagram": {"followers_count": "followers", "reach": "reach", "views": "views", "plays": "views",
                  "shares": "shares", "saved": "saves", "saves": "saves", "profile_visits": "profile_visits",
                  "follows": "follows", "ig_reels_avg_watch_time": "avg_watch_time_s", "comments": "comments",
                  "likes": "likes", "non_follower_reach": "non_follower_reach", "posts_published": "posts"},
    "tiktok": {"follower_count": "followers", "video_views": "views", "reach": "reach", "shares": "shares",
               "average_time_watched": "avg_watch_time_s", "full_video_watched_rate": "completion_rate",
               "profile_views": "profile_visits", "new_followers": "follows", "comments": "comments",
               "likes": "likes", "saves": "saves", "posts_published": "posts"},
    "facebook": {"page_follows": "followers", "post_impressions_unique": "reach", "post_video_views": "views",
                 "post_video_views_60s": "views_60s", "shares": "shares", "comments": "comments",
                 "page_daily_follows": "follows", "returning_viewers": "returning_viewers", "posts_published": "posts"},
    "linkedin": {"follower_count": "followers", "impressionCount": "impressions", "uniqueImpressionsCount": "reach",
                 "clickCount": "clicks", "shareCount": "shares", "commentCount": "comments",
                 "qualified_inbound_conversations": "qualified_inbound", "posts_published": "posts",
                 "follower_gains": "follows"},
    "youtube": {"subscriberCount": "followers", "views": "views", "averageViewDuration": "avg_watch_time_s",
                "averageViewPercentage": "completion_rate", "shares": "shares", "subscribersGained": "follows",
                "posts_published": "posts"},
    "x": {"followers_count": "followers", "impression_count": "impressions", "retweet_count": "shares",
          "reply_count": "comments", "bookmark_count": "saves", "url_link_clicks": "clicks", "posts_published": "posts"},
    "twitch": {"followers": "followers", "average_viewers": "avg_concurrent", "hours_watched": "watch_hours",
               "unique_chatters": "comments", "streams": "posts"},
}

NORTH_STAR = {
    "linkedin": {"metric": "qualified_inbound", "label": "Qualified inbound conversations"},
    "facebook": {"metric": "returning_viewers", "label": "Returning engaged viewers"},
    "instagram": {"metric": "follows_per_1k_reach", "label": "Non-follower reach → follows"},
    "tiktok": {"metric": "follows_per_1k_views", "label": "Watch time + follows per 1k views"},
    "youtube": {"metric": "avg_watch_time_s", "label": "Watch time / subs gained"},
    "x": {"metric": "clicks_per_1k_impressions", "label": "Clicks + replies during live windows"},
    "twitch": {"metric": "avg_concurrent", "label": "Average concurrent viewers"},
}

CHECKPOINTS = [("2h", timedelta(hours=2)), ("24h", timedelta(hours=24)), ("72h", timedelta(hours=72)),
               ("7d", timedelta(days=7))]


def checkpoint_for(published_at: datetime, captured_at: datetime) -> str:
    age = captured_at - published_at
    best = min(CHECKPOINTS, key=lambda c: abs(c[1] - age))
    return best[0]


def family(platform: str, metric: str) -> str:
    return METRIC_FAMILIES.get(platform, {}).get(metric, "")


def import_csv(db: DB, path: Path, actor: str = "manual") -> dict:
    """Long-format CSV: account_id, metric, value[, platform_post_id, variant_id, checkpoint, period_start, period_end].
    `account_id` may be replaced by `platform` + `username` (matched against the original handle)."""
    accounts = {a["account_id"]: a for a in db.all("accounts")}
    by_handle = {(a["platform"], a["username_original"].lower()): a for a in accounts.values() if a["username_original"]}
    imported, skipped = 0, []
    with open(path, newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            acc = accounts.get((row.get("account_id") or "").strip())
            if not acc and row.get("platform") and row.get("username"):
                acc = by_handle.get((row["platform"].strip().lower(), row["username"].strip().lower()))
            if not acc:
                skipped.append(f"line {i}: unknown account")
                continue
            raw = (row.get("value") or "").strip()
            if raw == "" or raw.lower() in {"unknown", "n/a", "na"}:
                skipped.append(f"line {i}: empty value kept as unknown (not imported as 0)")
                continue
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                skipped.append(f"line {i}: non-numeric value {raw!r}")
                continue
            metric = row["metric"].strip()
            db.insert("metrics", {
                "account_id": acc["account_id"], "variant_id": row.get("variant_id") or None,
                "platform_post_id": row.get("platform_post_id") or None, "platform": acc["platform"],
                "metric": metric, "canonical_metric": family(acc["platform"], metric), "value": value,
                "checkpoint": row.get("checkpoint") or ("snapshot" if not row.get("platform_post_id") else "7d"),
                "period_start": row.get("period_start") or None, "period_end": row.get("period_end") or None,
                "source": "manual_csv", "captured_at": now_iso(),
            })
            imported += 1
    db.audit(actor, "metrics_imported", "metrics", str(path), {"imported": imported, "skipped": skipped})
    return {"imported": imported, "skipped": skipped}


def _vals(rows: list[dict], fam: str, post_level: bool | None = None) -> list[float]:
    out = []
    for r in rows:
        if r["canonical_metric"] != fam:
            continue
        if post_level is True and not r["platform_post_id"]:
            continue
        if post_level is False and r["platform_post_id"]:
            continue
        out.append(r["value"])
    return out


def _median(v: list[float]) -> float | None:
    return statistics.median(v) if v else None


def baseline(db: DB, account_id: str) -> dict:
    """Pre-launch baseline for one account. Every field is a number or None (unknown)."""
    rows = db.all("metrics", "account_id=?", [account_id], order="captured_at")
    acc = db.get("accounts", "account_id", account_id)
    snap_followers = _vals(rows, "followers", post_level=False)
    posts = [r for r in rows if r["platform_post_id"]]
    post_ids = {r["platform_post_id"] for r in posts}
    # use the 7d (or latest) checkpoint per post for per-post medians
    def per_post(fam: str) -> list[float]:
        best: dict[str, tuple[str, float]] = {}
        order = {"2h": 0, "24h": 1, "72h": 2, "7d": 3}
        for r in posts:
            if r["canonical_metric"] != fam:
                continue
            k = r["platform_post_id"]
            rank = order.get(r["checkpoint"], 3)
            if k not in best or rank >= order.get(best[k][0], 0):
                best[k] = (r["checkpoint"], r["value"])
        return [v for _, v in best.values()]

    reach = per_post("reach") or per_post("views") or per_post("impressions")
    profile = sum(per_post("profile_visits")) or sum(_vals(rows, "profile_visits", post_level=False))
    follows = sum(per_post("follows")) or sum(_vals(rows, "follows", post_level=False))
    posts_snapshot = _vals(rows, "posts", post_level=False)
    periods = [r for r in rows if r["canonical_metric"] == "posts" and r["period_start"] and r["period_end"]]
    freq = None
    if periods:
        r = periods[-1]
        days = max(1, (datetime.fromisoformat(r["period_end"]) - datetime.fromisoformat(r["period_start"])).days)
        freq = round(r["value"] / days * 7, 2)
    return {
        "account_id": account_id,
        "platform": acc["platform"] if acc else None,
        "followers": snap_followers[-1] if snap_followers else None,
        "posts_per_week": freq,
        "posts_observed": len(post_ids) or (int(posts_snapshot[-1]) if posts_snapshot else 0),
        "median_reach_per_post": _median(reach),
        "median_watch_time_s": _median(per_post("avg_watch_time_s")),
        "median_completion_rate": _median(per_post("completion_rate")),
        "median_shares_per_post": _median(per_post("shares")),
        "median_saves_per_post": _median(per_post("saves")),
        "profile_to_follow_conversion": round(follows / profile, 4) if profile and follows else None,
        "data_source": sorted({r["source"] for r in rows}) or ["none — no analytics imported yet"],
    }


def rate_per_1k(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * 1000, 2)


def post_performance(db: DB, variant_id: str) -> dict:
    """Latest checkpoint values (by family) for a published variant."""
    rows = db.all("metrics", "variant_id=?", [variant_id])
    out: dict[str, float] = {}
    order = {"2h": 0, "24h": 1, "72h": 2, "7d": 3, "snapshot": 3}
    seen: dict[str, int] = {}
    for r in rows:
        fam = r["canonical_metric"] or r["metric"]
        rank = order.get(r["checkpoint"], 0)
        if fam not in seen or rank >= seen[fam]:
            seen[fam] = rank
            out[fam] = r["value"]
    denom = out.get("views") or out.get("reach") or out.get("impressions")
    out["follows_per_1k_views"] = rate_per_1k(out.get("follows"), denom)
    out["shares_per_1k_views"] = rate_per_1k(out.get("shares"), denom)
    out["follows_per_1k_reach"] = rate_per_1k(out.get("follows"), out.get("reach"))
    out["clicks_per_1k_impressions"] = rate_per_1k(out.get("clicks"), out.get("impressions"))
    return out


def fmt(v) -> str:
    return "unknown" if v is None else (f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
