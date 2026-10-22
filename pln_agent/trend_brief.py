"""The Monday brief: a short list of what to make and what to clip this week.

Two sources the Trend Scout didn't have:

* YouTube breakouts — the watched channels' public upload feeds (config/trend_sources.json `youtube_watch`, no API
  key). A breakout is an upload from the last week with views at least `outlier_min` times the channel's own median:
  its format worked for that audience, so it is worth copying (never the footage) for the brands listed beside the
  channel. A feed lists the channel's last 15 uploads, and the median comes from those.
* Prediction-market movers — Kalshi and Polymarket public APIs, snapshotted once a day so the 7-day move can be
  measured (Polymarket reports its own; Kalshi's comes from our snapshots, 24-hour until a week of them exists).
  A mover is a market with real money in it whose price moved at least `min_move_pts` points: a PREDCT storyline to
  clip from the shows' recordings or to write a short around. Political markets are marked: held off Instagram and
  judged case by case elsewhere (the founders' decision).

The brief adds the strongest active Trend Scout format cards and is built once a week, on Monday at 12:00 UTC, by the
worker, or on demand from the Trends page. It suggests; nothing is made or posted from it.
"""
from __future__ import annotations

import json
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from .db import DB, now_iso
from .trend_scout import _blocked, _get, sources

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
POLY = "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume1wk&ascending=false&limit={n}"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&mve_filter=exclude"
KALSHI_EVENTS = "https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
BRANDS = {"pln": "Pro League Network: a network of real but unusual pro leagues (CarJitsu, Putt Tour, SlapFIGHT, STR33T)",
          "carjitsu": "CarJitsu: jiu-jitsu grappling matches fought inside a parked car (no driving)",
          "putt_tour": "Putt Tour: a professional putting tour (formerly the World Putting League)",
          "pln_picks": "PREDCT: entertainment about what prediction markets think, never betting advice"}
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "m": "http://search.yahoo.com/mrss/"}
BRIEF_HOUR_UTC = 12  # Monday 8 am Eastern

CATEGORIES = [  # first match wins
    ("politics", r"\b(president|election|senate|house|congress|governor|mayor|trump|biden|vance|harris|democrat|republican|"
                 r"prime minister|parliament|primary|nominee|impeach|cabinet|supreme court|lula|bolsonaro)\b"),
    ("geopolitics", r"\b(ceasefire|military|nato|russia|ukraine|iran|israel|gaza|china|taiwan|invade|invasion|missile|"
                    r"strike on|troops|nuclear|sanction)"),
    ("hockey", r"\b(nhl|hockey|stanley cup|maple leafs|oilers|bruins|rangers|panthers|avalanche|canadiens)\b"),
    ("soccer", r"\b(premier league|epl|champions league|la liga|serie a|bundesliga|mls|world cup|arsenal|chelsea|liverpool|"
               r"man(chester)? (city|united)|tottenham|west ham|real madrid|barcelona|fc\b|ballon d'or|uefa|europa league|"
               r"mbapp|yamal|haaland|kane)"),
    ("sports", r"\b(nfl|nba|mlb|wnba|ncaa|ufc|boxing|f1|formula 1|super bowl|world series|playoffs?|mvp|game \d|vs\.?|"
               r"golf|pga|masters|tennis|open|spread|moneyline|total points|bears|chiefs|eagles|cowboys|lakers|celtics|yankees|dodgers)\b"
               r"|\bwin on \d{4}-\d\d-\d\d"),
    ("economy", r"\b(fed|interest rates?|inflation|cpi|gdp|recession|unemployment|payrolls|jobs report|s&p|nasdaq|dow|"
                r"stock|tariff|treasury|oil|gas price)\b"),
    ("crypto", r"\b(bitcoin|btc|ethereum|eth|solana|crypto|xrp|dogecoin)\b"),
    ("weather", r"\b(temperature|hurricane|snow|rain|weather|heat|storm)\b"),
    ("culture", r"\b(oscar|grammy|emmy|album|movie|box office|taylor swift|spotify|netflix|tiktok|youtube|celebrity|"
                r"dancing with the stars|bachelor|survivor)\b"),
]
SHOWS = {"hockey": "Five Hole", "soccer": "Bottle Jobs", "sports": "MarketZone sports desk", "economy": "WALL ST EXT. / MarketZone",
         "crypto": "PNN / MarketZone", "weather": "MarketZone weather desk", "culture": "MarketZone",
         "politics": "PNN (not Instagram)", "geopolitics": "not for social", "other": "MarketZone"}
BETTING_LINE = re.compile(r"^(spread|total|o/u)\b|\bover/under\b|\b(o|u) \d+(\.5)?\b|\(([+-])\d+(\.5)?\)", re.I)
SENSITIVE = {"politics", "geopolitics"}  # marked, at most two, never on Instagram


