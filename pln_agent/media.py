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


def _on_ops_vertical(asset: dict, v: dict, cut: dict, settings: Settings) -> tuple[dict, dict]:
    """ops already made a subject-tracked 9:16 of exactly this cut's window: use it as the source (times from 0),
    shift captions to match, and don't crop again."""
    ov = cut["ops_vertical"]
    start, end = ov["window"]
    bucket = os.environ.get("PLN_OPS_ASSETS_BUCKET", settings.ops_assets_bucket)
    asset = {**asset, "media_path": f"s3://{bucket}/{ov['key']}", "media_present": True, "width": 1080, "height": 1920,
             "transcript": [{**c, "start": c["start"] - start, "end": c["end"] - start}
                            for c in (asset.get("transcript") or []) if c["end"] > start and c["start"] < end],
             "moments": [{**m, "start": m["start"] - start, "end": m["end"] - start}
                         for m in (asset.get("moments") or []) if m["end"] > start and m["start"] < end]}
    seg = dict(cut["segments"][0])
    seg.update({"source_in": 0.0, "source_out": round(end - start, 2)})
    v = {**v, "edl": {**(v.get("edl") or {}), "segments": [seg]},
         "thumbnail": {**(v.get("thumbnail") or {}), "source_time": max(0.0, cut.get("payoff_at_s") or 0)}}
    return asset, v


