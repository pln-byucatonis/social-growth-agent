"""Carousels and Stories: post types beyond the Reel, made from a Reel post's own moments.

* A carousel is 2–10 still slides at 4:5 (1080x1350): the setup with the hook, the payoff with its word, the
  reaction, then a closing card with the call to action. Landscape footage is laid out stacked (the whole 16:9 frame
  across the slide over a blurred fill) so broadcast graphics are never cut in half and the text has its own space.
* A Story is one 9:16 item: the Reel's finished video when it has one, otherwise a stacked still with the hook.

Both are new posts derived from a Reel (`<reel id>~carousel`, `<reel id>~story`), reviewed and approved like any
other, so a carousel can be tested against the Reel it came from. They render on the task runner like videos.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .db import DB, now_iso

KINDS = {
    "carousel": {"w": 1080, "h": 1350, "format": "Instagram carousel 4:5 (setup / payoff / reaction / call to action)",
                 "min": 2, "max": 10},
    "story": {"w": 1080, "h": 1920, "format": "Instagram Story 9:16", "min": 1, "max": 1},
}
SLIDE_TYPES = tuple(KINDS)
PLATFORMS = ("instagram",)  # Facebook multi-photo, X images and TikTok photo posts follow
COPY = ("asset_id", "account_id", "platform", "brand_id", "route_role", "series", "hook", "hook_candidates", "title",
        "caption", "hashtags", "hashtag_rationale", "onscreen_text", "edl", "thumbnail", "cta", "angle", "utm_url",
        "score", "score_breakdown", "risk_flags", "pack_id")


class CarouselError(Exception):
    pass


def is_slides(v: dict | None) -> bool:
    return bool(v) and (v.get("post_type") or "") in SLIDE_TYPES


def _id(reel_id: str, kind: str) -> str:
    return f"{reel_id}~{kind}"


def make(db: DB, reel_id: str, kind: str, actor: str) -> dict:
    """Create (once) the carousel or Story version of a Reel post and queue its render."""
    from .workflow import caption_hash, refresh_validation
    if kind not in KINDS:
        raise CarouselError(f"unknown post type {kind!r}")
    reel = db.get("variants", "variant_id", reel_id)
    if not reel:
        raise CarouselError(f"unknown post {reel_id}")
    if is_slides(reel):
        raise CarouselError("make it from the Reel, not from another carousel or Story")
    if reel["platform"] not in PLATFORMS:
        raise CarouselError(f"{kind}s publish on Instagram only for now")
    vid = _id(reel_id, kind)
    if db.get("variants", "variant_id", vid):
        return {"variant_id": vid, "created": False}
    caption = reel["caption"] or ""
    if kind == "carousel":
        caption = caption.rstrip() + "\n\nSwipe for the finish."
    row = {k: reel.get(k) for k in COPY}
    row.update({
        "variant_id": vid, "post_type": kind, "format": KINDS[kind]["format"], "caption": caption,
        "caption_hash": caption_hash(caption), "state": "in_review", "render_status": "queued", "render_path": "",
        "public_media_url": "", "render_qc": {}, "slides": [], "voiceover": "",
        "treatment": {**(reel.get("treatment") or {}), "parent": reel_id, "post_type": kind},
        "test_hypothesis": f"{kind} vs the Reel it came from: follows and shares per 1,000 views",
        "validation": {"errors": [], "spec": {}}, "created_at": now_iso(), "updated_at": now_iso(),
    })
    db.insert("variants", row)
    refresh_validation(db, vid)
    db.audit(actor, f"{kind}_created", "variants", vid, {"from": reel_id})
    return {"variant_id": vid, "created": True}


# ---- slides ---------------------------------------------------------------------------------------

def slide_times(v: dict) -> list[float]:
    """Source times for setup / payoff / reaction: the strategist's picks, else spread across the cut."""
    picks = [float(t) for t in ((v.get("thumbnail") or {}).get("carousel_frames") or []) if t is not None]
    if len(picks) >= 2:
        return picks[:3]
    segs = (v.get("edl") or {}).get("segments") or []
    if not segs:
        return []
    a, b = segs[0]["source_in"], segs[-1]["source_out"]
    span = max(b - a, 0.6)
    return [round(a + span * f, 2) for f in (0.15, 0.6, 0.92)]


