"""Viral Content Scout: rank every moment in the footage library by creative potential.

Input is the ops clip library (pln-fast-aws tagger output: timecoded clips per source master, tags, machine
caption, transcript, highlight/energy scores, detected talent), read-only. Each clip gets an explainable
0–100 `potential` for its own property and a `pln_potential` for cross-property PLN programming, from:

  category      — matches against the property's stop-the-scroll taxonomy (config/scout_taxonomy.json)
  highlight     — the ops tagger's highlight score
  energy        — the ops tagger's audio/motion energy
  personality   — a recognisable person on screen (ops people match)
  duration_fit  — 6–40s clips cut cleanly into short-form; very short/long ones need more work
  novelty       — not already used in a post, not overlapping a stronger moment from the same event
  learned       — measured lift of the matching content type in the Creative Intelligence Database
                  (neutral until PLN evidence exists — never assumed)

`top_moments(source)` answers "this three-hour competition: which 20 moments are worth making?" with
non-overlapping, category-diverse picks. `promote` turns chosen moments into agent assets (and drafts).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from .config import CONFIG_DIR
from .db import DB, now_iso

SCAN_BATCH = 1500


@lru_cache(maxsize=1)
def taxonomy() -> dict:
    return json.loads((CONFIG_DIR / "scout_taxonomy.json").read_text())


def _cats(brand_id: str) -> list[dict]:
    t = taxonomy()["brands"]
    return t.get(brand_id) or t["_default"]


def match_categories(brand_id: str, text: str) -> list[dict]:
    out = []
    for c in _cats(brand_id):
        m = re.search(c["pattern"], text, re.I)
        if m:
            out.append({"key": c["key"], "label": c["label"], "weight": c["weight"], "content_type": c["content_type"],
                        "matched": m.group(0)})
    return sorted(out, key=lambda c: -c["weight"])


def learned_lift(db: DB, brand_id: str, content_type: str) -> tuple[float, str]:
    """From current PLN evidence on content_type (shares per 1k views preferred)."""
    rows = db.all("knowledge", "level='pln_evidence' AND status='current' AND topic='content_type' AND brand_id=?", [brand_id])
    for outcome in ("shares_per_1k_views", "engagements_per_1k_views", "saves_per_1k_views", "comments_per_1k_views"):
        for e in rows:
            ev = e.get("evidence") or {}
            if ev.get("outcome") == outcome and ev.get("value") == content_type and ev.get("lift") is not None:
                lift = max(-0.5, min(1.0, ev["lift"]))
                return 0.5 + lift / 2, f"{content_type}: {ev['lift']:+.0%} {outcome} ({ev['evidence']}, n={ev['n']}) [{e['entry_id']}]"
    return 0.5, "no PLN evidence for this content type yet (neutral)"


def _duration_fit(d: float) -> float:
    if d < 3:
        return 0.2
    if d < 6:
        return 0.6
    if d <= 40:
        return 1.0
    if d <= 90:
        return 0.7
    return 0.4


def score_clip(db: DB, clip: dict, brand_id: str, tags: list[str], people: list[str], used: bool,
               lift_cache: dict | None = None) -> dict:
    w = taxonomy()["weights"]
    text = " ".join([" ".join(tags), clip.get("caption") or "", clip.get("transcript") or ""])
    lift_cache = lift_cache if lift_cache is not None else {}

    def one(bid: str) -> tuple[float, dict]:
        cats = match_categories(bid, text)
        cat = min(1.0, (cats[0]["weight"] + 0.1 * (len({c["key"] for c in cats}) - 1)) if cats else 0.0)
        ctype = cats[0]["content_type"] if cats else "highlight"
        if (bid, ctype) not in lift_cache:
            lift_cache[(bid, ctype)] = learned_lift(db, bid, ctype)
        learned, lwhy = lift_cache[(bid, ctype)]
        dur = float(clip["end_sec"]) - float(clip["start_sec"])
        parts = {"category": (cat, ", ".join(f"{c['label']} ('{c['matched']}')" for c in cats) or "no taxonomy match"),
                 "highlight": (min(1.0, float(clip.get("highlight_score") or 0) / 100), f"ops highlight {clip.get('highlight_score')}"),
                 "energy": (min(1.0, max(0.0, float(clip.get("energy_score") or 0))), f"ops energy {clip.get('energy_score')}"),
                 "personality": (0.9 if people else 0.3, ", ".join(people[:3]) + " on screen (unverified)" if people else "no identified person"),
                 "duration_fit": (_duration_fit(dur), f"{dur:.0f}s clip"),
                 "novelty": (0.2 if used else 1.0, "already used in a post" if used else "not yet used"),
                 "learned": (learned, lwhy)}
        total = round(100 * sum(w[k] * v[0] for k, v in parts.items()), 1)
        return total, {"parts": {k: {"score": round(v[0], 3), "why": v[1], "weight": w[k]} for k, v in parts.items()},
                       "categories": cats, "content_type": ctype}

    own, detail = one(brand_id)
    pln, pdetail = one("pln") if brand_id != "pln" else (own, detail)
    return {"potential": own, "pln_potential": pln, "detail": detail, "pln_detail": {"categories": pdetail["categories"]}}


def scan(db: DB, ops: DB, limit: int = SCAN_BATCH, actor: str = "scout") -> dict:
    """Score clips not yet scored by the current model version (oldest first), in batches."""
    from .ops_sources import _brand, canonical_brand_id, merge_people, _ops_people_index
    ver = taxonomy()["model_version"]
    done = {r["clip_id"] for r in db.query("SELECT clip_id FROM scout_moments WHERE model_version=?", [ver])}
    # ids first (small), then full rows only for this batch: transcripts are large
    all_ids = [r["id"] for r in ops.query("SELECT id FROM clips WHERE superseded_by IS NULL AND status='tagged' "
                                          "ORDER BY highlight_score DESC, id")]
    pending = [i for i in all_ids if i not in done]
    ids = pending[:limit]
    if not ids:
        return {"scored": 0, "remaining": 0}
    todo = []
    for i in range(0, len(ids), 200):
        part = ids[i:i + 200]
        todo += ops.query("SELECT id, source_id, property, start_sec, end_sec, peak_offset_sec, highlight_score, "
                          "energy_score, caption, transcript, thumb_key FROM clips WHERE id IN (" + ",".join("?" for _ in part) + ")", part)
    tags: dict[str, list[str]] = {}
    talent: dict[str, list[dict]] = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        marks = ",".join("?" for _ in part)
        for t in ops.query(f"SELECT clip_id, tag FROM clip_tags WHERE clip_id IN ({marks})", part):
            tags.setdefault(t["clip_id"], []).append(t["tag"])
        for t in ops.query(f"SELECT ct.clip_id, coalesce(p.name, ct.name_raw) AS name, ct.source FROM clip_talent ct "
                           f"LEFT JOIN people p ON p.id = ct.person_id WHERE ct.clip_id IN ({marks})", part):
            talent.setdefault(t["clip_id"], []).append({"name": t["name"], "verified": t["source"] == "manual",
                                                        "detected_by": t["source"]})
    index = _ops_people_index(ops)
    used = {r["asset_id"] for r in db.query("SELECT DISTINCT asset_id FROM posts WHERE asset_id <> ''")}
    from .ops_sources import clip_asset_id
    lift_cache: dict = {}
    out = []
    for c in todo:
        brand = _brand(c["property"] or "")
        if not brand:
            continue
        bid = canonical_brand_id(brand)
        people = [p["name"] for p in merge_people(talent.get(c["id"], []), index)]
        s = score_clip(db, c, bid, tags.get(c["id"], []), people, clip_asset_id(c["id"]) in used, lift_cache)
        out.append({"clip_id": c["id"], "source_id": c["source_id"], "brand_id": bid, "start_sec": float(c["start_sec"]),
                    "end_sec": float(c["end_sec"]), "potential": s["potential"], "pln_potential": s["pln_potential"],
                    "categories": [x["key"] for x in s["detail"]["categories"]], "content_type": s["detail"]["content_type"],
                    "detail": {**s["detail"], "thumb_key": c.get("thumb_key")}, "caption": (c["caption"] or "")[:300],
                    "model_version": ver,
                    "status": "candidate", "scored_at": now_iso()})
    existing = {r["clip_id"]: r["status"] for r in db.query("SELECT clip_id, status FROM scout_moments")}
    for r in out:  # keep human decisions (promoted / dismissed) across re-scores
        if existing.get(r["clip_id"]) in ("promoted", "dismissed"):
            r["status"] = existing[r["clip_id"]]
    db.insert_many("scout_moments", out, upsert_key="clip_id")
    remaining = len(pending) - len(ids)
    db.audit(actor, "scout_scanned", "scout_moments", "", {"scored": len(out), "remaining": remaining, "model": ver})
    return {"scored": len(out), "remaining": remaining}


def _overlaps(a: dict, b: dict) -> bool:
    return a["source_id"] == b["source_id"] and a["start_sec"] < b["end_sec"] and b["start_sec"] < a["end_sec"]


def top_moments(db: DB, source_id: str = "", brand_id: str = "", n: int = 20, field: str = "potential",
                max_per_category: int = 4, include_used: bool = False) -> list[dict]:
    """Best non-overlapping moments, at most `max_per_category` per primary category (diversity)."""
    where, params = ["status <> 'dismissed'"], []
    if source_id:
        where.append("source_id=?"), params.append(source_id)
    if brand_id:
        where.append("brand_id=?"), params.append(brand_id)
    rows = db.all("scout_moments", " AND ".join(where), params, order=f"{field} DESC")
    picked: list[dict] = []
    per_cat: dict[str, int] = {}
    for r in rows:
        if not include_used and r["status"] == "promoted":
            continue
        if any(_overlaps(r, p) for p in picked):
            continue
        cat = (r.get("categories") or ["uncategorised"])[0]
        if per_cat.get(cat, 0) >= max_per_category:
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        picked.append(r)
        if len(picked) >= n:
            break
    return picked


def sources(db: DB, brand_id: str = "") -> list[dict]:
    q = ("SELECT source_id, brand_id, COUNT(*) AS moments, MAX(potential) AS best, AVG(potential) AS avg "
         "FROM scout_moments" + (" WHERE brand_id=?" if brand_id else "") + " GROUP BY source_id, brand_id ORDER BY best DESC")
    return db.query(q, [brand_id] if brand_id else [])


def promote(db: DB, ops: DB, clip_ids: list[str], settings, actor: str) -> dict:
    """Make assets (and draft packs) from chosen moments. Rights/identity gates still apply downstream."""
    from .editorial import generate_pack
    from .ops_sources import import_ops_clips
    r = import_ops_clips(db, ops, settings.ops_assets_bucket, clip_ids=clip_ids, actor=actor)
    packs = []
    for aid in r.get("asset_ids", []):
        try:
            packs.append(generate_pack(db, aid, settings, actor=actor)["pack_id"])
        except Exception as e:  # noqa: BLE001 — report per asset
            packs.append(f"{aid}: {e}")
    for cid in clip_ids:
        db.update("scout_moments", "clip_id", cid, {"status": "promoted"})
    db.audit(actor, "scout_promoted", "scout_moments", "", {"clips": clip_ids, "assets": r.get("asset_ids"), "packs": packs})
    return {"assets": r.get("asset_ids", []), "packs": packs, "skipped": r.get("skipped", [])}


def dismiss(db: DB, clip_id: str, actor: str, reason: str = "") -> None:
    db.update("scout_moments", "clip_id", clip_id, {"status": "dismissed"})
    db.audit(actor, "scout_dismissed", "scout_moments", clip_id, {"reason": reason})
