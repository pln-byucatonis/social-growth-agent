"""Read-only connectors to the ops portal (ops.proleaguenetwork.com) data in Aurora `plnops`.

* `import_ops_accounts` merges the portal's `social_account` registry (accounts discovered
  through Meta/YouTube OAuth) into the agent's inventory. A registry match is evidence of
  access, so it upgrades verification to `ownership_confirmed` and fills the stable platform
  id. It never authorizes publishing.
* `import_ops_clips` turns the clip library (timecoded, scored, transcribed, talent-tagged
  moments cut from licensed masters) into agent assets. The library carries NO rights
  metadata, so imported assets start rights-blocked until a human fills `asset_rights`.

Nothing here writes to `plnops`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from .accounts import _slug, brand_by_name, canonical_brand_id, load_brands, normalize_username
from .db import DB, now_iso

OPS_PLATFORMS = {"instagram", "facebook", "tiktok", "youtube", "x", "twitch", "linkedin"}


def _brand(name: str) -> dict | None:
    return brand_by_name().get((name or "").strip().lower())


def _norm_handle(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_username(h))


def import_ops_accounts(db: DB, ops: DB, actor: str = "ops-sync") -> dict:
    rows = ops.query("SELECT id, brand, platform, platform_account_id, handle, display_name, status "
                     "FROM social_account ORDER BY id")
    existing = db.all("accounts")
    by_key = {(a["platform"], _norm_handle(a["username_original"])): a for a in existing if a["username_original"]}
    matched, added, skipped = [], [], []
    for r in rows:
        platform = (r["platform"] or "").lower()
        brand = _brand(r["brand"])
        if platform not in OPS_PLATFORMS or not brand:
            skipped.append(f"ops#{r['id']} {r['brand']}/{platform}: unknown brand or platform")
            continue
        acc = by_key.get((platform, _norm_handle(r["handle"])))
        if acc:
            upd = {"stable_platform_id": r["platform_account_id"], "ops_account_id": r["id"], "updated_at": now_iso()}
            if acc["verification_status"] == "unverified" and r["status"] == "active":
                upd.update({"verification_status": "ownership_confirmed",
                            "ownership_owner": "ops registry (platform OAuth discovery)"})
            db.update("accounts", "account_id", acc["account_id"], upd)
            matched.append(acc["account_id"])
            continue
        acc_id = f"{brand['brand_id']}.{platform}.{_slug(normalize_username(r['handle']))}"
        if db.get("accounts", "account_id", acc_id):
            acc_id = f"{acc_id}_ops{r['id']}"
        db.insert("accounts", {
            "account_id": acc_id, "brand_id": brand["brand_id"], "canonical_brand_id": canonical_brand_id(brand),
            "source_brand_name": brand["name"], "platform": platform, "username_original": r["handle"],
            "username_normalized": normalize_username(r["handle"]), "stable_platform_id": r["platform_account_id"],
            "priority": brand.get("priority", ""),
            "role": "legacy_bridge" if brand.get("is_legacy") else "canonical",
            "supplied_status": f"ops_registry:{r['status']}",
            "verification_status": "ownership_confirmed" if r["status"] == "active" else "unverified",
            "ownership_owner": "ops registry (platform OAuth discovery)" if r["status"] == "active" else "",
            "credential_ref": "", "publishing_authorized": False, "publishing_policy": "approval_required",
            "hard_block_reason": "", "redirect_to_account_id": "", "campaign": "",
            "notes": "Discovered in the ops portal registry; not in the user-supplied CSV.",
            "source": "ops_registry", "csv_row": None, "ops_account_id": r["id"],
            "created_at": now_iso(), "updated_at": now_iso()}, upsert_key="account_id")
        added.append(acc_id)
    db.audit(actor, "ops_accounts_imported", "accounts", "", {"matched": matched, "added": added, "skipped": skipped})
    return {"matched": matched, "added": added, "skipped": skipped}


def clip_asset_id(clip_id: str) -> str:
    return "clip-" + hashlib.sha1(clip_id.encode()).hexdigest()[:12]


def _label(tags: list[str]) -> str:
    return tags[0].replace("_", " ").capitalize() if tags else "Big moment"


INJURY_TAGS = {"knockout", "ko", "tko", "injury", "blood", "down_hard"}
# Neutral, name-free descriptions of the action, used instead of the machine caption when the clip's
# athletes are unverified (machine captions routinely contain fighter names and nicknames).
# Several phrasings per tag, picked per clip, so a feed of submissions doesn't read like one post repeated.
TAG_SENTENCES = {
    "submission": ["A submission ends it.", "The hold gets locked in, and that's the match.", "One grip changes everything.",
                   "Trapped with nowhere to go, the tap is the only way out."],
    "flush_slap": ["A flush slap lands.", "Clean contact, right on target.", "That's what a perfect strike looks like."],
    "knockout": ["The exchange ends the match.", "One strike decides it.", "That's the end of the bout."],
    "reversal": ["A reversal flips the position.", "From the bottom to the top in one move.", "The position flips in an instant."],
    "tap": ["The tap comes.", "And there's the tap."],
    "three_pointer": ["The shot drops from deep.", "Pull-up from range, and it's good."],
    "putt": ["The putt drops.", "Pressure putt, right in the cup."],
    "hole_in_one": ["It drops for a hole-in-one."], "dunk": ["The dunk lands."],
}
GENERIC_SENTENCES = ["The decisive moment.", "This is the moment the match turns.", "Watch how this one ends."]


def neutral_description(tags: list[str], seed: str = "") -> str:
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16) if seed else 0
    for t in tags:
        if t in TAG_SENTENCES:
            opts = TAG_SENTENCES[t]
            return opts[h % len(opts)]
    return GENERIC_SENTENCES[h % len(GENERIC_SENTENCES)]


FINISH_TAGS = {"submission", "flush_slap", "knockout", "ko", "tap", "pin", "win", "hole_in_one"}


def clip_moments(start: float, end: float, center_offset: float | None, tags: list[str], score: float,
                 caption: str, relative: bool) -> list[dict]:
    """Split one library clip into setup / payoff / reaction moments around its decisive point.
    Times are relative to the media file: the clip's own rendition (relative=True) or the master."""
    base = 0.0 if relative else start
    dur = end - start
    c = min(max(center_offset if center_offset is not None else dur / 2, 0.5), max(dur - 0.5, 0.5))
    p0, p1 = max(0.0, c - 1.5), min(dur, c + 1.5)
    kind = "finish" if set(tags) & FINISH_TAGS else "payoff"
    out = []
    if p0 >= 1.0:
        out.append({"start": round(base, 2), "end": round(base + p0, 2), "label": "Build-up", "kind": "setup",
                    "intensity": round(max(0.1, score * 0.5), 2), "description": "The exchange builds."})
    out.append({"start": round(base + p0, 2), "end": round(base + p1, 2), "label": _label(tags), "kind": kind,
                "intensity": round(score, 2), "description": (caption or "").strip() or "The decisive moment."})
    if dur - p1 >= 1.0:
        out.append({"start": round(base + p1, 2), "end": round(base + dur, 2), "label": "Reaction", "kind": "reaction",
                    "intensity": round(max(0.1, score * 0.6), 2), "description": "The reaction."})
    return out


