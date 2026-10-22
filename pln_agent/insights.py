"""Platform insights collectors for accounts connected through the agent's OAuth flow.

YouTube (Data API v3 + YouTube Analytics API v2, scopes youtube.readonly + yt-analytics.readonly):
* channel snapshot      — subscribers, total views, video count (channels.list statistics)
* channel daily series  — views, watch minutes, average view duration, subscribers gained/lost, shares,
                          likes, comments per day (reports, dimensions=day)
* audience split        — views from subscribers vs non-subscribers over the window (dimensions=subscribedStatus):
                          the Audience Intelligence "non-follower reach" category, measured
* recent uploads        — every upload in the lookback window becomes a Creative Intelligence `posts` row
                          (title, publish time, duration; Short vs long-form inferred from duration)
* per-video lifetime    — views, watch minutes, average view duration and percentage, subscribers gained,
                          shares, likes, comments (reports, dimensions=video)

Rows land in `metrics` (source `platform_api`, idempotent `metric_key`), so the KPI model, the creative
evidence engine and the cadence engine use them unchanged. Nothing is estimated: a metric the API doesn't
return is simply absent. Quota: ~4 Data API units + 4 Analytics queries per channel per run.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from .adapters.base import HttpRequest, urllib_transport
from .db import DB, now_iso
from .metrics import checkpoint_for

YT = "https://www.googleapis.com/youtube/v3"
YTA = "https://youtubeanalytics.googleapis.com/v2/reports"
LOOKBACK_DAYS = 90
SHORT_MAX_S = 180  # YouTube Shorts can be up to 3 minutes; longer is certainly long-form

DAILY_METRICS = {"views": "views", "estimatedMinutesWatched": "watch_minutes", "averageViewDuration": "avg_watch_time_s",
                 "subscribersGained": "follows", "subscribersLost": "unfollows", "shares": "shares", "likes": "likes",
                 "comments": "comments"}
VIDEO_METRICS = {"views": "views", "estimatedMinutesWatched": "watch_minutes", "averageViewDuration": "avg_watch_time_s",
                 "averageViewPercentage": "completion_rate", "subscribersGained": "follows", "shares": "shares",
                 "likes": "likes", "comments": "comments"}


class CollectorError(Exception):
    pass


def iso_duration_s(s: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + se)


class YouTubeCollector:
    def __init__(self, db: DB, transport=None, tz: str = "America/New_York", today: date | None = None):
        self.db, self.transport = db, transport or urllib_transport
        self.tz, self.today = tz, today or datetime.now(timezone.utc).date()

    def _get(self, url: str, params: dict, token: str) -> dict:
        resp = self.transport(HttpRequest("GET", url + "?" + urllib.parse.urlencode(params),
                                          {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                          purpose="youtube insights"))
        if resp.status >= 300:
            err = (resp.body or {}).get("error") or {}
            raise CollectorError(f"{url.rsplit('/', 1)[-1]}: HTTP {resp.status} {err.get('message') or resp.body}"[:300])
        return resp.body or {}

    def _report(self, token: str, params: dict) -> list[dict]:
        body = self._get(YTA, {"ids": "channel==MINE", **params}, token)
        cols = [c["name"] for c in body.get("columnHeaders", [])]
        return [dict(zip(cols, r)) for r in body.get("rows") or []]

    def collect(self, acc: dict, token: str) -> dict:
        ch = acc["stable_platform_id"]
        aid, rows = acc["account_id"], []
        start = (self.today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        end = self.today.isoformat()
        stamp = now_iso()

        def m(metric, fam, value, d, post_id=None, ckpt="snapshot", captured=None, variant=None):
            if value is None:
                return
            key = f"yt:{'post:' + post_id if post_id else 'acct:' + ch}:{d}:{metric}"
            rows.append({"account_id": aid, "variant_id": variant, "platform_post_id": post_id, "platform": "youtube",
                         "metric": metric, "canonical_metric": fam, "value": float(value), "checkpoint": ckpt,
                         "period_start": d, "period_end": d, "source": "platform_api", "captured_at": captured or stamp,
                         "metric_key": key})

        # 1) channel snapshot + uploads playlist
        c = self._get(f"{YT}/channels", {"part": "statistics,contentDetails", "id": ch}, token).get("items") or []
        if not c:
            raise CollectorError(f"channel {ch} not visible to this login (reconnect with the brand account)")
        st = c[0].get("statistics", {})
        if not st.get("hiddenSubscriberCount"):
            m("subscriberCount", "followers", st.get("subscriberCount"), end)
        m("viewCount", "views_total", st.get("viewCount"), end)
        m("videoCount", "posts_total", st.get("videoCount"), end)
        uploads = c[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")

        # 2) daily channel series
        for r in self._report(token, {"startDate": start, "endDate": end, "dimensions": "day", "sort": "day",
                                      "metrics": ",".join(DAILY_METRICS)}):
            for k, fam in DAILY_METRICS.items():
                m(k, fam, r.get(k), r["day"])
        # 3) subscriber vs non-subscriber views over the window
        split = {r["subscribedStatus"]: r.get("views") for r in self._report(
            token, {"startDate": start, "endDate": end, "dimensions": "subscribedStatus", "metrics": "views"})}
        m("views_unsubscribed", "non_follower_views", split.get("UNSUBSCRIBED"), end)
        m("views_subscribed", "follower_views", split.get("SUBSCRIBED"), end)

        # 4) recent uploads → Creative Intelligence DB
        vids: list[str] = []
        token_page = None
        while uploads:
            params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if token_page:
                params["pageToken"] = token_page
            pl = self._get(f"{YT}/playlistItems", params, token)
            items = pl.get("items") or []
            fresh = [i["contentDetails"]["videoId"] for i in items
                     if (i["contentDetails"].get("videoPublishedAt") or end) >= start]
            vids += fresh
            token_page = pl.get("nextPageToken")
            if not token_page or len(fresh) < len(items) or len(vids) >= 200:
                break
        details = {}
        for i in range(0, len(vids), 50):
            for v in self._get(f"{YT}/videos", {"part": "snippet,contentDetails,statistics",
                                                "id": ",".join(vids[i:i + 50])}, token).get("items") or []:
                details[v["id"]] = v
        from .creative_db import upsert_ingested_post
        for vid, v in details.items():
            dur = iso_duration_s(v.get("contentDetails", {}).get("duration", ""))
            sn = v.get("snippet", {})
            kind = "short" if dur is not None and dur <= SHORT_MAX_S else "video"
            upsert_ingested_post(self.db, acc, {"platform_post_id": vid, "published_at": sn.get("publishedAt"),
                                                "text": (sn.get("title") or "") + ("\n" + sn["description"] if sn.get("description") else ""),
                                                "permalink": (f"https://www.youtube.com/shorts/{vid}" if kind == "short"
                                                              else f"https://www.youtube.com/watch?v={vid}"),
                                                "post_type": kind, "media": {"duration_s": dur}}, self.tz, origin="platform_api")
        # 5) per-video lifetime analytics (+ public statistics as a fallback for very new uploads)
        pub = {vid: datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
               for vid, v in details.items() if v.get("snippet", {}).get("publishedAt")}
        now = datetime.now(timezone.utc)
        analytics: dict[str, dict] = {}
        ids = list(details)
        for i in range(0, len(ids), 200):
            part = ids[i:i + 200]
            first = min((pub[x] for x in part if x in pub), default=now).date().isoformat()
            for r in self._report(token, {"startDate": first, "endDate": end, "dimensions": "video",
                                          "filters": "video==" + ",".join(part), "metrics": ",".join(VIDEO_METRICS),
                                          "maxResults": 200, "sort": "-views"}):
                analytics[r["video"]] = r
        for vid, v in details.items():
            ck = checkpoint_for(pub[vid], now) if vid in pub else "snapshot"
            a = analytics.get(vid)
            if a:
                for k, fam in VIDEO_METRICS.items():
                    m(k, fam, a.get(k), end, post_id=vid, ckpt=ck)
            else:  # Analytics lags ~2 days: public counters only, until the report catches up
                s = v.get("statistics", {})
                m("viewCount", "views", s.get("viewCount"), end, post_id=vid, ckpt=ck)
                m("likeCount", "likes", s.get("likeCount"), end, post_id=vid, ckpt=ck)
                m("commentCount", "comments", s.get("commentCount"), end, post_id=vid, ckpt=ck)
        n = self.db.insert_many("metrics", rows, upsert_key="metric_key")
        return {"account_id": aid, "metric_rows": n, "videos": len(details), "with_analytics": len(analytics)}


def collect_youtube(db: DB, tz: str = "America/New_York", transport=None, actor: str = "insights") -> dict:
    """Every OAuth-connected YouTube account. One failing channel doesn't stop the others."""
    from .oauth import access_token
    out = {"collected": [], "errors": {}}
    for acc in db.all("accounts", "platform='youtube' AND verification_status='oauth_connected'"):
        ref = acc.get("credential_ref") or ""
        if not ref.startswith("vault:") or not acc.get("stable_platform_id"):
            out["errors"][acc["account_id"]] = "not connected through the Accounts page"
            continue
        try:
            token = access_token(ref[len("vault:"):])
            out["collected"].append(YouTubeCollector(db, transport, tz).collect(acc, token))
        except (CollectorError, LookupError, PermissionError) as e:
            out["errors"][acc["account_id"]] = str(e)
    db.audit(actor, "youtube_insights_collected", "metrics", "", out)
    return out


