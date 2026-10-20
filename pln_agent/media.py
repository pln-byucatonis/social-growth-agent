"""Native media packaging with FFmpeg: trim per EDL, reframe (9:16 / 4:5 / 1:1 / 16:9), burn on-screen text +
caption track, extract a thumbnail. Without source media we write the EDL + an explicit "NOT RENDERED"
placeholder preview — never a fake video. No platform logos or third-party watermarks are added.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Settings
from .db import DB, now_iso

FONT_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


_s3 = None


def resolve_media(path: str, content_type: str | None = None, expires: int = 3600) -> str:
    """Local path as-is; s3://bucket/key → short-lived presigned HTTPS URL (ffmpeg range-reads it; browsers
    play/preview it). Signing is local — no network call. Uses the runtime's ambient AWS credentials."""
    if not path.startswith("s3://"):
        return path
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client("s3", config=Config(signature_version="s3v4", region_name="us-east-1"))
    bucket, _, key = path[5:].partition("/")
    params = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ResponseContentType"] = content_type
    return _s3.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def asset_thumb_url(asset: dict) -> str:
    thumb = ((asset.get("probe") or {}).get("ops_clip") or {}).get("thumb")
    return resolve_media(thumb, "image/jpeg") if thumb else ""


def asset_preview(asset: dict, segments: list[dict] | None = None) -> dict:
    """Browser-playable source clip for reviewers, cued to the post's cut (#t=start,end)."""
    path = asset.get("media_path") or ""
    if not (asset.get("media_present") and path.startswith("s3://")):
        return {}
    frag = ""
    if segments:
        frag = f"#t={segments[0]['source_in']:.2f},{segments[-1]['source_out']:.2f}"
    return {"src": resolve_media(path, "video/mp4") + frag, "poster": asset_thumb_url(asset)}


def upload_render(outdir: Path, variant_id: str, bucket: str) -> str:
    """Persist a finished render to S3 (container disks are ephemeral). Returns the s3:// path of the video."""
    import boto3
    s3 = boto3.client("s3")
    prefix = f"social-agent/renders/{_safe(variant_id)}"
    for name, ctype in (("video.mp4", "video/mp4"), ("thumbnail.jpg", "image/jpeg"), ("edl.json", "application/json"),
                        ("captions.srt", "application/x-subrip")):
        f = outdir / name
        if f.exists():
            s3.upload_file(str(f), bucket, f"{prefix}/{name}", ExtraArgs={"ContentType": ctype})
    return f"s3://{bucket}/{prefix}/video.mp4"