def import_ops_clips(db: DB, ops: DB, assets_bucket: str, limit: int = 100, min_score: int = 50,
                     properties: list[str] | None = None, actor: str = "ops-sync") -> dict:
    where = ["c.superseded_by IS NULL", "c.status = 'tagged'", "c.highlight_score >= ?"]
    params: list = [min_score]
    if properties:
        where.append("c.property IN (" + ",".join("?" for _ in properties) + ")")
        params += properties
    params.append(limit)
    clips = ops.query(
        "SELECT c.id, c.property, c.start_sec, c.end_sec, c.peak_offset_sec, c.moment_offset_sec, "
        "c.highlight_score, c.caption, c.transcript, c.source_bucket, c.source_key, c.vertical_key, "
        "c.vertical_mode, s.title AS source_title, s.aired_date::text AS aired_date, s.duration_sec AS source_duration "
        "FROM clips c JOIN clip_sources s ON s.id = c.source_id WHERE " + " AND ".join(where) +
        " ORDER BY c.highlight_score DESC NULLS LAST, c.id LIMIT ?", params)
    if not clips:
        return {"imported": 0, "skipped": []}
    ids = [c["id"] for c in clips]
    marks = ",".join("?" for _ in ids)
    tags: dict[str, list[str]] = {}
    for t in ops.query(f"SELECT clip_id, tag FROM clip_tags WHERE clip_id IN ({marks}) ORDER BY clip_id, tag", ids):
        tags.setdefault(t["clip_id"], []).append(t["tag"])
    talent: dict[str, list[dict]] = {}
    for t in ops.query(f"SELECT ct.clip_id, coalesce(p.name, ct.name_raw) AS name, ct.source FROM clip_talent ct "
                       f"LEFT JOIN people p ON p.id = ct.person_id WHERE ct.clip_id IN ({marks})", ids):
        talent.setdefault(t["clip_id"], []).append(
            # machine detections (nova/face) are NOT verified identities
            {"name": t["name"], "verified": t["source"] == "manual", "detected_by": t["source"]})
    imported, skipped = [], []
    brand_flags = {b["brand_id"]: b.get("content_flags", {}) for b in load_brands()}
    for c in clips:
        brand = _brand(c["property"] or "")
        if not brand:
            skipped.append(f"{c['id']}: unknown property {c['property']!r} (add an alias in config/brands.json)")
            continue
        start, end = float(c["start_sec"]), float(c["end_sec"])
        rel = bool(c["vertical_key"])
        offset = c["moment_offset_sec"] if c["moment_offset_sec"] is not None else c["peak_offset_sec"]
        score = float(c["highlight_score"] or 0) / 100
        ctags = tags.get(c["id"], [])
        aid = clip_asset_id(c["id"])
        rights = db.get("asset_rights", "asset_id", aid) or rights_default(db, brand["brand_id"])
        people = talent.get(c["id"], [])
        identities_ok = bool(rights.get("people_verified")) or (bool(people) and all(p["verified"] for p in people))
        # Copy is built from the description: never feed it machine text that may name unverified athletes.
        safe_caption = (c["caption"] or "").strip() if identities_ok else neutral_description(ctags, c["id"])
        row = {
            "asset_id": aid, "brand_id": canonical_brand_id(brand), "source_brand_name": brand["name"],
            # source titles are raw master filenames; never put them in copy or thumbnails
            "title": f"{_label(ctags)} — {brand['name']}" + (f" ({c['aired_date']})" if c["aired_date"] else ""),
            "description": safe_caption,
            "media_path": f"s3://{assets_bucket}/{c['vertical_key']}" if rel else f"s3://{c['source_bucket']}/{c['source_key']}",
            "media_present": True, "sha256": "", "content_id": c["id"], "parent_asset_id": "",
            "provenance": "ops clip library (pln-fast-aws tagger)" + (" — vertical rendition" if rel else " — master"),
            "owner": rights.get("owner", ""), "license": rights.get("license", ""),
            "territory": rights.get("territory") or [], "rights_expires_at": rights.get("rights_expires_at"),
            "rights_verified": bool(rights.get("rights_verified")),
            "people_verified": identities_ok,
            "music_cleared": bool(rights.get("music_cleared")), "sponsors": [],
            "sponsor_approved": bool(rights.get("sponsor_approved")), "people": people, "facts": [],
            "moments": clip_moments(start, end, float(offset) if offset is not None else None, ctags, score,
                                    safe_caption, rel),
            # knockouts / drops are injury-adjacent: always manual review, never the hook
            "flags": {**brand_flags.get(brand["brand_id"], {}),
                      **({"injury": True} if set(ctags) & INJURY_TAGS else {})}, "series": "",
            "event_name": "", "event_date": c["aired_date"],  # source titles are raw master filenames
            "duration_s": (end - start) if rel else (float(c["source_duration"]) if c["source_duration"] else None),
            "width": 1080 if rel else None, "height": 1920 if rel else None, "has_audio": True,
            "probe": {"ops_clip": {"tags": ctags, "highlight_score": c["highlight_score"],
                                   "machine_caption": c["caption"],  # for reviewers only; never used in copy unless verified
                                   "transcript": c["transcript"], "source_title": c["source_title"],
                                   "vertical_mode": c["vertical_mode"], "start_sec": start, "end_sec": end}},
            # Transcripts are burned in as captions; commentary names athletes, so only when verified.
            "transcript": [{"start": 0.0 if rel else start, "end": (end - start) if rel else end, "text": c["transcript"]}]
            if c["transcript"] and identities_ok else [],
            "is_sample": False, "ingested_at": now_iso(),
        }
        db.insert("assets", row, upsert_key="asset_id")
        imported.append(aid)
    db.audit(actor, "ops_clips_imported", "assets", "", {"imported": len(imported), "skipped": skipped,
                                                          "min_score": min_score, "on": date.today().isoformat()})
    return {"imported": len(imported), "asset_ids": imported, "skipped": skipped}


