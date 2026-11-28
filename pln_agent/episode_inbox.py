"""Episode inbox: highlights from the pln-fast-aws episode pipeline (a new master in the Masters folder becomes cuts,
captioned 9:16 verticals, .srt files and Gemini's post ideas). The pipeline writes one sidecar per top highlight to
config/episode_inbox.json `inbox_prefix`; `import_inbox` turns each into an asset:

* rights: a PLN-owned master (PLN owns the masters and all rights), worldwide, rights verified. No human entry needed.
* people: the athletes in the clip, verified, because every PLN athlete signs a likeness release.
* music: cleared, unless the editor notes say the music may be copyrighted (then it is blocked until cleared).
* flags: the brand's usual flags come from the planner; `gambling` is added when the notes mention a gambling sponsor
  (Roobet on CarJitsu), so the post carries that label and its checklist.
* media: the captioned 9:16 vertical, used as is (media.py does not burn captions or punch in on it again). The
  16:9 cut, the English and Spanish .srt and Gemini's per-platform ideas are kept in `probe.episode`.

The asset id is the same one an ops clip import would give the clip (ops_sources.clip_asset_id), and the ops import
skips pipeline clips (`ep-` ids), so a highlight never arrives twice. Idempotent: importing again changes nothing.
"""
from __future__ import annotations

import json
import os
import re

from .ai_inbox import _s3_list, _s3_read, brand_for
from .accounts import canonical_brand_id
from .config import CONFIG_DIR
from .db import DB, now_iso
from .ops_sources import clip_asset_id

SCHEMA = "pln-episode-clip/1"


class EpisodeInboxError(ValueError):
    pass


def policy() -> dict:
    return json.loads((CONFIG_DIR / "episode_inbox.json").read_text())


def _mentions(notes: list[str], words: list[str]) -> list[str]:
    return [n for n in notes if any(w in n.lower() for w in words)]


def srt_cues(text: str) -> list[dict]:
    """.srt -> [{"start", "end", "text"}] (the asset transcript format)."""
    cues = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        m = next((re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", l) for l in lines if "-->" in l), None)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = " ".join(l.strip() for l in lines[lines.index(next(l for l in lines if "-->" in l)) + 1:])
        cues.append({"start": round(start, 3), "end": round(end, 3), "text": body})
    return cues


def asset_row(meta: dict, bucket: str, pol: dict, transcript: list[dict]) -> dict:
    brand = brand_for(str(meta.get("property") or "pln"))
    notes = [str(n) for n in meta.get("notes_for_editor") or []]
    gambling = _mentions(notes, pol.get("gambling_words", []))
    music_risk = _mentions(notes, pol.get("music_risk_words", []))
    dur = meta.get("duration_s")
    dur = float(dur) if isinstance(dur, (int, float)) else None
    media = meta.get("media") or {}
    title = str(meta.get("title") or meta["id"])[:120]
    ideas = [s for s in meta.get("social") or [] if isinstance(s, dict)]
    hooks = [str(s.get("hook_first_2s")) for s in ideas if s.get("hook_first_2s")]
    description = " ".join(x for x in (meta.get("hook"), meta.get("why")) if x)
    if hooks:
        description += " Hook ideas: " + " / ".join(dict.fromkeys(hooks))
    people = [{"name": str(p), "verified": True, "likeness_release": True, "detected_by": "episode-pipeline"}
              for p in meta.get("people") or [] if p and str(p).lower() != "unclear"]
    return {
        "asset_id": clip_asset_id(meta["id"]), "brand_id": canonical_brand_id(brand), "source_brand_name": brand["name"],
        "title": title, "description": description[:2000],
        "media_path": f"s3://{bucket}/{media['vertical']}", "media_present": True, "sha256": "",
        "content_id": str(meta["id"])[:80], "parent_asset_id": "",
        "provenance": (f"PLN-owned master: {meta.get('master', '')}; episode pipeline run {meta.get('run_id', '')} "
                       f"({meta.get('episode_title', '')}, highlight #{meta.get('rank', '')}, Gemini score {meta.get('score', '')})"),
        "owner": pol.get("owner", "Pro League Network"), "license": "PLN-owned master (PLN owns all rights)",
        "territory": pol.get("territory", ["WORLDWIDE"]), "rights_expires_at": None, "rights_verified": True,
        "people_verified": True, "music_cleared": not music_risk,
        "sponsors": [], "sponsor_approved": False, "people": people, "facts": [],
        "moments": [{"start": 0.0, "end": dur, "label": title, "kind": "payoff"}] if dur else [],
        "flags": {"gambling": True} if gambling else {},
        "series": str(meta.get("episode_title") or ""), "event_name": str(meta.get("episode_title") or ""),
        "event_date": meta.get("aired") or None, "duration_s": dur,
        "width": int(meta.get("width") or 1080), "height": int(meta.get("height") or 1920), "has_audio": True,
        "probe": {"episode": {"id": meta["id"], "episode_key": meta.get("episode_key"), "rank": meta.get("rank"),
                              "captions_burned_in": bool(meta.get("captioned", True)), "media": media,
                              "kind": meta.get("kind"), "hook": meta.get("hook"), "social_ideas": ideas[:12],
                              "notes_for_editor": notes, "gambling_notes": gambling, "music_notes": music_risk}},
        "transcript": transcript, "is_sample": False, "ingested_at": now_iso(),
    }


def import_inbox(db: DB, bucket: str | None = None, lister=_s3_list, reader=_s3_read, actor: str = "episode-inbox") -> dict:
    bucket = bucket or os.environ.get("PLN_RENDER_BUCKET")
    if not bucket:
        raise EpisodeInboxError("needs PLN_RENDER_BUCKET")
    pol = policy()
    added, skipped = [], []
    for side in (k for k in lister(bucket, pol["inbox_prefix"]) if k.endswith(".json")):
        try:
            meta = json.loads(reader(bucket, side))
        except (ValueError, KeyError) as e:
            skipped.append({"key": side, "why": f"unreadable sidecar: {e}"})
            continue
        if meta.get("schema") != SCHEMA or not meta.get("id") or not (meta.get("media") or {}).get("vertical"):
            skipped.append({"key": side, "why": "not an episode clip sidecar (schema, id or vertical missing)"})
            continue
        if db.get("assets", "asset_id", clip_asset_id(meta["id"])):
            continue
        transcript = []
        en = (meta.get("media") or {}).get("srt_en")
        if en:
            try:
                transcript = srt_cues(reader(bucket, en).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001  captions are already burned in; the transcript is a nice-to-have
                transcript = []
        row = asset_row(meta, bucket, pol, transcript)
        db.insert("assets", row, upsert_key="asset_id")
        db.audit(actor, "asset_ingested", "assets", row["asset_id"], {"source": "episode-inbox", "sidecar": side})
        added.append(row["asset_id"])
    if added or skipped:
        db.audit(actor, "episode_inbox_imported", "assets", "", {"added": len(added), "skipped": skipped[:20]})
    return {"added": added, "skipped": skipped}
