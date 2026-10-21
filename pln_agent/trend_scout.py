"""Trend Scout: what formats, hooks and topics are rising right now, which of them fit each brand, and a one-click
way to put one on a real clip.

Signals (config/trend_sources.json) — each source fails soft and reports its status:
* Reddit — public daily top posts in sport / meme communities (titles carry the formats people are using)
* Google Trends — the public daily trending-searches feed (topics worth riding for 1–3 days)
* YouTube — most-popular Sports chart (only when PLN_YOUTUBE_API_KEY is set)
* Competitors — the standout hooks Competitive Intelligence already captured
* Manual — lines pasted from TikTok Creative Center (no API exists for it)

Signals are matched against the format library (config/trend_formats.json) and topic keywords. A trend card says what
the trend is, how fast it is rising (this window vs the previous one), how long it is likely worth using, how well it
fits each brand, a ready hook per brand, and the risks. Trending commercial music is never suggested — brand accounts
may only use licensed / commercial-library audio — and topics touching tragedy, politics or crime are blocked.

Applying a card puts its hook and overlay recipe on the brand's best unused clip and queues the render; the post then
waits for approval like any other. Nothing posts from here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .config import CONFIG_DIR
from .db import DB, now_iso
from .overlays import _pick

UA = "PLN-Growth-Agent/1.0 (trend research; contact by@proleaguenetwork.com)"
AUDIO_NOTE = "Use licensed or platform commercial-library audio only — no trending commercial songs on brand accounts."


def formats() -> dict:
    return json.loads((CONFIG_DIR / "trend_formats.json").read_text())


def sources() -> dict:
    return json.loads((CONFIG_DIR / "trend_sources.json").read_text())


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/rss+xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---- collectors: each returns [{source, text, url, score}] ----------------------------------------

def from_reddit(fetch=_get) -> list[dict]:
    out = []
    for sub in sources().get("reddit") or []:
        body = json.loads(fetch(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=25"))
        for c in (body.get("data") or {}).get("children") or []:
            d = c.get("data") or {}
            if d.get("over_18"):
                continue
            out.append({"source": f"reddit/r/{sub}", "text": d.get("title", ""), "url": "https://www.reddit.com" + d.get("permalink", ""),
                        "score": float(d.get("ups") or 0)})
    return out


def from_google_trends(fetch=_get) -> list[dict]:
    geo = sources().get("google_trends_geo", "US")
    root = ET.fromstring(fetch(f"https://trends.google.com/trending/rss?geo={geo}"))
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        traffic = next((el.text for el in item if el.tag.endswith("approx_traffic")), "") or ""
        n = float(re.sub(r"[^\d]", "", traffic) or 0)
        out.append({"source": "google_trends", "text": title, "url": item.findtext("link") or "", "score": n})
    return out


def from_youtube(fetch=_get) -> list[dict]:
    key = os.environ.get("PLN_YOUTUBE_API_KEY")
    if not key:
        raise LookupError("PLN_YOUTUBE_API_KEY not set")
    cat = sources().get("youtube_category", "17")
    body = json.loads(fetch("https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&chart=mostPopular"
                            f"&regionCode=US&videoCategoryId={cat}&maxResults=50&key={key}"))
    return [{"source": "youtube/popular", "text": i["snippet"]["title"], "url": f"https://youtu.be/{i['id']}",
             "score": float((i.get("statistics") or {}).get("viewCount") or 0)} for i in body.get("items") or []]


def from_competitors(db: DB) -> list[dict]:
    out, seen = [], set()
    for s in db.all("competitor_snapshots", order="captured_at DESC")[:200]:
        if s["handle"] in seen:
            continue
        seen.add(s["handle"])
        for st in ((s.get("profile") or {}).get("standouts") or [])[:8]:
            if st.get("hook"):
                out.append({"source": f"competitor/{s['handle']}", "text": st["hook"], "url": st.get("permalink", ""),
                            "score": float(st.get("er") or 0) * 1000})
    return out


COLLECTORS = {"reddit": from_reddit, "google_trends": from_google_trends, "youtube": from_youtube}


def collect(db: DB, fetch=_get, actor: str = "trend-scout") -> dict:
    """Pull every source into trend_signals (deduped by text+source per day). Returns per-source status."""
    status, rows = {}, []
    for name, fn in COLLECTORS.items():
        try:
            got = fn(fetch)
            rows += got
            status[name] = {"ok": True, "signals": len(got)}
        except LookupError as e:
            status[name] = {"ok": False, "why": str(e)}
        except Exception as e:  # noqa: BLE001 — one source down doesn't stop the scan
            status[name] = {"ok": False, "why": str(e)[:160]}
    try:
        got = from_competitors(db)
        rows += got
        status["competitors"] = {"ok": True, "signals": len(got)}
    except Exception as e:  # noqa: BLE001
        status["competitors"] = {"ok": False, "why": str(e)[:160]}
    day = datetime.now(timezone.utc).date().isoformat()
    recs = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        sid = hashlib.sha1(f"{r['source']}|{text.lower()}|{day}".encode()).hexdigest()[:16]
        recs.append({"signal_id": sid, "source": r["source"], "text": text[:300], "url": r.get("url", "")[:500],
                     "score": r.get("score") or 0, "seen_at": now_iso()})
    if recs:
        db.insert_many("trend_signals", recs, upsert_key="signal_id")
    db.kv_set("trend_scout:sources", json.dumps(status), actor)
    return status


def add_manual(db: DB, lines: str, actor: str) -> int:
    """Lines pasted from TikTok Creative Center (trending hashtags, sounds, creators' hooks)."""
    n = 0
    for ln in (lines or "").splitlines():
        t = ln.strip().lstrip("#•-").strip()
        if len(t) < 3:
            continue
        sid = hashlib.sha1(f"manual|{t.lower()}|{datetime.now(timezone.utc).date()}".encode()).hexdigest()[:16]
        db.insert("trend_signals", {"signal_id": sid, "source": f"manual/{actor}", "text": t[:300], "url": "", "score": 1000,
                                    "seen_at": now_iso()}, upsert_key="signal_id")
        n += 1
    db.audit(actor, "trend_signals_added", "trend_signals", "", {"n": n})
    return n


# ---- turning signals into cards ------------------------------------------------------------------

def _blocked(text: str) -> str:
    t = text.lower()
    return next((w for w in formats().get("blocked_topics") or [] if re.search(rf"\b{re.escape(w)}\b", t)), "")


def _families() -> dict:
    """brand_id → word family, for the brands we post for."""
    from .overlays import style_for
    from .accounts import load_brands
    return {b["brand_id"]: style_for(b["brand_id"]).get("family", "default") for b in load_brands()}


def _signals(db: DB, since: datetime, until: datetime) -> list[dict]:
    return [s for s in db.all("trend_signals", "seen_at >= ? AND seen_at < ?", [since.isoformat(), until.isoformat()])]


def build_cards(db: DB, now: datetime | None = None, llm=None, actor: str = "trend-scout") -> list[dict]:
    """Match the window's signals to formats and topics, score velocity vs the previous window, write trend_cards."""
    now = now or datetime.now(timezone.utc)
    cfg, src = formats(), sources()
    win = timedelta(days=src.get("window_days", 7))
    cur, prev = _signals(db, now - win, now + timedelta(seconds=1)), _signals(db, now - 2 * win, now - win)
    fams = _families()
    from . import team_params
    min_sightings = int(team_params.get(db, "trends", "min_sightings", 1))
    cards = []

    def fits_for(fit: dict, hooks: dict, fmt_key: str) -> dict:
        out = {}
        for bid, fam in fams.items():
            score = fit.get(fam, fit.get("default", 0.4))
            options = hooks.get(fam) or hooks.get("default") or []
            out[bid] = {"score": round(score, 2), "hook": _pick(options, f"{fmt_key}:{bid}") if options else ""}
        return out

    # formats
    for key, f in (cfg.get("formats") or {}).items():
        def hit(s):
            t = s["text"].lower()
            return any(m in t for m in f["match"]) and not _blocked(s["text"])
        now_hits, prev_hits = [s for s in cur if hit(s)], [s for s in prev if hit(s)]
        if len(now_hits) < min_sightings:
            continue
        velocity = (len(now_hits) + 1) / (len(prev_hits) + 1)
        examples = sorted(now_hits, key=lambda s: -(s.get("score") or 0))[:5]
        cards.append({"card_id": f"format:{key}", "kind": "format", "title": f["name"],
                      "summary": f"{len(now_hits)} sighting(s) in the last {win.days} days ({'rising' if velocity > 1.2 else 'steady'}).",
                      "signals": [{k: e[k] for k in ("source", "text", "url")} for e in examples],
                      "sightings": len(now_hits), "velocity": round(velocity, 2),
                      "score": round(min(1.0, len(now_hits) / 6) * 0.6 + min(1.0, velocity / 3) * 0.4, 2),
                      "lifespan_days": src.get("lifespan_days", {}).get("format", 10),
                      "fits": fits_for(f.get("fit") or {}, f.get("hooks") or {}, key),
                      "recipe": {"overlay": f.get("overlay") or {}, "cta": f.get("cta", "")},
                      "risk": [AUDIO_NOTE]})
    # topics: trending searches / posts that touch our sports
    for fam, words in (cfg.get("topic_keywords") or {}).items():
        external = [s for s in cur if s["source"] in ("google_trends", "youtube/popular") or s["source"].startswith("reddit/")]
        hits = [s for s in external if any(re.search(rf"\b{re.escape(w)}\b", s["text"].lower()) for w in words)]
        hits = [s for s in hits if not _blocked(s["text"])]
        if not hits:
            continue
        top = sorted(hits, key=lambda s: -(s.get("score") or 0))[:5]
        brands = [b for b, f in fams.items() if f == fam]
        cards.append({"card_id": f"topic:{fam}:{now.date().isoformat()}", "kind": "topic",
                      "title": f"People are talking about {top[0]['text'][:60]}",
                      "summary": f"{len(hits)} trending item(s) touch {', '.join(words[:3])} — ride it with a timely post while it's hot.",
                      "signals": [{k: e[k] for k in ("source", "text", "url")} for e in top], "sightings": len(hits),
                      "velocity": None, "score": round(min(1.0, len(hits) / 4), 2),
                      "lifespan_days": src.get("lifespan_days", {}).get("topic", 3),
                      "fits": {b: {"score": 0.8, "hook": ""} for b in brands}, "recipe": {"overlay": {}},
                      "risk": [AUDIO_NOTE, "Topical: check the context before posting — never imply an endorsement or comment on people."]})
    if llm is not None and cards:
        cards = refine_with_llm(cards, llm)
    out = []
    for c in cards:
        old = db.get("trend_cards", "card_id", c["card_id"]) or {}
        rec = {**c, "status": old.get("status") if old.get("status") in ("dismissed", "applied") else "active",
               "applied": old.get("applied") or [], "first_seen": old.get("first_seen") or now_iso(), "last_seen": now_iso(),
               "expires_at": (now + timedelta(days=c["lifespan_days"])).replace(microsecond=0).isoformat(), "updated_at": now_iso()}
        db.insert("trend_cards", rec, upsert_key="card_id")
        out.append(rec)
    # cards nobody has seen again for their lifespan expire
    for c in db.all("trend_cards", "status='active' AND expires_at < ?", [now.isoformat()]):
        db.update("trend_cards", "card_id", c["card_id"], {"status": "expired", "updated_at": now_iso()})
    db.audit(actor, "trend_cards_built", "trend_cards", "", {"cards": len(out)})
    return out


TREND_PROMPT = """You are the Trend Scout for Pro League Network (PLN), which runs unusual competitive sports brands: CarJitsu
(jiu-jitsu in a car), SlapFIGHT, STR33T, Putt Tour (pro mini golf), Pro Carrom Tour. Below are trend cards detected from
real signals, each with example signals and a draft hook per brand.

For each card, improve the brand hooks so they would stop a scroll on Instagram Reels: under 7 words, ALL CAPS, no athlete
or brand names, no claims about people, no slurs, nothing about tragedy, politics or crime. Keep the format recognisable.
If a card is a bad fit for a brand, set its score lower (0-1). Add one-line "why_now" per card.
Reply with JSON only: {"cards": [{"card_id": "...", "why_now": "...", "fits": {"<brand_id>": {"score": 0.0, "hook": "..."}}}]}

CARDS:
"""


def refine_with_llm(cards: list[dict], llm) -> list[dict]:
    """Optional: sharper hooks and fit scores from Claude. Any failure keeps the rule-based cards."""
    try:
        from .team import _json
        brief = [{"card_id": c["card_id"], "title": c["title"], "examples": [s["text"] for s in c["signals"][:4]],
                  "fits": c["fits"]} for c in cards]
        raw = llm("You write short-form sports social copy. JSON only.", [{"role": "user", "content": TREND_PROMPT + json.dumps(brief)}], 2500)
        got = {x["card_id"]: x for x in (_json(raw).get("cards") or [])}
    except Exception:  # noqa: BLE001
        return cards
    for c in cards:
        g = got.get(c["card_id"])
        if not g:
            continue
        c["why_now"] = (g.get("why_now") or "")[:200]
        for bid, f in (g.get("fits") or {}).items():
            if bid in c["fits"] and isinstance(f, dict):
                hook = (f.get("hook") or "").upper()[:60]
                if hook and not _blocked(hook):
                    from .overlays import is_generic
                    if not is_generic(hook):
                        c["fits"][bid]["hook"] = hook
                if isinstance(f.get("score"), (int, float)):
                    c["fits"][bid]["score"] = round(max(0.0, min(1.0, float(f["score"]))), 2)
    return cards


def refresh(db: DB, fetch=_get, use_llm: bool = True, actor: str = "trend-scout") -> dict:
    status = collect(db, fetch, actor)
    llm = None
    if use_llm:
        from . import team
        llm = team._llm
    cards = build_cards(db, llm=llm, actor=actor)
    return {"sources": {k: v.get("signals", v.get("why")) for k, v in status.items()}, "cards": len(cards)}


def active(db: DB) -> list[dict]:
    cards = db.all("trend_cards", "status IN ('active','applied')")
    return sorted(cards, key=lambda c: -(c.get("score") or 0))


def dismiss(db: DB, card_id: str, actor: str, why: str = "") -> None:
    db.update("trend_cards", "card_id", card_id, {"status": "dismissed", "updated_at": now_iso()})
    db.audit(actor, "trend_dismissed", "trend_cards", card_id, {"why": why})


# ---- applying a card to a real clip ---------------------------------------------------------------

def apply(db: DB, card_id: str, brand_id: str, actor: str, ops: DB | None, settings, hook: str = "") -> dict:
    """Put the trend's hook + overlay recipe on the brand's best unused clip (Instagram draft) and queue its render.
    The post then waits for approval like any other."""
    from .ops_sources import clip_asset_id
    from .scout import promote, top_moments
    card = db.get("trend_cards", "card_id", card_id)
    if not card:
        raise KeyError(card_id)
    fit = (card.get("fits") or {}).get(brand_id) or {}
    hook = (hook or fit.get("hook") or "").strip().upper()
    if not hook:
        raise ValueError("this trend has no hook for that brand — type one")
    used = {(v.get("treatment") or {}).get("trend_clip") for v in db.all("variants", "brand_id=?", [brand_id])}
    moment = next((m for m in top_moments(db, brand_id=brand_id, n=12) if m["clip_id"] not in used), None)
    if not moment:
        raise LookupError(f"no unused scored clip for {brand_id} — run the Scout first")
    aid = clip_asset_id(moment["clip_id"])
    if not db.get("assets", "asset_id", aid) or not db.query("SELECT variant_id FROM variants WHERE asset_id=?", [aid]):
        if ops is None:
            raise RuntimeError("the ops database isn't configured — can't import the clip")
        promote(db, ops, [moment["clip_id"]], settings, actor)
    acc = next((a for a in db.all("accounts", "brand_id=? AND platform='instagram'", [brand_id]) if not a.get("hard_block_reason")), None)
    v = next((x for x in db.all("variants", "asset_id=?", [aid]) if acc and x["account_id"] == acc["account_id"]), None)
    if not v:
        raise LookupError("no Instagram draft for that clip (rights or routing)")
    if v["state"] in ("scheduled", "publishing", "published", "simulated"):
        raise ValueError(f"that clip's post is already {v['state']}")
    ot = [dict(o) for o in (v.get("onscreen_text") or []) if not (o.get("position") or "").endswith("small")]
    if ot:
        ot[0]["text"] = hook
    else:
        ot = [{"start": 0.0, "end": 3.0, "text": hook, "position": "top"}]
    t = dict(v.get("treatment") or {})
    t["overlay"] = {**(t.get("overlay") or {}), **((card.get("recipe") or {}).get("overlay") or {}), "hook_custom": True}
    t["trend"], t["trend_clip"] = card_id, moment["clip_id"]
    changes = {"onscreen_text": ot, "treatment": t, "render_status": "queued", "updated_at": now_iso()}
    cta = (card.get("recipe") or {}).get("cta")
    if cta and cta not in (v.get("caption") or ""):
        changes["caption"] = ((v.get("caption") or "").rstrip() + "\n\n" + cta).strip()
        from .strategists.base import caption_hash
        changes["caption_hash"] = caption_hash(changes["caption"])
    if v["state"] == "approved":
        changes["state"] = "in_review"
    db.update("variants", "variant_id", v["variant_id"], changes)
    applied = list(card.get("applied") or []) + [{"variant_id": v["variant_id"], "brand_id": brand_id, "hook": hook, "by": actor, "at": now_iso()}]
    db.update("trend_cards", "card_id", card_id, {"applied": applied, "status": "applied", "updated_at": now_iso()})
    db.audit(actor, "trend_applied", "variants", v["variant_id"], {"card": card_id, "hook": hook, "clip": moment["clip_id"]})
    return {"variant_id": v["variant_id"], "clip": moment, "hook": hook}