def _font() -> str | None:
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def _wrap(text: str, width: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _reframe(out: dict, src_w: int, src_h: int, subject_x: float = 0.5) -> str:
    """Scale to cover, then crop. subject_x (0..1 across the source width) centres the crop on the
    action instead of the frame centre; the crop is clamped so it never runs off the picture."""
    W, H = out["w"], out["h"]
    fx = min(max(float(subject_x), 0.0), 1.0)
    x = f"(in_w-{W})/2" if abs(fx - 0.5) < 1e-3 else f"'max(0,min(in_w-{W},in_w*{fx:.4f}-{W}/2))'"
    return (f"scale=w='if(gt(a,{W}/{H}),-2,{W})':h='if(gt(a,{W}/{H}),{H},-2)',"
            f"crop={W}:{H}:{x}:(in_h-{H})/2,setsar=1")


ANALYSIS_W, ANALYSIS_H, ANALYSIS_FPS = 64, 36, 4


def motion_centroid(frames: list[bytes], w: int = ANALYSIS_W) -> tuple[float, float]:
    """Horizontal centre of motion across low-res grayscale frames: per-column sum of absolute frame
    differences, then the energy-weighted mean column. Returns (x 0..1, energy per pixel per frame)."""
    if len(frames) < 2:
        return 0.5, 0.0
    cols = [0.0] * w
    for a, b in zip(frames, frames[1:]):
        for i in range(0, min(len(a), len(b))):
            d = a[i] - b[i]
            cols[i % w] += d if d > 0 else -d
    total = sum(cols)
    if total <= 0:
        return 0.5, 0.0
    x = sum((i + 0.5) / w * c for i, c in enumerate(cols)) / total
    return x, total / (len(frames) - 1) / max(len(frames[0]), 1)


def subject_x(src: str, segments: list[dict], timeout: int = 60) -> tuple[float, str]:
    """Where the action is in a landscape master, from motion in the cut's own seconds (cheap: a few
    frames at 64x36). Falls back to centre when there's too little motion or FFmpeg fails."""
    xs, ws = [], []
    frame = ANALYSIS_W * ANALYSIS_H
    for s in segments:
        dur = max(0.5, s["source_out"] - s["source_in"])
        try:
            proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{s['source_in']:.3f}",
                                   "-t", f"{dur:.3f}", "-i", src, "-vf",
                                   f"fps={ANALYSIS_FPS},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray",
                                   "-f", "rawvideo", "-"], capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            continue
        data = proc.stdout
        frames = [data[i:i + frame] for i in range(0, len(data) - frame + 1, frame)]
        x, energy = motion_centroid(frames)
        if energy > 0.5:  # mean abs change per pixel (0-255): below this it's noise / a static shot
            xs.append(x)
            ws.append(energy * dur)
    if not xs:
        return 0.5, "centre crop (no clear motion found)"
    fx = sum(x * w for x, w in zip(xs, ws)) / sum(ws)
    return round(fx, 4), f"crop centred on motion at {fx:.0%} of frame width"


def _srt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def build_caption_track(variant: dict, asset: dict) -> list[dict]:
    """Map source transcript cues into output timeline; fall back to on-screen text."""
    cues, t_out = [], 0.0
    for seg in variant["edl"].get("segments", []):
        for c in asset.get("transcript") or []:
            if c["end"] > seg["source_in"] and c["start"] < seg["source_out"]:
                cues.append({"start": t_out + max(0, c["start"] - seg["source_in"]),
                             "end": t_out + min(seg["source_out"], c["end"]) - seg["source_in"], "text": c["text"]})
        t_out += seg["source_out"] - seg["source_in"]
    return cues


