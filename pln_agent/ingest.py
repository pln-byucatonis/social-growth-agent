"""Ingestion: licensed asset folder + sidecar JSON, official content calendar CSV, ffprobe metadata,
optional caption-file transcripts and FFmpeg scene detection. No paid model calls are needed to boot.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .accounts import canonical_brand_id, resolve_brand
from .db import DB, now_iso

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def ffprobe(path: Path) -> dict | None:
    if not shutil.which("ffprobe") or not path.exists():
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        return json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def summarize_probe(p: dict | None) -> dict:
    if not p:
        return {}
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in p.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
        n, d = v["r_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    return {"duration_s": float(p.get("format", {}).get("duration", 0) or 0) or None,
            "width": v.get("width"), "height": v.get("height"), "fps": fps,
            "video_codec": v.get("codec_name"), "has_audio": a is not None}


def detect_scenes(path: Path, threshold: float = 0.35, limit: int = 50) -> list[float]:
    """Return scene-change timestamps (seconds) via FFmpeg's scene filter. Optional hook."""
    if not shutil.which("ffmpeg") or not path.exists():
        return []
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", f"select='gt(scene,{threshold})',showinfo",
                           "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    ts = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]
    return ts[:limit]


def parse_caption_file(path: Path) -> list[dict]:
    """Parse .srt/.vtt into [{start,end,text}] — a cheap transcript hook (no ASR credits needed)."""
    if not path.exists():
        return []
    text = path.read_text(errors="replace")
    pat = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.+?)(?:\n\s*\n|\Z)", re.S)

    def sec(t: str) -> float:
        h, m, s = t.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    return [{"start": sec(a), "end": sec(b), "text": " ".join(c.split())} for a, b, c in pat.findall(text)]


def external_transcribe(path: Path) -> list[dict]:
    """Optional: PLN_TRANSCRIBE_CMD='whisper-cli --srt {input} -o {output}' writes an .srt we then parse."""
    cmd = os.environ.get("PLN_TRANSCRIBE_CMD")
    if not cmd or not path.exists():
        return []
    out = path.with_suffix(".auto.srt")
    subprocess.run(cmd.format(input=str(path), output=str(out)), shell=True, timeout=1800, check=False)
    return parse_caption_file(out)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


REQUIRED_SIDECAR = ["asset_id", "brand", "title"]


def ingest_sidecar(db: DB, sidecar: Path, actor: str = "ingest", scenes: bool = False) -> dict:
    meta = json.loads(sidecar.read_text())
    missing = [k for k in REQUIRED_SIDECAR if not meta.get(k)]
    if missing:
        raise ValueError(f"{sidecar.name}: missing required fields {missing}")
    brand = resolve_brand(meta["brand"])
    media_path = meta.get("media_path") or ""
    media = (sidecar.parent / media_path) if media_path and not os.path.isabs(media_path) else Path(media_path)
    if not media_path:
        for ext in VIDEO_EXT:
            cand = sidecar.with_suffix(ext)
            if cand.exists():
                media = cand
                break
    present = bool(media_path or media) and media.exists() and media.is_file()
    probe = ffprobe(media) if present else None
    ps = summarize_probe(probe)
    transcript = []
    if present:
        transcript = parse_caption_file(media.with_suffix(".srt")) or parse_caption_file(media.with_suffix(".vtt")) \
            or external_transcribe(media)
    moments = meta.get("moments") or []
    if present and scenes:
        meta.setdefault("analysis", {})["scene_changes_s"] = detect_scenes(media)
    # moments must sit inside the real clip; out-of-range moments are flagged, not silently kept
    warnings = []
    if ps.get("duration_s"):
        for m in moments:
            if m.get("end", 0) > ps["duration_s"] + 0.05:
                warnings.append(f"moment '{m.get('label')}' ends at {m['end']}s beyond clip duration {ps['duration_s']:.2f}s")
                m["out_of_range"] = True
    row = {
        "asset_id": meta["asset_id"], "brand_id": canonical_brand_id(brand),
        "source_brand_name": meta.get("source_brand") or brand["name"],
        "title": meta["title"], "description": meta.get("description", ""),
        "media_path": str(media) if (media_path or present) else "", "media_present": present,
        "sha256": sha256(media) if present else "", "content_id": meta.get("content_id", ""),
        "parent_asset_id": meta.get("parent_asset_id", ""), "provenance": meta.get("provenance", ""),
        "owner": meta.get("owner", ""), "license": meta.get("license", ""), "territory": meta.get("territory", []),
        "rights_expires_at": meta.get("rights_expires_at"), "rights_verified": meta.get("rights_verified", False),
        "people_verified": meta.get("people_verified", False), "music_cleared": meta.get("music_cleared", False),
        "sponsors": meta.get("sponsors", []), "sponsor_approved": meta.get("sponsor_approved", False),
        "people": meta.get("people", []), "facts": meta.get("facts", []), "moments": moments,
        "flags": meta.get("flags", {}), "series": meta.get("series", ""), "event_name": meta.get("event_name", ""),
        "event_date": meta.get("event_date"), "duration_s": ps.get("duration_s"), "width": ps.get("width"),
        "height": ps.get("height"), "has_audio": ps.get("has_audio") if present else meta.get("has_audio"),
        "probe": {**ps, "analysis": meta.get("analysis", {}), "warnings": warnings},
        "transcript": transcript, "is_sample": meta.get("is_sample", False), "ingested_at": now_iso(),
    }
    db.insert("assets", row, upsert_key="asset_id")
    db.audit(actor, "asset_ingested", "assets", row["asset_id"],
             {"media_present": present, "sidecar": sidecar.name, "warnings": warnings})
    return db.get("assets", "asset_id", row["asset_id"])


def scan_inbox(db: DB, inbox: Path, scenes: bool = False) -> list[dict]:
    """Watch-folder pass: every *.json sidecar in the inbox is (re)ingested; returns ingested assets."""
    results = []
    for sc in sorted(inbox.glob("*.json")):
        try:
            results.append(ingest_sidecar(db, sc, scenes=scenes))
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            db.audit("ingest", "asset_ingest_failed", "assets", sc.name, {"error": str(e)})
            results.append({"error": str(e), "sidecar": sc.name})
    return results


def watch(db: DB, inbox: Path, interval_s: float = 10.0, iterations: int | None = None) -> None:
    """Simple polling watcher (portable; no inotify dependency). Re-ingests changed sidecars."""
    import time
    mtimes: dict[Path, float] = {}
    n = 0
    while iterations is None or n < iterations:
        for sc in inbox.glob("*.json"):
            m = sc.stat().st_mtime
            if mtimes.get(sc) != m:
                mtimes[sc] = m
                try:
                    a = ingest_sidecar(db, sc)
                    print(f"ingested {a['asset_id']} (media_present={a['media_present']})")
                except Exception as e:  # keep watching
                    print(f"failed {sc.name}: {e}")
        n += 1
        if iterations is None or n < iterations:
            time.sleep(interval_s)


def load_calendar(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["brand_id"] = canonical_brand_id(resolve_brand(r["brand"]))
    return rows