def rights_default(db: DB, brand_id: str) -> dict:
    """Standing rights policy for newly imported clips (e.g. "PLN owns all content").
    Stored in settings_kv as JSON under rights_default:<brand_id> or rights_default:* .
    Never includes people_verified: identities are confirmed clip by clip."""
    import json
    raw = db.kv_get(f"rights_default:{brand_id}") or db.kv_get("rights_default:*")
    if not raw:
        return {}
    d = json.loads(raw)
    d.pop("people_verified", None)
    return d


def set_rights_default(db: DB, actor: str, brand_id: str = "*", **fields) -> None:
    import json
    fields.pop("people_verified", None)
    db.kv_set(f"rights_default:{brand_id}", json.dumps(fields, sort_keys=True), actor)


def set_asset_rights(db: DB, asset_id: str, actor: str, **fields) -> dict:
    """Record a human's rights/identity confirmation and apply it to the asset immediately."""
    allowed = {"license", "owner", "territory", "rights_expires_at", "rights_verified", "people_verified",
               "music_cleared", "sponsor_approved", "note"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown rights fields {bad}")
    if not db.get("assets", "asset_id", asset_id):
        raise KeyError(asset_id)
    cur = db.get("asset_rights", "asset_id", asset_id) or {"asset_id": asset_id}
    cur.update(fields)
    cur.update({"updated_by": actor, "updated_at": now_iso()})
    db.insert("asset_rights", cur, upsert_key="asset_id")
    db.update("assets", "asset_id", asset_id, {k: v for k, v in fields.items() if k != "note"})
    db.audit(actor, "asset_rights_set", "assets", asset_id, fields)
    return cur
