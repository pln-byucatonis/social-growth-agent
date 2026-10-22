"""PREDCT curation queue: a person picks the moments, then the team makes the posts.

PREDCT is strategic, so it starts hand-picked (config/predct.json): Five Hole and Bottle Jobs shorts and the live
MarketZone / PNN desks, never a "go bet". The queue holds footage waiting to be looked at:

- recordings: MarketZone's encoder keeps a copy of what goes on air as one-minute MPEG-TS files
  (encoder-copy/<channel>/<session start>/<minute>.ts). One recording session is one candidate.
- files: show episodes MarketZone publishes (films/<show>/), files uploaded on the page, or any s3:// link.

Browsers can't play MPEG-TS, so the task runner makes a small MP4 preview of each candidate first. A curator watches
it, marks an in and out point (and where the payoff lands), and writes one or two lines on what is happening and
which market it is about. That note is the only thing the writers may state as fact. The task runner then cuts that
range into its own clip, makes it an asset (PLN Picks brand until the page is renamed PREDCT; gambling and AI-host
flags on, so every post is reviewed on its own page and carries the labels), drafts the posts for the PREDCT accounts
and sends them through the writers' room under PREDCT's rules (no wagering call to action, the never-say list).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .db import DB, now_iso

PREVIEW_W = 640
MAX_PICK_S = 90.0
MIN_PICK_S = 5.0


class CurationError(Exception):
    pass


def policy() -> dict:
    return json.loads((CONFIG_DIR / "predct.json").read_text())


def accounts() -> list[str]:
    return list(policy().get("accounts") or [])


def never_said(text: str) -> list[str]:
    """PREDCT's never-say phrases found in a piece of copy (wagering calls to action, tout language)."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    return [p for p in policy().get("never_say") or [] if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", low)]


def _cid(*parts: str) -> str:
    return "pc-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _list(bucket: str, prefix: str) -> list[dict]:
    import boto3
    s3 = boto3.client("s3")
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, **({"ContinuationToken": token} if token else {})}
        r = s3.list_objects_v2(**kw)
        out += [{"key": o["Key"], "size": o["Size"], "modified": o["LastModified"].isoformat()} for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            return out
        token = r["NextContinuationToken"]


def _stamp(s: str) -> str | None:
    try:
        return datetime.strptime(s, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def scan(db: DB, lister=_list, actor: str = "predct") -> dict:
    """Find footage in the configured places. New candidates wait for a preview; known ones are left alone."""
    pol, new, seen = policy(), [], 0
    for src in pol.get("sources") or []:
        objs = lister(src["bucket"], src["prefix"])
        if src["kind"] == "recording":
            sessions: dict[tuple[str, str], list[dict]] = {}
            for o in objs:
                parts = o["key"][len(src["prefix"]):].split("/")
                if len(parts) == 3 and parts[2].endswith(".ts"):
                    sessions.setdefault((parts[0], parts[1]), []).append(o)
            for (channel, start), segs in sessions.items():
                chans = src.get("channels") or {}
                if channel in chans and chans[channel] is None:  # e.g. the encoder's copy-test
                    continue
                show = chans.get(channel, "other")
                segs = sorted(segs, key=lambda o: o["key"])
                cid = _cid(src["bucket"], src["prefix"], channel, start)
                seen += 1
                if db.get("predct_candidates", "cand_id", cid):
                    continue
                label = pol["shows"].get(show, {}).get("label", show)
                when = _stamp(start)
                _add(db, cid, show, "recording", f"{label} · {when[:16].replace('T', ' ') if when else start} UTC",
                     [f"s3://{src['bucket']}/{o['key']}" for o in segs], when, 60.0 * len(segs))
                new.append(cid)
        else:
            for o in objs:
                if not o["key"].lower().endswith((".mp4", ".mov", ".webm", ".m4v", ".ts")):
                    continue
                path = f"s3://{src['bucket']}/{o['key']}"
                cid = _cid(path)
                seen += 1
                if db.get("predct_candidates", "cand_id", cid):
                    continue
                _add(db, cid, src.get("show", "other"), "file", Path(o["key"]).name, [path], o["modified"], None)
                new.append(cid)
    db.audit(actor, "predct_scanned", "predct_candidates", "", {"new": len(new), "seen": seen})
    return {"new": new, "seen": seen}


def _add(db: DB, cid: str, show: str, kind: str, title: str, sources: list[str], when: str | None,
         duration: float | None, note: str = "") -> None:
    db.insert("predct_candidates", {"cand_id": cid, "show": show, "kind": kind, "title": title, "sources": sources,
                                    "recorded_at": when, "duration_s": duration, "preview_path": "", "thumb_path": "",
                                    "preview_status": "pending", "status": "new", "picks": [], "note": note,
                                    "created_at": now_iso(), "updated_at": now_iso()})


def add_file(db: DB, path: str, show: str, title: str = "", actor: str = "dashboard") -> str:
    """One video already in S3 (an upload from the page, or a link someone pasted)."""
    if not path.startswith("s3://") or "/" not in path[5:]:
        raise CurationError("give an s3://bucket/key link")
    if show not in policy()["shows"]:
        raise CurationError(f"unknown show {show!r}")
    cid = _cid(path)
    if not db.get("predct_candidates", "cand_id", cid):
        _add(db, cid, show, "file", title or Path(path).name, [path], now_iso(), None)
        db.audit(actor, "predct_added", "predct_candidates", cid, {"path": path, "show": show})
    return cid


def pick(db: DB, cid: str, start: float, end: float, note: str, actor: str, payoff_at: float | None = None) -> dict:
    """A curator's chosen moment. The cut, the asset and the drafts are made on the task runner."""
    c = db.get("predct_candidates", "cand_id", cid)
    if not c:
        raise CurationError("no such footage")
    note = re.sub(r"\s+", " ", note or "").strip()
    if len(note) < 15:
        raise CurationError("write a line or two on what happens and which market it's about: it's all the writers may state")
    if bad := never_said(note):
        raise CurationError(f"the note uses PREDCT never-say language: {', '.join(bad)}")
    start, end = float(start), float(end)
    if c.get("duration_s") and end > float(c["duration_s"]) + 0.5:
        raise CurationError(f"the out point is past the end ({c['duration_s']:.0f}s)")
    if not (MIN_PICK_S <= end - start <= MAX_PICK_S):
        raise CurationError(f"pick {MIN_PICK_S:.0f}–{MAX_PICK_S:.0f} seconds (this is {end - start:.1f}s)")
    if payoff_at is not None and not (start <= float(payoff_at) <= end):
        raise CurationError("the payoff point has to be inside the pick")
    picks = list(c.get("picks") or [])
    pid = f"{cid[3:]}-{len(picks) + 1}"
    picks.append({"pick_id": pid, "start": round(start, 2), "end": round(end, 2), "note": note, "by": actor,
                  "payoff_at": round(float(payoff_at), 2) if payoff_at is not None else None, "status": "pending", "at": now_iso()})
    db.update("predct_candidates", "cand_id", cid, {"picks": picks, "status": "picked", "updated_at": now_iso()})
    db.audit(actor, "predct_picked", "predct_candidates", cid, {"pick": pid, "start": start, "end": end})
    return {"pick_id": pid}


def dismiss(db: DB, cid: str, actor: str) -> None:
    db.update("predct_candidates", "cand_id", cid, {"status": "dismissed", "updated_at": now_iso()})
    db.audit(actor, "predct_dismissed", "predct_candidates", cid, {})


def pending(db: DB) -> bool:
    return bool(db.one("SELECT cand_id FROM predct_candidates WHERE preview_status='pending' AND status<>'dismissed'")
                or any(p.get("status") == "pending" for c in db.all("predct_candidates", "status='picked'")
                       for p in c.get("picks") or []))


# ---------------------------------------------------------------- task runner (ffmpeg + S3) -------------------------

def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=1800)


def _probe(f: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_type",
                          "-of", "json", f], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": float(j.get("format", {}).get("duration") or 0), "width": v.get("width"), "height": v.get("height"),
            "has_audio": any(s.get("codec_type") == "audio" for s in j.get("streams", []))}