# ---- Instagram (Instagram API with Facebook Login; instagram_manage_insights) -------------------------

IG_WINDOW_DAYS = 28
IG_MEDIA_LOOKBACK_DAYS = 60
IG_MEDIA_MAX = 100


class InstagramCollector:
    """What the ops ingest doesn't request: the follower / non-follower split of account reach, follows vs
    unfollows, accounts engaged and link taps over the window, Reels average watch time, and follows /
    profile visits on feed posts (Instagram doesn't report those two for Reels)."""

    def __init__(self, db: DB, transport=None, now: datetime | None = None):
        from .oauth import META_VERSION
        self.db, self.transport = db, transport or urllib_transport
        self.now = now or datetime.now(timezone.utc)
        self.base = f"https://graph.facebook.com/{META_VERSION}"

    def _get(self, path: str, params: dict, token: str) -> dict:
        resp = self.transport(HttpRequest("GET", f"{self.base}/{path}?" + urllib.parse.urlencode({**params, "access_token": token}),
                                          {"Accept": "application/json"}, purpose="instagram insights"))
        if resp.status >= 300:
            err = (resp.body or {}).get("error") or {}
            raise CollectorError(f"{path.split('/')[-1]}: HTTP {resp.status} {err.get('message') or resp.body}"[:300])
        return resp.body or {}

    def _totals(self, ig: str, metric: str, token: str, since: int, until: int, breakdown: str = "") -> dict:
        params = {"metric": metric, "period": "day", "metric_type": "total_value", "since": since, "until": until}
        if breakdown:
            params["breakdown"] = breakdown
        out = {}
        for d in self._get(f"{ig}/insights", params, token).get("data") or []:
            tv = d.get("total_value") or {}
            out[d["name"]] = {"value": tv.get("value"), "split": {
                r["dimension_values"][0]: r.get("value") for b in tv.get("breakdowns") or [] for r in b.get("results") or []}}
        return out

    def collect(self, acc: dict, token: str) -> dict:
        ig, aid, rows, notes = acc["stable_platform_id"], acc["account_id"], [], []
        end = self.now.date()
        start = end - timedelta(days=IG_WINDOW_DAYS)
        since, until = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()), int(self.now.timestamp())

        def m(metric, fam, value, post_id=None, ckpt="snapshot"):
            if value is None:
                return
            d = end.isoformat()
            key = f"ig:{'post:' + post_id if post_id else 'acct:' + ig}:{d}:{metric}"
            rows.append({"account_id": aid, "variant_id": None, "platform_post_id": post_id, "platform": "instagram",
                         "metric": metric, "canonical_metric": fam, "value": float(value), "checkpoint": ckpt,
                         "period_start": start.isoformat() if not post_id else d, "period_end": d,
                         "source": "platform_api", "captured_at": now_iso(), "metric_key": key})

        reach = self._totals(ig, "reach", token, since, until, "follow_type").get("reach", {})
        m("reach_follower", "follower_reach", reach.get("split", {}).get("FOLLOWER"))
        m("reach_non_follower", "non_follower_reach", reach.get("split", {}).get("NON_FOLLOWER"))
        m("reach_window_total", "reach_window", reach.get("value"))
        try:  # not returned below 100 followers
            fu = self._totals(ig, "follows_and_unfollows", token, since, until, "follow_type").get("follows_and_unfollows", {})
            m("follows_window", "follows_window", fu.get("split", {}).get("FOLLOWER"))
            m("unfollows_window", "unfollows_window", fu.get("split", {}).get("NON_FOLLOWER"))
        except CollectorError as e:
            notes.append(f"follows_and_unfollows: {e}")
        try:
            other = self._totals(ig, "accounts_engaged,profile_links_taps", token, since, until)
            m("accounts_engaged", "accounts_engaged", (other.get("accounts_engaged") or {}).get("value"))
            m("profile_links_taps", "clicks_window", (other.get("profile_links_taps") or {}).get("value"))
        except CollectorError as e:
            notes.append(f"accounts_engaged/profile_links_taps: {e}")

        # per-post extras for posts we already know (ops ingest / Creative DB), newest first
        cutoff = (self.now - timedelta(days=IG_MEDIA_LOOKBACK_DAYS)).replace(microsecond=0).isoformat()
        posts = self.db.query("SELECT platform_post_id, post_type, published_at FROM posts WHERE account_id=? AND published_at >= ? "
                              "ORDER BY published_at DESC", [aid, cutoff])[:IG_MEDIA_MAX]
        done = 0
        for p in posts:
            kind = (p["post_type"] or "").lower()
            reel = kind in ("reels", "reel", "clips")
            # carousels: saves and shares are what the post-type tests compare (per 1,000 views) against the Reels
            metrics = "ig_reels_avg_watch_time" if reel else (
                "follows,profile_visits,saved,shares,views" if "carousel" in kind else "follows,profile_visits")
            try:
                data = self._get(f"{p['platform_post_id']}/insights", {"metric": metrics}, token).get("data") or []
            except CollectorError as e:
                notes.append(f"{p['platform_post_id']}: {e}")
                continue
            pub = datetime.fromisoformat(str(p["published_at"]).replace("Z", "+00:00"))
            ck = checkpoint_for(pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc), self.now)
            for d in data:
                v = (d.get("values") or [{}])[0].get("value")
                if d["name"] == "ig_reels_avg_watch_time" and v is not None:
                    m("ig_reels_avg_watch_time", "avg_watch_time_s", float(v) / 1000, p["platform_post_id"], ck)  # ms → s
                elif d["name"] == "follows":
                    m("follows", "follows", v, p["platform_post_id"], ck)
                elif d["name"] == "profile_visits":
                    m("profile_visits", "profile_visits", v, p["platform_post_id"], ck)
                elif d["name"] in ("saved", "shares", "views"):
                    m(d["name"], {"saved": "saves"}.get(d["name"], d["name"]), v, p["platform_post_id"], ck)
            done += 1
        n = self.db.insert_many("metrics", rows, upsert_key="metric_key")
        return {"account_id": aid, "metric_rows": n, "posts_checked": done, "notes": notes[:5]}