def _words(v: dict) -> tuple[str, str, str]:
    from . import overlays
    s = overlays.style_for(v["brand_id"])
    w = overlays.words_for(s)
    hook = ((v.get("onscreen_text") or [{}])[0].get("text") or v.get("hook") or "").strip()
    if overlays.is_generic(hook):
        hook = overlays._pick(w.get("hook") or ["WATCH THIS"], v["variant_id"])
    payoff = overlays._pick(w.get("payoff") or ["NO WAY."], v["variant_id"] + ":payoff")
    reaction = overlays._pick(w.get("reaction") or ["UNREAL."], v["variant_id"] + ":reaction")
    return hook, payoff, reaction


def _frame(src: str, t: float, out: Path) -> Path | None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src,
                    "-frames:v", "1", str(out)], capture_output=True, timeout=120)
    return out if out.exists() else None


def compose(frame_path: Path, w: int, h: int):
    """The frame on a w x h slide. Landscape → stacked: the whole frame across the slide over a darkened, blurred fill
    (nothing cropped, room above and below for text). Portrait → filled. Returns (image, (y0, y1) of the frame)."""
    from PIL import Image, ImageEnhance, ImageFilter
    im = Image.open(frame_path).convert("RGB")
    fw, fh = im.size
    cover = max(w / fw, h / fh)
    bg = im.resize((int(fw * cover) + 1, int(fh * cover) + 1)).crop((0, 0, w, h))
    if fw <= fh * 0.9:  # portrait or square-ish source: fill the slide
        x0 = (bg.width - w) // 2
        return bg.crop((x0, 0, x0 + w, h)) if bg.width > w else bg, (0, h)
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(28))).enhance(0.45)
    fg = im.resize((w, int(fh * w / fw)))
    y0 = int(h * 0.40) if h < w * 1.5 else (h - fg.height) // 2  # carousel: hook band above; Story: centred
    y0 = max(0, min(y0, h - fg.height - int(h * 0.12)))
    bg.paste(fg, (0, y0))
    return bg, (y0, y0 + fg.height)


def _layer(img, png: Path):
    from PIL import Image
    layer = Image.open(png).convert("RGBA")
    base = img.convert("RGBA")
    base.alpha_composite(layer)
    return base.convert("RGB")