def render_variant(db: DB, variant_id: str, settings: Settings, actor: str = "renderer", cut: dict | None = None) -> dict:
    """Render the post's EDL — or, with `cut` (a Creative Director duration variant), that cut into its own
    folder without touching the post's main render. Every render gets quality checks."""
    v = db.get("variants", "variant_id", variant_id)
    asset = db.get("assets", "asset_id", v["asset_id"])
    outdir = settings.render_dir / _safe(variant_id) / (f"cut_{cut['label']}" if cut else "")
    if not cut:
        # the post's own render: when it is on a Creative Director cut that MediaConvert already cropped, use that
        # 9:16 (same window) instead of the agent's crop
        label = (v.get("edl") or {}).get("variant_label")
        mine = next((c for c in (v.get("treatment") or {}).get("duration_variants") or [] if c["label"] == label), None)
        if mine and (mine.get("ops_vertical") or {}).get("status") == "ready" and mine.get("segments") == (v.get("edl") or {}).get("segments"):
            asset, v = _on_ops_vertical(asset, v, mine, settings)
    if cut:
        v = {**v, "edl": {**(v.get("edl") or {}), "segments": cut["segments"]}}
        if (cut.get("ops_vertical") or {}).get("status") == "ready":
            asset, v = _on_ops_vertical(asset, v, cut, settings)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "edl.json").write_text(json.dumps({"variant_id": variant_id, "source": asset["media_path"],
                                                 "source_sha256": asset["sha256"], **v["edl"]}, indent=2))
    segs = v["edl"].get("segments", [])
    out = v["edl"].get("output")
    font = _font()
    overlay_style, overlay_words = "", {}
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
        final, styled = "ov", None
        try:  # designed overlays (knockout / pill / kinetic captions / payoff stamp / countdown)
            from . import overlays
            label = (v.get("edl") or {}).get("variant_label")
            dv = cut or next((c for c in (v.get("treatment") or {}).get("duration_variants") or [] if c["label"] == label), None)
            payoff = (dv or {}).get("payoff_at_s")
            beats, t_out = [], 0.0
            for sg in segs:  # the clip's moments mapped onto the edited timeline
                for m in asset.get("moments") or []:
                    if m.get("kind") != "setup" and sg["source_in"] <= m.get("start", -1) < sg["source_out"]:
                        beats.append({"kind": m.get("kind"), "t": t_out + m["start"] - sg["source_in"], "intensity": m.get("intensity") or 0})
                t_out += sg["source_out"] - sg["source_in"]
            hits = [b for b in beats if b["kind"] != "reaction"]
            if payoff is None and hits:  # not on a Creative Director cut: the strongest action moment in the edit
                payoff = max(hits, key=lambda b: b["intensity"])["t"]
            duration = sum(x["source_out"] - x["source_in"] for x in segs)
            styled = overlays.build(v, build_caption_track(v, asset), duration, out["w"], out["h"], outdir, payoff, beats)
            ins, ps, final = overlays.ffmpeg_graph("base", len(segs), styled["layers"], styled["countdown"], out["w"], out["h"])
            parts.append(f"[cv]{chain}[base]")
            parts += ps
            inputs += ins
            overlay_style = styled["style_id"]
            overlay_words = styled.get("words") or {}
        except Exception as e:  # noqa: BLE001 — fall back to plain drawtext rather than fail the render
            styled = None
            framing += f"; plain text overlays ({str(e)[:80]})"
        if styled is None:
            draws = []
            if font:
                for j, o in enumerate(v.get("onscreen_text") or []):
                    tf = outdir / f"overlay_{j}.txt"
                    tf.write_text(_wrap(o["text"], 22 if out["w"] <= 1080 else 40))
                    small = o.get("position", "").endswith("small")
                    y = {"upper_third": "h*0.18", "center": "(h-text_h)/2", "top": "h*0.13",
                         "lower_third": "h*0.62", "lower_left_small": "h*0.74"}.get(o.get("position"), "h*0.15")
                    x = "w*0.05" if small else "(w-text_w)/2"
                    draws.append(f"drawtext=fontfile={font}:textfile={tf}:fontsize={34 if small else 64}:fontcolor=white:"
                                 f"borderw=4:bordercolor=black:line_spacing=10:x={x}:y={y}:"
                                 f"enable='between(t,{o['start']},{o['end']})'")
                for k, c in enumerate(build_caption_track(v, asset)):
                    tf = outdir / f"cap_{k}.txt"
                    tf.write_text(_wrap(c["text"], 28))
                    draws.append(f"drawtext=fontfile={font}:textfile={tf}:fontsize=48:fontcolor=yellow:borderw=3:"
                                 f"bordercolor=black:x=(w-text_w)/2:y=h*0.66:enable='between(t,{c['start']:.2f},{c['end']:.2f})'")
            parts.append(f"[cv]{chain}{(',' + ','.join(draws)) if draws else ''}[ov]")
            final = "ov"
        if has_audio:  # keep peaks ~1.5 dB under full scale so platform re-encoding doesn't clip (QC: true peak <= -1 dBTP)
            parts.append("[ca]alimiter=limit=0.84:level=false[cal]")
        video = outdir / "video.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
               "-filter_complex", ";".join(parts), "-map", f"[{final}]"]
        if has_audio:
            cmd += ["-map", "[cal]", "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            status, path, detail = "render_failed", "", proc.stderr[-800:]
        else:
            status, path, detail = "rendered", str(video), f"ok; {framing}" + (
                "; ops subject-tracked vertical (MediaConvert Smart Crop)" if "/verticals/" in asset["media_path"] else "")
            cues = build_caption_track(v, asset)
            if cues:
                (outdir / "captions.srt").write_text("\n".join(
                    f"{i + 1}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n" for i, c in enumerate(cues)))
            t = v["thumbnail"].get("source_time", 0)
            thumb = outdir / "thumbnail.jpg"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t), "-i", src,
                            "-frames:v", "1", "-vf", _reframe(out, asset["width"], asset["height"], fx), str(thumb)],
                           capture_output=True, timeout=120)
    qc = qc_render(str(outdir / "video.mp4"), out, (v.get("validation") or {}).get("spec") or {}, v.get("platform", "")) \
        if status == "rendered" else {}
    if qc and overlay_style:
        qc["overlay_style"] = overlay_style  # recorded with the post so 'what works' can compare looks
        qc["overlay_words"] = overlay_words
    if status == "rendered" and os.environ.get("PLN_RENDER_BUCKET"):
        path = upload_render(outdir, variant_id + (f"/cut_{cut['label']}" if cut else ""), os.environ["PLN_RENDER_BUCKET"])
    if cut:
        db.audit(actor, "cut_rendered", "variants", variant_id, {"cut": cut["label"], "status": status, "qc": qc.get("summary")})
        return {"variant_id": variant_id, "cut": cut["label"], "status": status, "path": path, "detail": detail, "qc": qc}
    # a new render invalidates any earlier public link (the file it points at is the old cut)
    db.update("variants", "variant_id", variant_id, {"render_status": status, "render_path": path, "render_qc": qc,
                                                     "public_media_url": "", "updated_at": now_iso()})
    db.audit(actor, "variant_rendered", "variants", variant_id, {"status": status, "detail": detail[:300],
                                                                  "qc": qc.get("summary")})
    return {"variant_id": variant_id, "status": status, "path": path, "detail": detail, "dir": str(outdir), "qc": qc}


def render_queued(db: DB, settings: Settings, limit: int = 12, actor: str = "renderer") -> list[dict]:
    """Render posts queued from the dashboard (Re-render, on-screen text / style edits). Runs on the task runner:
    App Runner throttles CPU between requests, so renders there stall."""
    out = []
    for v in db.all("variants", "render_status='queued'", order="updated_at")[:limit]:
        db.update("variants", "variant_id", v["variant_id"], {"render_status": "rendering"})
        try:
            r = render_variant(db, v["variant_id"], settings, actor=actor)
            out.append({"variant_id": v["variant_id"], "status": r["status"], "qc": (r.get("qc") or {}).get("passed")})
        except Exception as e:  # noqa: BLE001 — never leave a post stuck in "rendering"
            db.update("variants", "variant_id", v["variant_id"], {"render_status": "render_failed"})
            db.audit(actor, "variant_render_failed", "variants", v["variant_id"], {"error": str(e)[:300]})
            out.append({"variant_id": v["variant_id"], "status": "render_failed"})
    return out