def render_variant(db: DB, variant_id: str, settings: Settings, actor: str = "renderer") -> dict:
    v = db.get("variants", "variant_id", variant_id)
    asset = db.get("assets", "asset_id", v["asset_id"])
    outdir = settings.render_dir / _safe(variant_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "edl.json").write_text(json.dumps({"variant_id": variant_id, "source": asset["media_path"],
                                                 "source_sha256": asset["sha256"], **v["edl"]}, indent=2))
    segs = v["edl"].get("segments", [])
    out = v["edl"].get("output")
    font = _font()
    if not segs or not out:
        status, path, detail = "not_applicable", "", "no video output for this format"
    elif not (asset["media_present"] and shutil.which("ffmpeg")):
        preview = outdir / "PREVIEW_NOT_RENDERED.svg"
        lines = "".join(f'<text x="40" y="{200 + i * 60}" font-size="36" fill="#fff">{s["source_timecode"]} — {s["purpose"]}</text>'
                        for i, s in enumerate(segs))
        preview.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{out["w"] // 2}" height="{out["h"] // 2}" '
                           f'viewBox="0 0 {out["w"]} {out["h"]}"><rect width="100%" height="100%" fill="#333"/>'
                           f'<text x="40" y="100" font-size="48" fill="#ffcc00">NOT RENDERED — source media missing</text>{lines}</svg>')
        status, path, detail = "edl_only", str(preview), "source media missing: EDL + placeholder preview only"
    else:
        has_audio = bool(asset.get("has_audio"))
        src = resolve_media(asset["media_path"])
        # One input per segment with input-side seeking: ffmpeg range-reads just those seconds,
        # so a multi-GB master on S3 is never downloaded or decoded from the start.
        inputs, parts, labels = [], [], []
        for i, s in enumerate(segs):
            inputs += ["-ss", f"{s['source_in']:.3f}", "-t", f"{s['source_out'] - s['source_in']:.3f}", "-i", src]
            parts.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
            labels.append(f"[v{i}]")
            if has_audio:
                parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
                labels.append(f"[a{i}]")
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a={1 if has_audio else 0}" + ("[cv][ca]" if has_audio else "[cv]"))
        sw, sh = asset.get("width"), asset.get("height")
        fx, framing = 0.5, "source already matches target orientation"
        if out["h"] > out["w"] and not (sw and sh and sh > sw):  # landscape (or unknown) master → vertical
            fx, framing = subject_x(src, segs)
        chain = _reframe(out, sw or out["w"], sh or out["h"], fx)
        draws = []
        if font:
            for j, o in enumerate(v.get("onscreen_text") or []):
                tf = outdir / f"overlay_{j}.txt"
                tf.write_text(_wrap(o["text"], 22 if out["w"] <= 1080 else 40))
                small = o.get("position", "").endswith("small")
                y = {"upper_third": "h*0.18", "center": "(h-text_h)/2", "top": "h*0.06",
                     "lower_third": "h*0.70", "lower_left_small": "h*0.90"}.get(o.get("position"), "h*0.15")
                x = "w*0.05" if small else "(w-text_w)/2"
                draws.append(f"drawtext=fontfile={font}:textfile={tf}:fontsize={34 if small else 64}:fontcolor=white:"
                             f"borderw=4:bordercolor=black:line_spacing=10:x={x}:y={y}:"
                             f"enable='between(t,{o['start']},{o['end']})'")
            for k, c in enumerate(build_caption_track(v, asset)):
                tf = outdir / f"cap_{k}.txt"
                tf.write_text(_wrap(c["text"], 28))
                draws.append(f"drawtext=fontfile={font}:textfile={tf}:fontsize=48:fontcolor=yellow:borderw=3:"
                             f"bordercolor=black:x=(w-text_w)/2:y=h*0.78:enable='between(t,{c['start']:.2f},{c['end']:.2f})'")
        parts.append(f"[cv]{chain}{(',' + ','.join(draws)) if draws else ''}[ov]")
        video = outdir / "video.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
               "-filter_complex", ";".join(parts), "-map", "[ov]"]
        if has_audio:
            cmd += ["-map", "[ca]", "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            status, path, detail = "render_failed", "", proc.stderr[-800:]
        else:
            status, path, detail = "rendered", str(video), f"ok; {framing}"
            cues = build_caption_track(v, asset)
            if cues:
                (outdir / "captions.srt").write_text("\n".join(
                    f"{i + 1}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n" for i, c in enumerate(cues)))
            t = v["thumbnail"].get("source_time", 0)
            thumb = outdir / "thumbnail.jpg"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", src,
                            "-frames:v", "1", "-vf", _reframe(out, asset["width"], asset["height"], fx), str(thumb)],
                           capture_output=True, timeout=120)
    if status == "rendered" and os.environ.get("PLN_RENDER_BUCKET"):
        path = upload_render(outdir, variant_id, os.environ["PLN_RENDER_BUCKET"])
    # a new render invalidates any earlier public link (the file it points at is the old cut)
    db.update("variants", "variant_id", variant_id, {"render_status": status, "render_path": path,
                                                     "public_media_url": "", "updated_at": now_iso()})
    db.audit(actor, "variant_rendered", "variants", variant_id, {"status": status, "detail": detail[:300]})
    return {"variant_id": variant_id, "status": status, "path": path, "detail": detail, "dir": str(outdir)}


def probe_output(path: str) -> dict:
    from .ingest import ffprobe, summarize_probe
    return summarize_probe(ffprobe(Path(path)))