def collect_instagram(db: DB, transport=None, actor: str = "insights") -> dict:
    from .oauth import access_token
    out = {"collected": [], "errors": {}}
    for acc in db.all("accounts", "platform='instagram' AND verification_status='oauth_connected'"):
        ref = acc.get("credential_ref") or ""
        if not ref.startswith("vault:") or not acc.get("stable_platform_id"):
            out["errors"][acc["account_id"]] = "not connected through the Accounts page"
            continue
        try:
            out["collected"].append(InstagramCollector(db, transport).collect(acc, access_token(ref[len("vault:"):])))
        except (CollectorError, LookupError, PermissionError) as e:
            out["errors"][acc["account_id"]] = str(e)
    db.audit(actor, "instagram_insights_collected", "metrics", "", out)
    return out


FOLLOWER_FIELDS = {"instagram": "followers_count", "facebook": "followers_count,fan_count"}


def snapshot_followers(db: DB, transport=None, today: date | None = None, actor: str = "insights") -> dict:
    """Today's follower count for every connected priority account (worked + mirrored in config/focus.json).

    Platforms keep no follower history, so a day missed is a day lost: this runs once a day from the worker tick.
    YouTube subscriber counts come with collect_youtube once a channel is connected."""
    from .accounts import focus
    from .oauth import META_VERSION, access_token
    today = today or datetime.now(timezone.utc).date()
    f = focus()
    wanted = set(f.get("worked") or []) | set(f.get("mirrored") or []) | set(f.get("bridge") or [])
    send = transport or urllib_transport
    out = {"day": today.isoformat(), "saved": {}, "errors": {}}
    rows = []
    for acc in db.all("accounts", "verification_status='oauth_connected'"):
        if (wanted and acc["account_id"] not in wanted) or acc["platform"] not in FOLLOWER_FIELDS:
            continue
        ref = acc.get("credential_ref") or ""
        if not ref.startswith("vault:") or not acc.get("stable_platform_id"):
            out["errors"][acc["account_id"]] = "not connected through the Accounts page"
            continue
        try:
            token = access_token(ref[len("vault:"):])
            q = urllib.parse.urlencode({"fields": FOLLOWER_FIELDS[acc["platform"]], "access_token": token})
            resp = send(HttpRequest("GET", f"https://graph.facebook.com/{META_VERSION}/{acc['stable_platform_id']}?{q}",
                                    {"Accept": "application/json"}, purpose="follower snapshot"))
            if resp.status >= 300:
                raise CollectorError(f"HTTP {resp.status} {((resp.body or {}).get('error') or {}).get('message') or resp.body}"[:200])
            body = resp.body or {}
            n = body.get("followers_count", body.get("fan_count"))
            if n is None:
                raise CollectorError("no follower count returned")
        except (CollectorError, LookupError, PermissionError, OSError) as e:
            out["errors"][acc["account_id"]] = str(e)[:200]
            continue
        d = today.isoformat()
        rows.append({"account_id": acc["account_id"], "variant_id": None, "platform_post_id": None, "platform": acc["platform"],
                     "metric": "followers_count", "canonical_metric": "followers", "value": float(n), "checkpoint": "snapshot",
                     "period_start": d, "period_end": d, "source": "platform_api", "captured_at": now_iso(),
                     "metric_key": f"{acc['platform']}:acct:{acc['stable_platform_id']}:{d}:followers"})
        out["saved"][acc["account_id"]] = int(n)
    if rows:
        db.insert_many("metrics", rows, upsert_key="metric_key")
    db.audit(actor, "followers_snapshot", "metrics", "", out)
    return out


def snapshot_followers_due(db: DB, actor: str = "worker") -> dict | str:
    """Once a day (UTC): the worker tick calls this every minute; it only does work on the first call of the day."""
    day = datetime.now(timezone.utc).date().isoformat()
    if db.kv_get("followers:last_day") == day:
        return "done today"
    db.kv_set("followers:last_day", day, actor)  # one attempt a day; failures are in the audit log and on the Accounts page
    r = snapshot_followers(db, actor=actor)
    return {"saved": len(r["saved"]), "errors": len(r["errors"])}