def render_cuts(db: DB, variant_id: str, settings: Settings, actor: str = "renderer", only: list[str] | None = None) -> list[dict]:
    """Video Production: render every available Creative Director cut (A ≈12s / B ≈25s / C ≈40s) so they can
    be compared and tested. Results (path + QC) are stored on the treatment's duration variants."""
    v = db.get("variants", "variant_id", variant_id)
    t = dict(v.get("treatment") or {})
    results = []
    cuts = []
    for c in t.get("duration_variants") or []:
        c = dict(c)
        if c.get("available") and c.get("segments") and (only is None or c["label"] in only):
            r = render_variant(db, variant_id, settings, actor, cut=c)
            c["render"] = {"status": r["status"], "path": r["path"], "qc": r.get("qc", {}), "at": now_iso()}
            results.append(r)
        cuts.append(c)
    if cuts:
        t["duration_variants"] = cuts
        db.update("variants", "variant_id", variant_id, {"treatment": t, "updated_at": now_iso()})
    return results


# ---- quality control ---------------------------------------------------------------------------------

SAFE_ZONE = {"top": 0.12, "bottom": 0.20}  # share of a 9:16 frame covered by platform UI (handles, captions, buttons)
LOUDNESS_TARGET = (-20.0, -8.0)  # integrated LUFS acceptable range for social (platforms normalise near -14)


def _run(cmd: list[str], timeout: int = 300) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stderr or ""
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"QC_ERROR {e}"


def qc_render(path: str, out: dict | None, spec: dict, platform: str = "") -> dict:
    """Checks a finished render the way a producer would: duration vs the platform's limits, output size,
    black frames (especially a black opening), long silences, loudness, and text inside UI safe zones."""
    errors, warnings, measures = [], [], {}
    from .ingest import ffprobe
    try:
        pr = ffprobe(Path(path))
    except Exception as e:  # noqa: BLE001
        return {"passed": False, "errors": [f"can't read the render: {e}"], "warnings": [], "measures": {},
                "summary": "unreadable"}
    fmt = pr.get("format", {})
    vstreams = [x for x in pr.get("streams", []) if x.get("codec_type") == "video"]
    astreams = [x for x in pr.get("streams", []) if x.get("codec_type") == "audio"]
    dur = float(fmt.get("duration") or 0)
    measures["duration_s"] = round(dur, 2)
    if spec.get("max_s") and dur > spec["max_s"] + 0.5:
        errors.append(f"{dur:.1f}s is longer than {platform or 'the platform'}'s {spec['max_s']}s limit for this format")
    if spec.get("min_s") and dur < spec["min_s"] - 0.5:
        warnings.append(f"{dur:.1f}s is shorter than the {spec['min_s']}s minimum we aim for")
    if vstreams and out:
        w, h = vstreams[0].get("width"), vstreams[0].get("height")
        measures["size"] = f"{w}x{h}"
        if (w, h) != (out.get("w"), out.get("h")):
            errors.append(f"output is {w}x{h}, expected {out.get('w')}x{out.get('h')}")
    log = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", "blackdetect=d=0.4:pix_th=0.10",
                "-af", "silencedetect=n=-50dB:d=2,ebur128=peak=true", "-f", "null", "-"])
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    measures["black_segments"] = len(blacks)
    if any(a < 0.3 for a, _ in blacks):
        errors.append("opens on black — the first frame must be the action")
    long_black = [b - a for a, b in blacks if b - a >= 1.0]
    if long_black:
        warnings.append(f"{len(long_black)} black stretch(es) of {max(long_black):.1f}s+ inside the cut")
    silences = re.findall(r"silence_duration: ([\d.]+)", log)
    if astreams and silences:
        warnings.append(f"{len(silences)} silence(s) of 2s+ (longest {max(float(x) for x in silences):.1f}s)")
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    if astreams and lufs:
        i_lufs = float(lufs[-1])
        measures["loudness_lufs"] = i_lufs
        if i_lufs < LOUDNESS_TARGET[0]:
            warnings.append(f"quiet ({i_lufs:.1f} LUFS; aim for about -14)")
        elif i_lufs > LOUDNESS_TARGET[1]:
            warnings.append(f"very loud ({i_lufs:.1f} LUFS; aim for about -14)")
        peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", log)
        if peak and float(peak[-1]) > -1.0:
            warnings.append(f"true peak {float(peak[-1]):.1f} dBFS may clip after platform encoding")
    elif not astreams:
        warnings.append("no audio track")
    if "QC_ERROR" in log:
        warnings.append("ffmpeg analysis didn't complete")
    passed = not errors
    return {"passed": passed, "errors": errors, "warnings": warnings, "measures": measures,
            "summary": ("pass" if passed else "fail") + (f" · {len(warnings)} warning(s)" if warnings else "")}


def probe_output(path: str) -> dict:
    from .ingest import ffprobe, summarize_probe
    return summarize_probe(ffprobe(Path(path)))
