"""Music library: beds a person has heard and approved, each with a record of where it came from (config/music.json).

Two ways in:
* Generated — ACE-Step on the PREDCT GPU box writes each bed with a sidecar JSON (model, version, licence, prompt,
  seed, checksum) and the pair is copied to the render bucket's music inbox. `import_inbox` reads the sidecars and
  lists every bed as pending. Only beds from a model whose licence is in `generated_licenses` are taken.
* Licensed — a track from a library PLN pays for, added on the Music page with the provider, the licence and the
  platforms that licence covers.

Nothing plays under a post until someone has listened and approved it, and its licence clears the platform the
post is going to. Which approved bed a post gets is fixed by the post's id, so a re-render keeps the same music.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .config import CONFIG_DIR
from .db import DB, now_iso

AUDIO = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")


class MusicError(ValueError):
    pass


def policy() -> dict:
    return json.loads((CONFIG_DIR / "music.json").read_text())


def _bid(path: str) -> str:
    return "mb-" + hashlib.sha1(path.encode()).hexdigest()[:12]


def _license_key(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", (s or "").lower()).strip("-")


def _s3_list(bucket: str, prefix: str) -> list[str]:
    from .predct import _list
    return [o["key"] for o in _list(bucket, prefix)]


def _s3_read(bucket: str, key: str) -> bytes:
    import boto3
    return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def import_inbox(db: DB, bucket: str | None = None, lister=_s3_list, reader=_s3_read, actor: str = "music") -> dict:
    """List every generated bed in the inbox that has a sidecar and a commercial-use model licence, as pending."""
    bucket = bucket or os.environ.get("PLN_RENDER_BUCKET")
    if not bucket:
        raise MusicError("needs PLN_RENDER_BUCKET")
    pol = policy()
    keys = lister(bucket, pol["inbox_prefix"])
    audio = {k.rsplit(".", 1)[0]: k for k in keys if k.lower().endswith(AUDIO)}
    added, skipped = [], []
    for side in (k for k in keys if k.endswith(".json")):
        stem = side[:-5]
        if stem not in audio:
            skipped.append({"key": side, "why": "no audio file next to the sidecar"})
            continue
        path = f"s3://{bucket}/{audio[stem]}"
        if db.get("music_beds", "bed_id", _bid(path)):
            continue
        try:
            meta = json.loads(reader(bucket, side))
        except (ValueError, KeyError) as e:
            skipped.append({"key": side, "why": f"unreadable sidecar: {e}"})
            continue
        lic = _license_key(meta.get("license", ""))
        if lic not in pol["generated_licenses"]:
            skipped.append({"key": side, "why": f"model licence '{meta.get('license')}' is not cleared for commercial use"})
            continue
        if not meta.get("instrumental", True):
            skipped.append({"key": side, "why": "has vocals; beds go under commentary, so instrumental only"})
            continue
        db.insert("music_beds", {
            "bed_id": _bid(path), "path": path, "title": meta.get("title") or stem.rsplit("/", 1)[-1],
            "brands": meta.get("brands") or [], "mood": meta.get("mood", ""), "bpm": meta.get("bpm"),
            "duration_s": meta.get("duration_s"), "source": "generated",
            "provider": meta.get("model", ""), "model_version": meta.get("model_version", ""),
            "license": meta.get("license", ""), "license_url": meta.get("license_url", ""),
            "platforms": pol["platforms"], "prompt": meta.get("prompt", ""), "seed": str(meta.get("seed", "")),
            "sha256": meta.get("sha256", ""), "status": "pending", "note": "", "created_at": now_iso(),
            "updated_at": now_iso()})
        added.append(_bid(path))
    db.audit(actor, "music_inbox_imported", "music_beds", "", {"added": len(added), "skipped": skipped[:20]})
    return {"added": added, "skipped": skipped}


def add_licensed(db: DB, path: str, title: str, provider: str, license: str, platforms: list[str],
                 brands: list[str], mood: str, actor: str, license_url: str = "") -> str:
    """A track from a paid library. The licence text and the platforms it covers are what the person says they are."""
    if not path.startswith("s3://") or not path.lower().endswith(AUDIO):
        raise MusicError("needs an s3:// link to an audio file (wav, mp3, flac, m4a, aac, ogg)")
    if not provider.strip() or not license.strip():
        raise MusicError("say which library it's from and what the licence is")
    plats = [p for p in platforms if p in policy()["platforms"]]
    if not plats:
        raise MusicError("tick at least one platform the licence covers")
    bid = _bid(path)
    db.insert("music_beds", {
        "bed_id": bid, "path": path, "title": title or path.rsplit("/", 1)[-1], "brands": brands, "mood": mood,
        "source": "licensed", "provider": provider.strip(), "license": license.strip(), "license_url": license_url,
        "platforms": plats, "status": "pending", "note": "", "created_at": now_iso(), "updated_at": now_iso()},
        upsert_key="bed_id")
    db.audit(actor, "music_bed_added", "music_beds", bid, {"provider": provider, "license": license, "platforms": plats})
    return bid


def review(db: DB, bed_id: str, approve: bool, actor: str, note: str = "") -> None:
    if not db.get("music_beds", "bed_id", bed_id):
        raise MusicError("no such bed")
    db.update("music_beds", "bed_id", bed_id, {"status": "approved" if approve else "rejected", "reviewed_by": actor,
                                                 "reviewed_at": now_iso(), "note": note, "updated_at": now_iso()})
    db.audit(actor, "music_bed_" + ("approved" if approve else "rejected"), "music_beds", bed_id, {"note": note})


def cleared(bed: dict, brand_id: str, platform: str) -> bool:
    return (bed.get("status") == "approved" and bool(bed.get("path")) and bool(bed.get("license"))
            and (not bed.get("brands") or brand_id in bed["brands"])
            and (not platform or platform in (bed.get("platforms") or [])))


def pick(db: DB, brand_id: str, platform: str, key: str = "") -> dict | None:
    """An approved bed for this brand that its licence clears on this platform. Moods in the brand's list come
    first; among those, the post's id picks one, so the same post always gets the same bed."""
    beds = [b for b in db.all("music_beds", "status='approved'", order="bed_id") if cleared(b, brand_id, platform)]
    if not beds:
        return None
    moods = policy()["moods"].get(brand_id) or []
    fit = [b for b in beds if b.get("mood") in moods] or beds
    return fit[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(fit)]
