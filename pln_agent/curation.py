"""Curation queue for a league's own footage (CarJitsu first): a person picks the moment, the team makes the post.

Most CarJitsu finishes happen inside the car, and the editor drops nearly all of them as unreadable on a phone. A
person who knows the sport can still find the ones that work, so this queue puts the clip library in front of them:

* the best-scored library clips from each of the league's recordings (and anything the moment finder thought could
  work), each with a short preview — the clip and three seconds either side — made on the task runner;
* the curator marks where the moment starts, where the payoff lands and where it ends, and writes a line on what
  happens (what the writers may state);
* the runner makes the pick an asset and drafts the posts, which then go through the editor, the writers, the
  on-screen words and the critic like any other draft — except that a person's pick is never thrown away: the editor
  cuts inside the marked range rather than dropping it, and a critic's objection goes to Approvals as a note on the post.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .db import DB, now_iso

PER_RECORDING = 8
PAD_S = 3.0
PREVIEW_W = 480
MIN_PICK_S, MAX_PICK_S = 3.0, 60.0
BRANDS = ("carjitsu",)  # leagues with a queue; add a brand_id to open one


class CurationError(ValueError):
    pass


def _properties(brand_id: str) -> list[str]:
    from .accounts import load_brands
    b = next((x for x in load_brands() if x["brand_id"] == brand_id), None)
    if not b:
        raise CurationError(f"unknown brand {brand_id}")
    return sorted({b["name"], *b.get("aliases", [])})


def fill(db: DB, ops: DB, brand_id: str, per_recording: int = PER_RECORDING, actor: str = "curation") -> dict:
    """Queue the best library clips of each of the league's recordings that aren't drafts or in the queue already."""
    from .ops_sources import clip_asset_id
    props = _properties(brand_id)
    marks = ",".join("?" for _ in props)
    rows = ops.query(
        "SELECT c.id, c.start_sec, c.end_sec, c.highlight_score, c.energy_score, c.caption, c.source_bucket, c.source_key, "
        "s.title AS source_title FROM clips c JOIN clip_sources s ON s.id = c.source_id "
        f"WHERE c.superseded_by IS NULL AND c.status='tagged' AND c.property IN ({marks}) "
        "ORDER BY c.source_key, c.highlight_score DESC, c.id", props)
    seen = {r["media_path"]: r for r in db.all("moment_searches")}  # what the moment finder thought of these clips
    added, per = [], {}
    for r in rows:
        path = f"s3://{r['source_bucket']}/{r['source_key']}"
        taken = per.setdefault(path, [])
        s, e = float(r["start_sec"]), float(r["end_sec"])
        if len(taken) >= per_recording or any(s < te and ts < e for ts, te in taken):
            continue
        taken.append((s, e))
        if db.get("curation_clips", "clip_id", r["id"]) or db.get("assets", "asset_id", clip_asset_id(r["id"])):
            continue
        hint = next((c for c in (seen.get(path) or {}).get("candidates") or [] if c.get("clip_id") == r["id"]), None)
        db.insert("curation_clips", {
            "clip_id": r["id"], "brand_id": brand_id, "media_path": path, "source_title": r["source_title"] or Path(r["source_key"]).stem,
            "start_s": s, "end_s": e, "score": r["highlight_score"], "energy": r["energy_score"], "caption": r["caption"] or "",
            "finder": {k: hint.get(k) for k in ("readable", "strength", "what", "verify_why")} if hint else {},
            "preview_path": "", "thumb_path": "", "preview_status": "pending", "status": "new", "pick": {},
            "created_at": now_iso(), "updated_at": now_iso()})
        added.append(r["id"])
    db.audit(actor, "curation_filled", "curation_clips", brand_id, {"added": len(added), "recordings": len(per)})
    return {"added": added, "recordings": len(per)}


def pick(db: DB, clip_id: str, start: float, end: float, payoff_at: float, note: str, actor: str) -> dict:
    """A person's moment, in seconds of the recording. The asset and drafts are made on the task runner."""
    c = db.get("curation_clips", "clip_id", clip_id)
    if not c:
        raise CurationError("no such clip")
    note = re.sub(r"\s+", " ", note or "").strip()
    if len(note) < 10:
        raise CurationError("write a line on what happens: it's what the writers may say")
    start, end, payoff_at = float(start), float(end), float(payoff_at)
    lo, hi = c["start_s"] - PAD_S - 0.5, c["end_s"] + PAD_S + 0.5
    if not (lo <= start < end <= hi):
        raise CurationError("mark in and out inside the preview")
    if not (MIN_PICK_S <= end - start <= MAX_PICK_S):
        raise CurationError(f"pick {MIN_PICK_S:.0f}–{MAX_PICK_S:.0f} seconds (this is {end - start:.1f}s)")
    if not (start <= payoff_at <= end):
        raise CurationError("the payoff has to be between in and out")
    p = {"start": round(start, 2), "end": round(end, 2), "payoff_at": round(payoff_at, 2), "note": note, "by": actor,
         "at": now_iso(), "status": "pending"}
    db.update("curation_clips", "clip_id", clip_id, {"pick": p, "status": "picked", "updated_at": now_iso()})
    db.audit(actor, "curation_picked", "curation_clips", clip_id, p)
    return p


def dismiss(db: DB, clip_id: str, actor: str) -> None:
    db.update("curation_clips", "clip_id", clip_id, {"status": "dismissed", "updated_at": now_iso()})
    db.audit(actor, "curation_dismissed", "curation_clips", clip_id, {})


