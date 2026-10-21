"""Competitive Intelligence: what comparable accounts publicly do, set against what ours do.

Instagram Business Discovery gives any connected business account read access to another business/creator
account's public numbers: followers, media count and recent posts with likes, comments, type and caption.
No views, reach or watch time — so the comparison metric is engagement per follower ((likes + comments) /
followers), the same calculation for them and for us.

What it writes:
* `competitor_snapshots` — one row per handle per capture (follower growth comes from successive snapshots);
* knowledge entries at the `external_research` level, topic `benchmark` — observations about another
  account, never treated as causal evidence for PLN;
* gap recommendations for our accounts (posting frequency, Reels share, engagement per follower), which the
  Chief Growth Officer brief picks up.
"""
from __future__ import annotations

import json
import re
import statistics
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

from .adapters.base import HttpRequest, urllib_transport
from .config import CONFIG_DIR
from .db import DB, now_iso

FIELDS = ("business_discovery.username({u}){{username,name,followers_count,media_count,"
          "media.limit(30){{id,caption,like_count,comments_count,timestamp,media_type,media_product_type,permalink}}}}")


def config() -> dict:
    return json.loads((CONFIG_DIR / "competitors.json").read_text())


class DiscoveryError(Exception):
    pass


def _dt(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _hook(caption: str) -> str:
    first = (caption or "").strip().split("\n")[0]
    return re.split(r"(?<=[.!?])\s", first)[0][:120]


def profile(media: list[dict], followers: float | None, now: datetime | None = None) -> dict:
    """Same summary for any account: posts/week, Reels share, engagement per follower, caption habits, standouts."""
    now = now or datetime.now(timezone.utc)
    posts = [m for m in media if _dt(m.get("timestamp"))]
    posts.sort(key=lambda m: _dt(m["timestamp"]), reverse=True)
    if not posts:
        return {"posts": 0}
    span_days = max((now - _dt(posts[-1]["timestamp"])).total_seconds() / 86400, 1)
    reels = [m for m in posts if (m.get("media_product_type") or "").upper() == "REELS" or (m.get("media_type") or "").upper() == "VIDEO"]
    def er(m):
        if not followers:
            return None
        return ((m.get("like_count") or 0) + (m.get("comments_count") or 0)) / followers
    ers = [e for e in (er(m) for m in posts) if e is not None]
    med = statistics.median(ers) if ers else None
    caps = [len(m.get("caption") or "") for m in posts]
    tags = [len(re.findall(r"#\w+", m.get("caption") or "")) for m in posts]
    standouts = []
    if med:
        for m in posts:
            e = er(m)
            if e is not None and e >= 2 * med:
                standouts.append({"hook": _hook(m.get("caption", "")), "type": m.get("media_product_type") or m.get("media_type"),
                                  "er": e, "x_median": e / med, "permalink": m.get("permalink"), "at": m.get("timestamp")})
    standouts.sort(key=lambda s: -s["er"])
    hours = [(_dt(m["timestamp"]).hour) for m in posts]
    return {"posts": len(posts), "span_days": round(span_days, 1), "posts_per_week": round(len(posts) / span_days * 7, 1),
            "reels_share": round(len(reels) / len(posts), 2), "median_er": med,
            "median_caption_chars": statistics.median(caps), "median_hashtags": statistics.median(tags),
            "question_hooks": round(sum(1 for m in posts if "?" in _hook(m.get("caption", ""))) / len(posts), 2),
            "common_hours_utc": [h for h, _ in sorted(((h, hours.count(h)) for h in set(hours)), key=lambda x: -x[1])[:3]],
            "standouts": standouts[:5]}


def discover(handle: str, ig_user_id: str, token: str, transport=None) -> dict:
    from .oauth import META_VERSION
    t = transport or urllib_transport
    q = urllib.parse.urlencode({"fields": FIELDS.format(u=handle), "access_token": token})
    resp = t(HttpRequest("GET", f"https://graph.facebook.com/{META_VERSION}/{ig_user_id}?{q}", {"Accept": "application/json"},
                         purpose="instagram business discovery"))
    if resp.status >= 300:
        err = (resp.body or {}).get("error") or {}
        raise DiscoveryError(f"HTTP {resp.status} {err.get('message') or resp.body}"[:300])
    bd = (resp.body or {}).get("business_discovery") or {}
    return {"username": bd.get("username", handle), "name": bd.get("name", ""), "followers": bd.get("followers_count"),
            "media_count": bd.get("media_count"), "media": (bd.get("media") or {}).get("data") or []}


def _viewer(db: DB):
    """A connected Instagram business account to make Business Discovery calls through (PLN first)."""
    accs = db.all("accounts", "platform='instagram' AND verification_status='oauth_connected'")
    accs = [a for a in accs if a.get("stable_platform_id") and (a.get("credential_ref") or "").startswith("vault:")]
    accs.sort(key=lambda a: (a["brand_id"] != "pln", a["account_id"]))
    return accs[0] if accs else None


def _ours(db: DB, now: datetime) -> dict:
    """Our Instagram accounts profiled the same way from the Creative DB."""
    from .audience import _account_series, post_outcomes
    out = {}
    since = now - timedelta(days=60)
    for a in db.all("accounts", "platform='instagram'"):
        posts = [p for p in post_outcomes(db, [a["account_id"]], min_age_h=0) if (_dt(p["published_at"]) or now) >= since]
        if not posts:
            continue
        fol = _account_series(db, a["account_id"], "followers")
        media = [{"timestamp": p["published_at"], "caption": p.get("caption") or "", "like_count": p["metrics"].get("likes"),
                  "comments_count": p["metrics"].get("comments"),
                  "media_product_type": "REELS" if (p.get("post_type") or "").lower() in ("reels", "reel", "clips") else "FEED"}
                 for p in posts]
        out[a["account_id"]] = {"brand_id": a["brand_id"], "handle": a.get("username_original"),
                                "followers": fol[-1][1] if fol else None, **profile(media, fol[-1][1] if fol else None, now)}
    return out


def snapshot(db: DB, transport=None, now: datetime | None = None, actor: str = "competitive-intel", token_for=None) -> dict:
    from .knowledge import add_entry
    from .oauth import access_token
    from .team_params import get as _param
    cfg, now = config(), now or datetime.now(timezone.utc)
    viewer = _viewer(db)
    out = {"captured": [], "errors": {}}
    if not viewer:
        out["errors"]["*"] = "no connected Instagram business account to read Business Discovery through"
        return out
    token = (token_for or access_token)(viewer["credential_ref"][len("vault:"):])
    for b in cfg["benchmarks"]:
        h = b["handle"]
        try:
            d = discover(h, viewer["stable_platform_id"], token, transport)
        except DiscoveryError as e:
            out["errors"][h] = str(e)
            continue
        prof = profile(d["media"], d["followers"], now)
        prev = db.query("SELECT followers, captured_at FROM competitor_snapshots WHERE handle=? ORDER BY captured_at DESC", [h])[:1]
        growth = None
        if prev and prev[0]["followers"] and d["followers"]:
            days = max(((now - _dt(prev[0]["captured_at"])).total_seconds() / 86400), 0.5)
            growth = {"since": str(prev[0]["captured_at"])[:10], "delta": d["followers"] - prev[0]["followers"],
                      "per_week": round((d["followers"] - prev[0]["followers"]) / days * 7)}
        db.insert("competitor_snapshots", {"snapshot_id": f"cs-{uuid.uuid4().hex[:10]}", "handle": h, "platform": "instagram",
                                           "name": d["name"] or b.get("name", ""), "captured_at": now.replace(microsecond=0).isoformat(),
                                           "followers": d["followers"], "media_count": d["media_count"],
                                           "profile": {**prof, "growth": growth}, "compare_to": b.get("compare_to", [])})
        if prof["posts"] >= _param(db, "competitive", "min_posts", cfg["min_posts"]) and prof.get("median_er") is not None:
            stmt = (f"Benchmark @{h} ({b.get('name', h)}): {prof['posts_per_week']:g} posts/week, {prof['reels_share']:.0%} Reels, "
                    f"median {prof['median_er']:.2%} engagement per follower across its last {prof['posts']} posts"
                    + (f"; {len(prof['standouts'])} post(s) at 2×+ its median, led by \"{prof['standouts'][0]['hook'][:70]}\"" if prof["standouts"] else "")
                    + ". Observation about another account, not evidence for ours.")
            add_entry(db, {"entry_id": f"benchmark-instagram-{h}", "level": "external_research", "platform": "instagram",
                           "topic": "benchmark", "statement": stmt, "source_url": f"https://www.instagram.com/{h}/",
                           "source_title": f"@{h} via Instagram Business Discovery", "publisher": "Instagram (public business account)",
                           "source_date": now.date().isoformat(), "applicability": ", ".join(b.get("compare_to", []))}, actor)
        out["captured"].append({"handle": h, "followers": d["followers"], **{k: prof.get(k) for k in ("posts", "posts_per_week", "median_er")}})
    db.audit(actor, "competitors_snapshot", "competitor_snapshots", "", {"captured": len(out["captured"]), "errors": out["errors"]})
    return out


def latest(db: DB) -> list[dict]:
    rows = db.all("competitor_snapshots", order="captured_at DESC")
    seen, out = set(), []
    for r in rows:
        if r["handle"] not in seen:
            seen.add(r["handle"])
            out.append(r)
    return out


def gaps(db: DB, now: datetime | None = None) -> list[dict]:
    """Where our Instagram accounts differ most from the benchmarks they're compared to."""
    now = now or datetime.now(timezone.utc)
    bench = latest(db)
    ours = _ours(db, now)
    recs = []
    for aid, o in ours.items():
        from .team_params import get as _param
        min_posts = _param(db, "competitive", "min_posts", config()["min_posts"])
        peers = [b["profile"] for b in bench if o["brand_id"] in (b.get("compare_to") or []) and (b["profile"] or {}).get("posts", 0) >= min_posts]
        if not peers or not o.get("posts"):
            continue
        ppw = statistics.median(p["posts_per_week"] for p in peers)
        reels = statistics.median(p["reels_share"] for p in peers)
        ers = [p["median_er"] for p in peers if p.get("median_er") is not None]
        er = statistics.median(ers) if ers else None
        names = ", ".join("@" + b["handle"] for b in bench if o["brand_id"] in (b.get("compare_to") or []))
        row = {"account_id": aid, "brand_id": o["brand_id"], "ours": {k: o.get(k) for k in ("posts_per_week", "reels_share", "median_er")},
               "benchmarks": {"posts_per_week": ppw, "reels_share": reels, "median_er": er}, "peers": names, "notes": []}
        if o["posts_per_week"] < 0.5 * ppw:
            row["notes"].append(f"posts {o['posts_per_week']:g}/week vs a benchmark median of {ppw:g} — volume is the first gap "
                                "(test it through the cadence frequency test, don't just copy it)")
        if o["reels_share"] + 0.2 < reels:
            row["notes"].append(f"{o['reels_share']:.0%} Reels vs {reels:.0%} for benchmarks")
        if er and o.get("median_er") is not None and o["median_er"] < 0.5 * er:
            row["notes"].append(f"median engagement per follower {o['median_er']:.2%} vs {er:.2%}")
        if row["notes"]:
            recs.append(row)
    return recs


def standouts(db: DB, brand_id: str = "", limit: int = 12) -> list[dict]:
    out = []
    for b in latest(db):
        if brand_id and brand_id not in (b.get("compare_to") or []):
            continue
        for s in (b["profile"] or {}).get("standouts", []):
            out.append({**s, "handle": b["handle"]})
    out.sort(key=lambda s: -s["x_median"])
    return out[:limit]