def _cfg() -> dict:
    return sources()


POLITICAL_WORDS = {"election", "trump", "biden", "harris"}  # in the Trend Scout's blocked list; for markets they are marked instead


def _market_blocked(title: str) -> str:
    from .trend_scout import formats
    low = (title or "").lower()
    return next((w for w in formats().get("blocked_topics") or [] if w not in POLITICAL_WORDS
                 and re.search(rf"\b{re.escape(w)}\b", low)), "")


KALSHI_SERIES = [("hockey", ("nhl",)), ("soccer", ("epl", "ucl", "uel", "laliga", "seriea", "bundesliga", "mls", "wc", "soccer")),
                 ("sports", ("nfl", "nba", "mlb", "ncaa", "wnba", "ufc", "pga", "f1", "atp", "wta")),
                 ("crypto", ("btc", "eth", "sol", "xrp", "doge")),
                 ("economy", ("fed", "cpi", "gdp", "payroll", "u3", "inx", "nasdaq", "rate", "gas", "oil")),
                 ("weather", ("high", "temp", "rain", "snow", "hurricane")),
                 ("politics", ("pres", "senate", "house", "gov", "mayor", "trump", "elect"))]


def kalshi_category(series: str) -> str:
    """Kalshi's series code says what a game market is when its title doesn't ("Toronto vs Boston" is KXNHLGAME)."""
    s = re.sub(r"^kx", "", (series or "").lower())
    return next((c for c, keys in KALSHI_SERIES if any(s.startswith(k) or k in s[:8] for k in keys)), "")


def category(title: str) -> str:
    low = (title or "").lower()
    return next((c for c, pat in CATEGORIES if re.search(pat, low)), "other")


# ---- YouTube breakouts ----------------------------------------------------------------------------