def pending(db: DB) -> bool:
    return bool(db.one("SELECT clip_id FROM curation_clips WHERE status<>'dismissed' AND preview_status='pending'")
                or any((c.get("pick") or {}).get("status") == "pending" for c in db.all("curation_clips", "status='picked'")))


# ---------------------------------------------------------------- task runner ------------------------------------

def make_preview(db: DB, clip_id: str, bucket: str, resolve=None, put=None) -> dict:
    """A small MP4 of the clip with a few seconds either side (input-side seek: only those seconds are read)."""
    from .media import resolve_media
    from .predct import _ff, _put
    resolve, put = resolve or resolve_media, put or _put
    c = db.get("curation_clips", "clip_id", clip_id)
    t0 = max(0.0, c["start_s"] - PAD_S)
    dur = c["end_s"] + PAD_S - t0
    with tempfile.TemporaryDirectory() as td:
        prev, thumb = Path(td) / "preview.mp4", Path(td) / "thumb.jpg"
        src = resolve(c["media_path"])
        _ff("-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", src, "-vf", f"scale={PREVIEW_W}:-2", "-c:v", "libx264", "-preset",
            "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(prev))
        _ff("-ss", f"{t0 + PAD_S + 1:.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={PREVIEW_W}:-2", str(thumb))
        base = f"social-agent/curation/{c['brand_id']}/{re.sub(r'[^A-Za-z0-9._-]+', '-', clip_id)[-80:]}"
        upd = {"preview_path": put(prev, bucket, f"{base}/preview.mp4", "video/mp4"),
               "thumb_path": put(thumb, bucket, f"{base}/thumb.jpg", "image/jpeg"),
               "preview_from": round(t0, 2), "preview_status": "ready", "updated_at": now_iso()}
    db.update("curation_clips", "clip_id", clip_id, upd)
    return upd


def make_pick(db: DB, ops: DB, clip_id: str, settings, actor: str = "curation") -> dict:
    """The pick becomes an asset (the library clip, narrowed to the person's range) and its drafts."""
    from .editorial import generate_pack
    from .ops_sources import clip_asset_id, clip_moments, import_ops_clips
    c = db.get("curation_clips", "clip_id", clip_id)
    p = c["pick"]
    r = import_ops_clips(db, ops, settings.ops_assets_bucket, clip_ids=[clip_id], actor=actor)
    aid = clip_asset_id(clip_id)
    a = db.get("assets", "asset_id", aid)
    if not a:
        raise CurationError("; ".join(map(str, r.get("skipped") or [])) or "the library clip couldn't be imported")
    probe = dict(a.get("probe") or {})
    probe["ops_clip"] = {**(probe.get("ops_clip") or {}), "start_sec": p["start"], "end_sec": p["end"]}
    probe["curated"] = {"by": p["by"], "at": p["at"], "note": p["note"], "start": p["start"], "end": p["end"], "payoff_at": p["payoff_at"]}
    db.update("assets", "asset_id", aid, {
        "probe": probe, "description": p["note"],
        "facts": [{"text": p["note"], "verified": True, "source": f"curator {p['by']}"}],
        "moments": clip_moments(p["start"], p["end"], p["payoff_at"] - p["start"], (probe["ops_clip"].get("tags") or []), 0.9,
                                p["note"], False)})
    pack = generate_pack(db, aid, settings, actor=actor)
    return {"asset_id": aid, "variants": [v["variant_id"] for v in pack.get("variants", [])]}


def _set_pick(db: DB, clip_id: str, **kw) -> None:
    c = db.get("curation_clips", "clip_id", clip_id)
    db.update("curation_clips", "clip_id", clip_id, {"pick": {**(c.get("pick") or {}), **kw}, "updated_at": now_iso()})


def run_pending(db: DB, ops: DB | None, settings, bucket: str, limit: int = 12, actor: str = "curation") -> list[dict]:
    """Picks first (a person is waiting on them), then previews."""
    out = []
    for c in db.all("curation_clips", "status='picked'", order="updated_at"):
        if (c.get("pick") or {}).get("status") != "pending" or not ops:
            continue
        try:
            r = make_pick(db, ops, c["clip_id"], settings, actor)
            _set_pick(db, c["clip_id"], status="done", asset_id=r["asset_id"])
            out.append({"clip_id": c["clip_id"], "pick": "done", **r})
        except Exception as e:  # noqa: BLE001 — one pick failing never stops the others
            _set_pick(db, c["clip_id"], status="failed", error=str(e)[:300])
            out.append({"clip_id": c["clip_id"], "pick": "failed", "error": str(e)[:120]})
    for c in db.all("curation_clips", "preview_status='pending' AND status<>'dismissed'", order="score DESC", limit=limit):
        try:
            make_preview(db, c["clip_id"], bucket)
            out.append({"clip_id": c["clip_id"], "preview": "ready"})
        except Exception as e:  # noqa: BLE001
            db.update("curation_clips", "clip_id", c["clip_id"], {"preview_status": "failed", "error": str(e)[:300]})
            out.append({"clip_id": c["clip_id"], "preview": "failed", "error": str(e)[:120]})
    return out


def curated(asset: dict) -> dict | None:
    """The person's pick behind an asset, if it came from this queue."""
    return ((asset or {}).get("probe") or {}).get("curated")