def _cta_card(text: str, s: dict, w: int, h: int, out: Path) -> Path:
    from PIL import Image, ImageDraw
    from . import overlays
    img = Image.new("RGB", (w, h), overlays._rgba(s["primary"])[:3])
    d = ImageDraw.Draw(img)
    f, lines = overlays._fit(text.upper(), s["font_path"], int(w * 0.82), int(w * 0.12), 48, max_lines=4)
    lh, block = overlays._text_block(lines, f)
    y = (h - block) // 2
    for ln in lines:
        d.text(((w - f.getlength(ln)) / 2, y), ln, font=f, fill=overlays._rgba(s["text"]))
        y += int(lh * 1.08)
    d.rectangle([int(w * 0.38), y + int(lh * 0.4), int(w * 0.62), y + int(lh * 0.4) + max(8, h // 160)],
                fill=overlays._rgba(s["accent"]))
    img.save(out, "JPEG", quality=92)
    return out


def _cta_text(v: dict) -> str:
    cta = (v.get("cta") or "").strip()
    first = cta.split(",")[0].split(" and ")[0].strip().rstrip(".")
    return first if 6 <= len(first) <= 60 else "Follow for the next one"


STAMP_CY = {"lower": 0.67}  # where the payoff / reaction word sits on a filled slide


def build_slides(v: dict, src: str, kind: str, outdir: Path, llm=None) -> list[dict]:
    """Draw the slides for a carousel (or a still Story) into outdir. Returns [{kind, file, role, text, t, placed}].

    Before a word goes on a filled slide, Claude looks at the slides for graphics already in the picture (a score bug,
    a "HOLE IN ONE" card): when there is one, our word is dropped, as on the Reels. Stacked slides put the word in the
    band under the frame, which never covers the footage."""
    from . import art_director, overlays
    spec = KINDS[kind]
    w, h = spec["w"], spec["h"]
    s = overlays.style_for(v["brand_id"])
    if s["hook"] == "knockout":  # cut-out letters need moving footage behind them; on a still they read as noise
        s = {**s, "hook": "pill"}
    hook, payoff, reaction = _words(v)
    times = slide_times(v)[: (1 if kind == "story" else 3)]
    roles = [("setup", hook), ("payoff", payoff), ("reaction", reaction)]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        drawn = []
        for i, t in enumerate(times):
            fr = _frame(src, t, tmp / f"f{i}.png")
            if fr:
                img, band = compose(fr, w, h)
                drawn.append((i, t, img, band))
        filled = {float(i): img for i, _, img, band in drawn if band == (0, h)}
        seen = art_director.look(filled, llm) if filled else {}
        for i, t, img, (y0, y1) in drawn:
            role, text = roles[i]
            png, placed = tmp / f"t{i}.png", ""
            if role == "setup":
                overlays.render_hook(text, s, w, h, png, "top")
                placed = "top"
            elif y1 < h * 0.92:  # stacked: the band under the frame
                overlays.render_stamp(text, s, w, h, png, tilt=-5 if role == "payoff" else 4, cy=min(0.9, ((y1 + h) / 2) / h),
                                      heavy=role == "payoff")
                placed = "under the frame"
            else:
                busy = seen.get(round(float(i), 2), set())
                if busy:  # the footage celebrates the moment itself (a "HOLE IN ONE" card): ours would compete with it
                    text, placed = "", f"dropped: the picture has its own graphic ({', '.join(sorted(busy))})"
                else:
                    overlays.render_stamp(text, s, w, h, png, tilt=-5 if role == "payoff" else 4, cy=STAMP_CY["lower"],
                                          heavy=role == "payoff")
                    placed = "lower"
            if placed and not placed.startswith("dropped"):
                img = _layer(img, png)
            f = outdir / f"slide_{len(out) + 1}.jpg"
            img.save(f, "JPEG", quality=92)
            out.append({"kind": "image", "file": str(f), "role": role, "text": text, "t": t, "placed": placed})
        if kind == "carousel" and out:
            f = _cta_card(_cta_text(v), s, w, h, outdir / f"slide_{len(out) + 1}.jpg")
            out.append({"kind": "image", "file": str(f), "role": "cta", "text": _cta_text(v), "t": None, "placed": "card"})
    return out


def qc_slides(slides: list[dict], kind: str) -> dict:
    """Instagram's rules for carousel / Story items: count, JPEG, size, under 8 MB."""
    from PIL import Image
    spec = KINDS[kind]
    errors, warnings = [], []
    if not spec["min"] <= len(slides) <= spec["max"]:
        errors.append(f"{len(slides)} item(s); a {kind} needs {spec['min']}–{spec['max']}")
    for i, sl in enumerate(slides, 1):
        if sl["kind"] != "image":
            continue
        p = Path(sl["file"])
        if not p.exists():
            errors.append(f"slide {i} missing")
            continue
        with Image.open(p) as im:
            if im.size != (spec["w"], spec["h"]):
                errors.append(f"slide {i} is {im.size[0]}x{im.size[1]}, needs {spec['w']}x{spec['h']}")
            if im.format != "JPEG":
                errors.append(f"slide {i} is {im.format}, Instagram needs JPEG")
        if p.stat().st_size > 8 * 1024 * 1024:
            errors.append(f"slide {i} is over 8 MB")
    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "summary": "pass" if not errors else f"{len(errors)} error(s)",
            "measures": {"items": len(slides), "size": f"{spec['w']}x{spec['h']}"}}


def _upload(slides: list[dict], variant_id: str, bucket: str) -> list[dict]:
    import boto3
    from .media import _safe
    s3 = boto3.client("s3")
    prefix = f"social-agent/renders/{_safe(variant_id)}"
    out = []
    for sl in slides:
        if sl["kind"] == "image" and not sl["file"].startswith("s3://"):
            key = f"{prefix}/{Path(sl['file']).name}"
            s3.upload_file(sl["file"], bucket, key, ExtraArgs={"ContentType": "image/jpeg"})
            sl = {**sl, "file": f"s3://{bucket}/{key}"}
        out.append(sl)
    return out


def render_slides(db: DB, variant_id: str, settings, actor: str = "renderer") -> dict:
    from .media import _safe, resolve_media
    v = db.get("variants", "variant_id", variant_id)
    kind = v["post_type"]
    asset = db.get("assets", "asset_id", v["asset_id"])
    outdir = settings.render_dir / _safe(variant_id)
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    parent = db.get("variants", "variant_id", (v.get("treatment") or {}).get("parent") or "") or {}
    slides, detail = [], ""
    if kind == "story" and parent.get("render_status") == "rendered" and parent.get("render_path"):
        slides, detail = [{"kind": "video", "file": parent["render_path"], "role": "story", "text": "", "t": None}], \
            "the Reel's finished video as a Story"
    elif not (asset.get("media_present") and shutil.which("ffmpeg")):
        status = "edl_only"
        db.update("variants", "variant_id", variant_id, {"render_status": status, "slides": [], "render_qc": {},
                                                         "updated_at": now_iso()})
        return {"variant_id": variant_id, "status": status, "detail": "source media missing"}
    else:
        slides = build_slides(v, resolve_media(asset["media_path"]), kind, outdir)
        detail = f"{len(slides)} slide(s)"
    qc = qc_slides(slides, kind)
    status = "rendered" if slides else "render_failed"  # a failed check stays visible and blocks publishing
    if os.environ.get("PLN_RENDER_BUCKET") and slides:
        slides = _upload(slides, variant_id, os.environ["PLN_RENDER_BUCKET"])
    path = slides[0]["file"] if slides else ""
    db.update("variants", "variant_id", variant_id, {"render_status": status, "render_path": path, "slides": slides,
                                                     "render_qc": qc, "public_media_url": "", "updated_at": now_iso()})
    db.audit(actor, "slides_rendered", "variants", variant_id, {"status": status, "detail": detail, "qc": qc["summary"]})
    return {"variant_id": variant_id, "status": status, "detail": detail, "qc": qc, "slides": slides}


def preview_urls(v: dict) -> list[dict]:
    """Browser URLs for every item, for the post page and the run sheet."""
    from .media import resolve_media
    out = []
    for sl in v.get("slides") or []:
        f = sl["file"]
        url = resolve_media(f, "video/mp4" if sl["kind"] == "video" else "image/jpeg") if f.startswith("s3://") else ""
        out.append({**sl, "url": url})
    return out


def public_links(v: dict, bucket: str, base_url: str, copier) -> list[dict]:
    """Copy every item of an approved carousel / Story to the public bucket (Instagram pulls each by URL)."""
    import re
    import secrets
    out = []
    slug = re.sub(r"[^a-z0-9]+", "-", f"{v['brand_id']}-{v['platform']}-{v['post_type']}".lower()).strip("-")
    token = secrets.token_urlsafe(18)
    for i, sl in enumerate(v.get("slides") or [], 1):
        if not sl["file"].startswith("s3://"):
            raise CarouselError("slides are not stored yet: render them first")
        src_bucket, _, src_key = sl["file"][5:].partition("/")
        ext, ctype = (".mp4", "video/mp4") if sl["kind"] == "video" else (".jpg", "image/jpeg")
        key = f"v/{token}/{slug}-{i}{ext}"
        copier(src_bucket, src_key, bucket, key, ctype)
        out.append({**sl, "public_url": base_url.rstrip("/") + "/" + key})
    return out