def parse_feed(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        stats = e.find("m:group/m:community/m:statistics", NS)
        link = e.find("a:link", NS)
        out.append({"video_id": vid, "title": e.findtext("a:title", default="", namespaces=NS),
                    "published_at": e.findtext("a:published", default="", namespaces=NS),
                    "views": float(stats.get("views")) if stats is not None and stats.get("views") else None,
                    "url": link.get("href") if link is not None else f"https://youtu.be/{vid}"})
    return out


def collect_youtube(db: DB, fetch=_get) -> dict:
    rows, status = [], {}
    for w in _cfg().get("youtube_watch") or []:
        try:
            vids = parse_feed(fetch(YT_FEED + w["channel_id"]))
            rows += [{**v, "channel_id": w["channel_id"], "handle": w["handle"], "captured_at": now_iso()}
                     for v in vids if v["video_id"] and v["views"] is not None]
            status[w["handle"]] = len(vids)
        except Exception as e:  # noqa: BLE001 — one channel down doesn't stop the rest
            status[w["handle"]] = f"failed: {str(e)[:80]}"
    if rows:
        db.insert_many("yt_videos", rows, upsert_key="video_id")
    return status


def breakouts(db: DB, now: datetime | None = None, days: int = 7) -> list[dict]:
    """Uploads from the last `days` (at least a day old, so the count has settled a little) far above their channel's
    median. The median uses the channel's last 15 known uploads, this one included."""
    now = now or datetime.now(timezone.utc)
    lo, hi = (now - timedelta(days=days)).isoformat(), (now - timedelta(days=1)).isoformat()
    cfg = _cfg()
    min_x = float(cfg.get("outlier_min", 3.0))
    brands = {w["channel_id"]: w.get("brands") or [] for w in cfg.get("youtube_watch") or []}
    out = []
    for ch in brands:
        vids = db.all("yt_videos", "channel_id=?", [ch], order="published_at DESC", limit=15)
        views = [v["views"] for v in vids if v["views"]]
        if len(views) < 5:
            continue
        med = statistics.median(views)
        for v in vids:
            pub = str(v["published_at"] or "")
            if not (lo <= pub <= hi) or not med or _blocked(v["title"]):
                continue
            x = v["views"] / med
            if x >= min_x:
                out.append({"title": v["title"], "url": v["url"], "handle": v["handle"], "views": int(v["views"]),
                            "median": int(med), "x": round(x, 1), "published_at": pub[:10], "brands": brands[ch],
                            "short": "/shorts/" in (v["url"] or "")})
    return sorted(out, key=lambda o: -o["x"])


# ---- prediction-market movers ---------------------------------------------------------------------

def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def from_polymarket(fetch=_get) -> list[dict]:
    n = int((_cfg().get("markets") or {}).get("polymarket_limit", 200))
    rows = []
    for m in json.loads(fetch(POLY.format(n=n))):
        try:
            price = _num(json.loads(m.get("outcomePrices") or "[]")[0])
        except (ValueError, IndexError, TypeError):
            price = None
        ev = (m.get("events") or [{}])[0]
        slug = ev.get("slug") or m.get("slug") or ""
        rows.append({"source": "polymarket", "market_id": str(m.get("id") or m.get("slug")), "title": m.get("question") or "",
                     "price": price, "change_1d": _num(m.get("oneDayPriceChange")), "change_7d": _num(m.get("oneWeekPriceChange")),
                     "volume": _num(m.get("volume1wk")), "url": f"https://polymarket.com/event/{slug}" if slug else "",
                     "end_date": (m.get("endDate") or "")[:10]})
    return rows


def _kalshi_price(m: dict, key: str) -> float | None:
    v = _num(m.get(f"{key}_dollars"))
    if v is None and _num(m.get(key)) is not None:
        v = _num(m.get(key)) / 100  # older shape: cents
    return v


def _kalshi_markets(fetch) -> list[dict]:
    """Open single markets. The plain list is crowded with multi-leg combos, so they are excluded at the source; if a
    venue change ever empties it, the events list (markets nested under each event) is read instead."""
    pages = int((_cfg().get("markets") or {}).get("kalshi_pages", 3))
    out, cursor = [], ""
    for _ in range(pages):
        body = json.loads(fetch(KALSHI + (f"&cursor={cursor}" if cursor else "")))
        out += [m for m in body.get("markets") or [] if not (m.get("ticker") or "").startswith("KXMVE")]
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    if not out:
        body = json.loads(fetch(KALSHI_EVENTS))
        for ev in body.get("events") or []:
            out += [{**m, "event_ticker": m.get("event_ticker") or ev.get("event_ticker"),
                     "title": m.get("title") or ev.get("title")} for m in ev.get("markets") or []]
    return out


def from_kalshi(fetch=_get) -> list[dict]:
    rows = []
    for m in _kalshi_markets(fetch):
        t = m.get("ticker") or ""
        if t.startswith("KXMVE"):  # multi-leg combos: not a storyline
            continue
        last, prev = _kalshi_price(m, "last_price"), _kalshi_price(m, "previous_price")
        vol = _num(m.get("volume_24h_fp")) or _num(m.get("volume_24h"))
        title = m.get("title") or ""
        if m.get("yes_sub_title") and m["yes_sub_title"].lower() not in title.lower():
            title = f"{title} — {m['yes_sub_title']}"
        series = (m.get("event_ticker") or t).split("-")[0].lower()
        rows.append({"source": "kalshi", "market_id": t, "title": title, "price": last, "hint": kalshi_category(series),
                     "change_1d": (last - prev) if last is not None and prev else None, "change_7d": None,
                     "volume": (vol * last) if vol and last else None,  # contracts x price ~ dollars
                     "url": f"https://kalshi.com/markets/{series}", "end_date": (m.get("close_time") or "")[:10]})
    return rows


def snapshot_markets(db: DB, fetch=_get, today: date | None = None) -> dict:
    day = (today or datetime.now(timezone.utc).date()).isoformat()
    status, rows = {}, []
    for name, fn in (("polymarket", from_polymarket), ("kalshi", from_kalshi)):
        try:
            got = fn(fetch)
            rows += got
            status[name] = len(got)
        except Exception as e:  # noqa: BLE001
            status[name] = f"failed: {str(e)[:100]}"
    recs = [{**{k: v for k, v in r.items() if k != "hint"}, "snap_id": f"{r['source']}:{r['market_id']}:{day}", "day": day,
             "category": r.get("hint") or category(r["title"]), "captured_at": now_iso()}
            for r in rows if r["title"] and r["price"] is not None]
    if recs:
        db.insert_many("market_snapshots", recs, upsert_key="snap_id")
    return status


def movers(db: DB, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    cfg = _cfg().get("markets") or {}
    min_move, min_vol, top = float(cfg.get("min_move_pts", 8)) / 100, float(cfg.get("min_volume_usd", 25000)), int(cfg.get("top", 8))
    latest = db.one("SELECT MAX(day) AS d FROM market_snapshots")
    if not latest or not latest["d"]:
        return []
    day = latest["d"]
    week_ago = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    then = {(r["source"], r["market_id"]): r["price"] for r in
            db.all("market_snapshots", "day BETWEEN ? AND ?", [(date.fromisoformat(day) - timedelta(days=8)).isoformat(), week_ago])}
    out = []
    for r in db.all("market_snapshots", "day=?", [day]):
        move, window = r["change_7d"], "7d"
        if move is None and (r["source"], r["market_id"]) in then and then[(r["source"], r["market_id"])] is not None:
            move = r["price"] - then[(r["source"], r["market_id"])]
        if move is None:
            move, window = r["change_1d"], "24h"
        if move is None or abs(move) < min_move or (r["volume"] or 0) < min_vol or _market_blocked(r["title"]):
            continue
        if BETTING_LINE.search(r["title"] or ""):  # spreads and totals are betting lines, not storylines (PREDCT never sells a bet)
            continue
        if r["price"] is not None and (r["price"] >= 0.99 or r["price"] <= 0.01):  # already settled in all but name
            continue
        cat = r["category"] or category(r["title"])
        out.append({"title": r["title"], "source": r["source"], "url": r["url"], "price": round(r["price"] * 100),
                    "move": round(move * 100), "window": window, "volume": int(r["volume"] or 0), "category": cat,
                    "show": SHOWS.get(cat, SHOWS["other"]), "politics": cat in SENSITIVE, "end_date": r["end_date"]})
    out.sort(key=lambda o: -(abs(o["move"]) * (1 + min(o["volume"], 5e6) / 1e6)))
    seen, picked = set(), []
    for o in out:  # one line per storyline: sibling markets ("... win by 3+", "... win by 7+") collapse
        key = o["url"] or re.sub(r"[^a-z]+", " ", o["title"].lower().split(" — ")[0])[:60]  # one event = one storyline
        if key in seen:
            continue
        seen.add(key)
        picked.append(o)
    nonpol = [o for o in picked if not o["politics"]]
    return (nonpol[:top] + [o for o in picked if o["politics"]][:2])


# ---- the brief ------------------------------------------------------------------------------------

def week_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def build(db: DB, now: datetime | None = None, llm=None, actor: str = "trend-scout") -> dict:
    now = now or datetime.now(timezone.utc)
    make = breakouts(db, now)[:5]
    if llm and make:
        try:
            from .team import _json
            prompt = ("For each breakout YouTube upload, one line (under 20 words) on the format to copy for the listed brands — "
                      "the structure, not the footage or the people. Our brands: " + "; ".join(BRANDS.values())
                      + ". JSON only: {\"lines\": [\"...\"]}\n"
                      + json.dumps([{k: m[k] for k in ("title", "handle", "brands")} for m in make]))
            lines = _json(llm("You turn competitor breakouts into format ideas. JSON only.", [{"role": "user", "content": prompt}], 600)).get("lines") or []
            for m, ln in zip(make, lines):
                m["idea"] = re.sub(r"[*_#`]+", "", str(ln)).strip()[:200]
        except Exception:  # noqa: BLE001 — the brief stands without the lines
            pass
    clip = movers(db, now.date())
    formats = [{"title": c["title"], "summary": c.get("summary") or "", "card_id": c["card_id"]}
               for c in db.all("trend_cards", "status='active'", order="score DESC", limit=3)]
    items = {"make": make, "clip": clip, "formats": formats,
             "sources": json.loads(db.kv_get("trend_brief:sources") or "{}")}
    wk = week_of(now.date())
    db.insert("trend_briefs", {"week_start": wk, "created_at": now_iso(), "items": items, "status": "current"}, upsert_key="week_start")
    db.audit(actor, "trend_brief_built", "trend_briefs", wk, {"make": len(make), "clip": len(clip), "formats": len(formats)})
    return {"week_start": wk, **items}


def latest(db: DB) -> dict | None:
    rows = db.all("trend_briefs", order="week_start DESC", limit=1)
    return rows[0] if rows else None


def collect(db: DB, fetch=_get, today: date | None = None) -> dict:
    st = {"youtube": collect_youtube(db, fetch), "markets": snapshot_markets(db, fetch, today)}
    db.kv_set("trend_brief:sources", json.dumps(st), "trend-scout")
    return st


def run_due(db: DB, fetch=_get, now: datetime | None = None, llm=None) -> dict:
    """Worker: collect once a day; build the brief on Monday from 12:00 UTC (or any later day of a week that has none)."""
    import os
    if os.environ.get("PLN_TREND_BRIEF_DISABLED") == "1":  # tests: no network from the worker tick
        return {"skipped": "disabled"}
    now = now or datetime.now(timezone.utc)
    out = {}
    day = now.date().isoformat()
    if db.kv_get("trend_brief:last_collect") != day:
        out["collected"] = collect(db, fetch, now.date())
        db.kv_set("trend_brief:last_collect", day, "trend-scout")
    wk = week_of(now.date())
    if not db.get("trend_briefs", "week_start", wk) and (now.weekday() > 0 or now.hour >= BRIEF_HOUR_UTC):
        out["brief"] = build(db, now, llm)["week_start"]
    return out