def _fetch(paths: list[str], work: Path) -> Path:
    """The candidate as one local file (a recording's segments joined in order)."""
    import boto3
    s3 = boto3.client("s3")
    local = []
    for i, p in enumerate(paths):
        b, _, k = p[5:].partition("/")
        f = work / f"src{i:03d}{Path(k).suffix}"
        s3.download_file(b, k, str(f))
        local.append(f)
    if len(local) == 1:
        return local[0]
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{f}'\n" for f in local))
    joined = work / "joined.ts"
    _ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined))
    return joined


def _put(f: Path, bucket: str, key: str, ctype: str) -> str:
    import boto3
    boto3.client("s3").upload_file(str(f), bucket, key, ExtraArgs={"ContentType": ctype})
    return f"s3://{bucket}/{key}"


def make_preview(db: DB, cid: str, bucket: str) -> dict:
    c = db.get("predct_candidates", "cand_id", cid)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        src = _fetch(c["sources"], work)
        info = _probe(str(src))
        prev, thumb = work / "preview.mp4", work / "thumb.jpg"
        _ff("-i", str(src), "-vf", f"scale={PREVIEW_W}:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(prev))
        _ff("-ss", str(min(5.0, info["duration"] / 2)), "-i", str(src), "-frames:v", "1", "-vf", f"scale={PREVIEW_W}:-2", str(thumb))
        base = f"social-agent/predct/previews/{cid}"
        upd = {"preview_path": _put(prev, bucket, f"{base}/preview.mp4", "video/mp4"),
               "thumb_path": _put(thumb, bucket, f"{base}/thumb.jpg", "image/jpeg"),
               "duration_s": round(info["duration"], 2), "preview_status": "ready", "updated_at": now_iso()}
    db.update("predct_candidates", "cand_id", cid, upd)
    return upd


def asset_row(c: dict, p: dict, master: str, info: dict, thumb: str = "") -> dict:
    """The asset for one pick: our own footage, AI or animated hosts, gambling-adjacent subject."""
    from .ops_sources import clip_moments
    pol = policy()
    label = pol["shows"].get(c["show"], {}).get("label", c["show"])
    dur = round(info["duration"], 2)
    center = (p["payoff_at"] - p["start"]) if p.get("payoff_at") is not None else None
    return {
        "asset_id": f"predct-{p['pick_id']}", "brand_id": "pln_picks", "source_brand_name": "PREDCT",
        "title": f"{label} — PREDCT", "description": p["note"], "media_path": master, "media_present": True,
        "sha256": "", "content_id": p["pick_id"], "parent_asset_id": "",
        "provenance": f"PREDCT curation queue: {c['title']} {p['start']:.0f}–{p['end']:.0f}s, picked by {p.get('by', '')}",
        "owner": "Pro League Network (PREDCT)", "license": "owned", "territory": ["US"], "rights_expires_at": None,
        "rights_verified": True, "people_verified": True,  # AI / animated hosts: nobody real to name
        "music_cleared": True, "sponsors": [], "sponsor_approved": True, "people": [],
        "facts": [{"text": p["note"], "verified": True, "source": f"curator {p.get('by', '')}"}],
        "moments": clip_moments(0.0, dur, center, [], 0.8, p["note"], True),
        "flags": {"gambling": True, "ai_generated_person": True}, "series": c["show"],
        "event_name": label, "event_date": (c.get("recorded_at") or "")[:10] or None,
        "duration_s": dur, "width": info.get("width"), "height": info.get("height"), "has_audio": info.get("has_audio", True),
        "probe": {"ops_clip": {"tags": [], "machine_caption": p["note"], "transcript": "", "source_title": label,
                               "start_sec": 0.0, "end_sec": dur, "thumb": thumb or None},
                  "predct": {"cand_id": c["cand_id"], "pick_id": p["pick_id"], "show": c["show"]}},
        "transcript": [], "is_sample": False, "ingested_at": now_iso(),
    }


def make_pick(db: DB, cid: str, pid: str, settings, bucket: str, actor: str = "predct") -> dict:
    """Cut the picked range into its own clip, make it an asset and draft the PREDCT posts."""
    from .editorial import generate_pack
    c = db.get("predct_candidates", "cand_id", cid)
    p = next(x for x in c["picks"] if x["pick_id"] == pid)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        src = _fetch(c["sources"], work)
        out, thumb = work / "master.mp4", work / "thumb.jpg"
        _ff("-ss", str(p["start"]), "-i", str(src), "-t", str(p["end"] - p["start"]), "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out))
        info = _probe(str(out))
        _ff("-ss", str(min(info["duration"] / 2, (p["payoff_at"] or p["start"]) - p["start"] or 1.0)), "-i", str(out),
            "-frames:v", "1", str(thumb))
        base = f"social-agent/predct/masters/{pid}"
        master = _put(out, bucket, f"{base}/master.mp4", "video/mp4")
        thumb_path = _put(thumb, bucket, f"{base}/thumb.jpg", "image/jpeg")
    row = asset_row(c, p, master, info, thumb_path)
    db.insert("assets", row, upsert_key="asset_id")
    pack = generate_pack(db, row["asset_id"], settings, actor=actor)
    return {"asset_id": row["asset_id"], "pack": pack.get("pack_id"),
            "variants": [v["variant_id"] for v in pack.get("variants", [])], "notes": pack.get("notes", [])[:5]}


def _set_pick(db: DB, cid: str, pid: str, **kw) -> None:
    c = db.get("predct_candidates", "cand_id", cid)
    picks = [({**x, **kw} if x["pick_id"] == pid else x) for x in c.get("picks") or []]
    db.update("predct_candidates", "cand_id", cid, {"picks": picks, "updated_at": now_iso()})


def run_pending(db: DB, settings, bucket: str, limit: int = 4, actor: str = "predct") -> list[dict]:
    """Task runner: previews first (so curators have something to watch), then picks."""
    out = []
    for c in db.all("predct_candidates", "preview_status='pending' AND status<>'dismissed'", order="created_at", limit=limit):
        try:
            make_preview(db, c["cand_id"], bucket)
            out.append({"cand_id": c["cand_id"], "preview": "ready"})
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the queue
            db.update("predct_candidates", "cand_id", c["cand_id"], {"preview_status": "failed", "note": str(e)[:300]})
            out.append({"cand_id": c["cand_id"], "preview": "failed", "error": str(e)[:120]})
    for c in db.all("predct_candidates", "status='picked'", order="updated_at"):
        for p in c.get("picks") or []:
            if p.get("status") != "pending":
                continue
            try:
                r = make_pick(db, c["cand_id"], p["pick_id"], settings, bucket, actor)
                _set_pick(db, c["cand_id"], p["pick_id"], status="done", asset_id=r["asset_id"])
                out.append({"pick": p["pick_id"], **r})
            except Exception as e:  # noqa: BLE001
                _set_pick(db, c["cand_id"], p["pick_id"], status="failed", error=str(e)[:300])
                out.append({"pick": p["pick_id"], "status": "failed", "error": str(e)[:120]})
    return out
