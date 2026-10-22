"""Designed on-screen text: each overlay is drawn as a transparent full-frame PNG (Pillow) and FFmpeg lays it
over the video for its time window. FFmpeg's drawtext can't cut letters out of a band or animate word by word;
this can.

Styles (config/overlay_styles.json, per brand; a post's treatment may override):
* hook      knockout — a solid brand-colour band with the letters cut out, so the footage shows through them
            pill     — text on a rounded brand-colour block
            outline  — heavy white type, thick dark stroke and a soft shadow
* captions  kinetic  — 2–4 word phrases, the spoken word lit in the accent colour as it's said
            classic  — the whole cue in white with a stroke
* stamp     a bold word ("TAP.", "ACE.") slammed onto the payoff; a second one on the reaction
* countdown a thin bar filling up to the payoff, under the hook

How it's applied (the Creative Director's rules):
* placement follows the frames: the clean render is analysed first (art_director) and text moves off broadcast
  graphics, score bugs and sudden animations — or is dropped, with the reason recorded in the plan
* motion: every element eases in (scale overshoot over four frames) and fades out over the last 0.15 s
* timing: nothing overlaps, every word holds ≥0.7 s, nothing new in the last 0.5 s, captions step aside while a
  stamp is up
* contrast: on bright backgrounds stamps get a heavier stroke and a dark glow
Everything sits inside the 9:16 safe zone (clear of the top 12% and bottom 20% platform UI).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CONFIG_DIR, ROOT

SAFE_TOP, SAFE_BOTTOM = 0.12, 0.20
FPS = 30
EASE_IN = [0.72, 1.12, 1.04, 0.99]  # scale per frame on entry (ease-out-back), then 1.0
FADE_OUT = [0.6, 0.3]  # alpha per step over the last 0.15 s
MIN_HOLD, END_GUARD = 0.7, 0.5
ZONE_TOP = {"top": SAFE_TOP + 0.03, "middle": 0.37, "lower": 0.58}  # where a hook block starts (share of height)
ZONE_CENTRE = {"top": 0.24, "middle": 0.44, "lower": 0.66}  # where a stamp is centred


def config() -> dict:
    return json.loads((CONFIG_DIR / "overlay_styles.json").read_text())


def style_for(brand_id: str, override: dict | None = None) -> dict:
    c = config()
    s = {**c["default"], **c["brands"].get(brand_id or "", {}), **(override or {})}
    s["font_path"] = str(ROOT / c["fonts"].get(s["font"], c["fonts"]["anton"]))
    return s


def style_id(s: dict) -> str:
    """Compact label recorded on the post so 'what works' can compare looks."""
    return (f"{s['hook']}+{s['captions']}" + ("+stamp" if s.get("stamp") != "none" else "")
            + ("+reaction" if s.get("reaction") else "") + ("+countdown" if s.get("countdown") else ""))


def _pick(options: list[str], seed: str) -> str:
    """Stable but varied choice: the same post always gets the same word, different posts get different ones."""
    import hashlib
    return options[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(options)] if options else ""


def words_for(s: dict) -> dict:
    w = config().get("words") or {}
    return w.get(s.get("family") or "default") or w.get("default") or {}


def _brand_terms() -> set[str]:
    from .accounts import load_brands
    out = set()
    for b in load_brands():
        out |= {b["name"].lower(), b["brand_id"].replace("_", " ").lower(), *[a.lower() for a in b.get("aliases") or []]}
    return {t for t in out if len(t) > 2}


def is_generic(text: str) -> bool:
    """Brand-name filler ("CarJitsu Championship: Highlight skill") — not a hook."""
    t = (text or "").lower()
    return not t.strip() or any(b in t for b in _brand_terms())


# ---- drawing -------------------------------------------------------------------------------------

def _rgba(hex_: str, a: int = 255) -> tuple[int, int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def _font(path: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def _fit(text: str, path: str, max_w: int, start: int, min_size: int = 40, max_lines: int = 2):
    """Largest size (and line breaks) at which the text fits max_w in at most max_lines lines."""
    words = text.split()
    for size in range(start, min_size - 1, -4):
        f = _font(path, size)
        lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if f.getlength(t) <= max_w or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
            return f, lines
    f = _font(path, min_size)
    return f, [text]


def _canvas(w: int, h: int):
    from PIL import Image
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _text_block(lines, f, line_gap: float = 0.08):
    asc, desc = f.getmetrics()
    lh = asc + desc
    return lh, int(lh * (1 + line_gap)) * len(lines) - int(lh * line_gap)


def render_hook(text: str, s: dict, w: int, h: int, out: Path, zone: str = "top") -> int:
    """Draw the hook; returns the y (px) of its bottom edge so other layers can sit under it."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    text = text.upper().strip()
    pad_x = int(w * 0.06)
    f, lines = _fit(text, s["font_path"], int(w * (0.72 if s["hook"] == "knockout" else 0.80)), int(w * 0.13))
    lh, block_h = _text_block(lines, f)
    top = int(h * ZONE_TOP.get(zone, ZONE_TOP["top"]))
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    step = int(lh * 1.08)
    if s["hook"] == "knockout":
        band_h = block_h + int(lh * 0.55)
        band = Image.new("RGBA", (w - 2 * pad_x, band_h), _rgba(s["primary"], 240))
        mask = Image.new("L", band.size, 0)
        md = ImageDraw.Draw(mask)
        y = (band_h - block_h) // 2
        for ln in lines:
            md.text(((band.width - f.getlength(ln)) / 2, y), ln, font=f, fill=255)
            y += step
        band.putalpha(ImageChops.subtract(band.getchannel("A"), mask))  # letters become holes: the footage shows through
        img.alpha_composite(band, (pad_x, top))
        bottom = top + band_h + max(6, h // 240)
        d.rectangle([pad_x, top + band_h, w - pad_x, bottom], fill=_rgba(s["accent"]))
    elif s["hook"] == "pill":
        widths = [f.getlength(ln) for ln in lines]
        bw, bh = int(max(widths) + lh * 0.9), block_h + int(lh * 0.5)
        x0 = (w - bw) // 2
        shadow = _canvas(w, h)
        ImageDraw.Draw(shadow).rounded_rectangle([x0 + 8, top + 12, x0 + bw + 8, top + bh + 12], radius=int(lh * 0.35),
                                                 fill=(0, 0, 0, 120))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
        d.rounded_rectangle([x0, top, x0 + bw, top + bh], radius=int(lh * 0.35), fill=_rgba(s["primary"]))
        bottom = top + bh
        y = top + (bh - block_h) // 2
        for ln in lines:
            d.text(((w - f.getlength(ln)) / 2, y), ln, font=f, fill=_rgba(s["text"]))
            y += step
    else:  # outline
        shadow = _canvas(w, h)
        sd, y = ImageDraw.Draw(shadow), top
        for ln in lines:
            sd.text(((w - f.getlength(ln)) / 2 + 6, y + 8), ln, font=f, fill=(0, 0, 0, 160))
            y += step
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
        y = top
        for ln in lines:
            d.text(((w - f.getlength(ln)) / 2, y), ln, font=f, fill=_rgba(s["text"]), stroke_width=max(4, lh // 14),
                   stroke_fill=_rgba(s["ink"]))
            y += step
        bottom = y
    img.save(out)
    return bottom


def render_stamp(word: str, s: dict, w: int, h: int, out: Path, tilt: float = -7, cy: float = 0.44,
                 heavy: bool = False, size: int | None = None) -> Path:
    """A slammed word centred at cy (share of height). heavy = bright background: thicker stroke + dark glow.
    size caps the type (px) when the word must fit a band, e.g. under a stacked frame."""
    from PIL import ImageDraw, ImageFilter
    img = _canvas(w, h)
    f, _ = _fit(word, s["font_path"], int(w * 0.84), min(int(w * 0.30), size or w), min(60, size or 60), max_lines=1)
    tw = f.getlength(word)
    asc, desc = f.getmetrics()
    x, y = (w - tw) / 2, h * cy - (asc + desc) / 2
    stroke = max(6, int(w * (0.018 if heavy else 0.012)))
    glow = _canvas(w, h)
    ImageDraw.Draw(glow).text((x + 4, y + 10), word, font=f, fill=(0, 0, 0, 200 if heavy else 130), stroke_width=stroke + 6,
                              stroke_fill=(0, 0, 0, 200 if heavy else 130))
    tmp = glow.filter(ImageFilter.GaussianBlur(14 if heavy else 9))
    ImageDraw.Draw(tmp).text((x, y), word, font=f, fill=_rgba(s["accent"]), stroke_width=stroke, stroke_fill=_rgba(s["ink"]))
    img.alpha_composite(tmp.rotate(tilt, center=(w / 2, h * cy)))
    img.save(out)
    return out


def phrases(cues: list[dict], max_words: int = 3) -> list[dict]:
    """Split caption cues into short phrases with per-word timing (evenly spread across the cue)."""
    out = []
    for c in cues:
        words = [x for x in re.split(r"\s+", (c.get("text") or "").strip()) if x]
        if not words:
            continue
        per = max(0.12, (c["end"] - c["start"]) / len(words))
        for i in range(0, len(words), max_words):
            chunk = words[i:i + max_words]
            start = c["start"] + i * per
            out.append({"words": chunk, "start": start, "times": [start + k * per for k in range(len(chunk))],
                        "end": min(c["end"], start + len(chunk) * per)})
    return out


def render_caption(words: list[str], lit: int | None, s: dict, w: int, h: int, out: Path, zone: str = "lower") -> Path:
    """One caption frame; `lit` is the index of the word being spoken (kinetic) or None (classic)."""
    from PIL import ImageDraw
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    text = " ".join(words).upper() if s["captions"] == "kinetic" else " ".join(words)
    f, lines = _fit(text, s["font_path"], int(w * 0.84), int(w * (0.085 if s["captions"] == "kinetic" else 0.06)), 34)
    lh, block_h = _text_block(lines, f)
    y = int(h * (1 - SAFE_BOTTOM) - block_h - h * 0.04) if zone == "lower" else int(h * 0.40)
    idx = 0
    for ln in lines:
        x = (w - f.getlength(ln)) / 2
        for k, word in enumerate(ln.split(" ")):
            colour = _rgba(s["accent"]) if (lit is not None and idx == lit) else _rgba(s["text"])
            d.text((x, y), word, font=f, fill=colour, stroke_width=max(4, lh // 12), stroke_fill=_rgba(s["ink"]))
            x += f.getlength(word + " ")
            idx += 1
        y += int(lh * 1.08)
    img.save(out)
    return out


def _transform(src: Path, out: Path, scale: float, alpha: float, cx: float, cy: float) -> Path:
    """The same layer scaled about (cx, cy) and/or faded: the frames of an entrance or exit."""
    from PIL import Image
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    if scale != 1.0:  # scale about (cx, cy): that point stays put, the layer grows or shrinks around it
        big = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(big, (int(cx * w * (1 - scale)), int(cy * h * (1 - scale))), big)
        img = canvas
    if alpha < 1.0:
        a = img.getchannel("A").point(lambda v: int(v * alpha))
        img.putalpha(a)
    img.save(out)
    return out


def _animate(layers: list, png: Path, t0: float, t1: float, cy: float, ease=EASE_IN) -> None:
    """Ease in (scale overshoot, one frame per step), hold, fade out over the last 0.15 s."""
    f = 1 / FPS
    t = t0
    for k, sc in enumerate(ease):
        if t + f >= t1:
            break
        layers.append({"png": _transform(png, png.with_name(f"{png.stem}_in{k}.png"), sc, 1.0, 0.5, cy), "start": t, "end": t + f})
        t += f
    fade_at = max(t, t1 - 0.15)
    layers.append({"png": png, "start": t, "end": fade_at})
    step = (t1 - fade_at) / len(FADE_OUT) if t1 > fade_at else 0
    for k, al in enumerate(FADE_OUT):
        if step <= 0:
            break
        layers.append({"png": _transform(png, png.with_name(f"{png.stem}_out{k}.png"), 1.0, al, 0.5, cy),
                       "start": fade_at + k * step, "end": fade_at + (k + 1) * step})


def build(v: dict, cues: list[dict], duration: float, w: int, h: int, outdir: Path, payoff_at: float | None,
          beats: list[dict] | None = None, analysis: dict | None = None, layout: dict | None = None, llm=None) -> dict:
    """All overlay layers for one render ([{png, start, end}]), an optional countdown bar, the words used and the
    plan (what went where and why). `beats` are the clip's moments on the edited timeline ({kind, t, intensity});
    `analysis` is the art director's read of the clean render (None = no frame checks)."""
    from . import art_director as ad
    ov = (v.get("treatment") or {}).get("overlay") or {}
    s = style_for(v.get("brand_id", ""), ov)
    bank, seed = words_for(s), v.get("variant_id", "")
    beats, a = beats or [], analysis or {}
    plan, words, busy = [], {}, []  # busy: (zone, start, end) already taken
    payoff_kind = next((b["kind"] for b in beats if payoff_at is not None and abs(b["t"] - payoff_at) < 0.6), "payoff")
    layers, hook_bottom, hook_zone = [], int(h * 0.20), None
    stacked = bool(layout and layout.get("mode") == "stack")
    band_cy = ((layout["y1"] + h * (1 - SAFE_BOTTOM)) / 2 / h) if stacked else None  # words sit in the band under the frame
    if stacked:
        plan.append({"what": "layout", "zone": "stacked", "at": "", "why": "the whole broadcast frame is kept; text sits in the bands"})

    def free(zone, t0, t1):
        return not any(z == zone and t0 < e and s0 < t1 for z, s0, e in busy)

    # 1. hook — the first 3 seconds; top unless the frames say otherwise
    for j, o in enumerate(v.get("onscreen_text") or []):
        text = (o.get("text") or "").strip()
        if (o.get("position") or "").endswith("small"):
            continue  # brand tags: cheap-looking, and the account already says who posted it
        if is_generic(text) and not ov.get("hook_custom"):
            text = _pick(bank.get("hook") or [], seed + ":hook")
        if not text or "hook" in words:
            continue
        t0, t1 = float(o.get("start", 0)), max(float(o.get("end", 3)), float(o.get("start", 0)) + 1.6)
        t1 = min(t1, duration - 0.2)
        if a.get("ok"):  # a hook that states a number must agree with the clock / score on screen
            bad, reason = ad.hook_contradicts(a, text, t0, t1, llm)
            if bad:
                old, text = text, _pick([x for x in bank.get("hook") or [] if not any(ch.isdigit() for ch in x)] or ["WATCH THIS"],
                                        seed + ":hook2")
                plan.append({"what": f"hook “{old}”", "zone": "—", "at": "", "why": f"replaced with “{text}”: {reason}"})
        if stacked:
            zone, why = "top", "the band above the frame"
        else:
            zone, why = ad.choose(a, ["top", "lower"], (t0, t1)) if a.get("ok") else ("top", "default position")
        if zone is None:
            zone, why = "top", why + " — kept at the top (a hook is essential)"
        p = outdir / f"ov_hook_{j}.png"
        hook_bottom, hook_zone = render_hook(text, s, w, h, p, zone), zone
        _animate(layers, p, t0, t1, ZONE_TOP[zone] + 0.06, ease=[0.9, 1.05, 1.0])
        busy.append((zone, t0, t1))
        words["hook"] = text
        plan.append({"what": f"hook “{text}”", "zone": zone, "at": f"{t0:.1f}–{t1:.1f}s", "why": why})

    # 2. payoff word
    stamp = (s.get("stamp") or "").strip()
    stamp_win = None
    if stamp != "none" and payoff_at is not None:
        stamp = stamp or _pick(bank.get("reversal" if payoff_kind == "reversal" else "payoff") or bank.get("payoff") or [],
                               seed + ":payoff")
        t0, t1 = payoff_at, min(payoff_at + 1.3, duration - END_GUARD)
        if not stamp:
            pass
        elif t1 - t0 < MIN_HOLD:
            plan.append({"what": f"payoff word “{stamp}”", "zone": "—", "at": f"{t0:.1f}s", "why": "dropped: too close to the end"})
        else:
            cands = ["lower"] if stacked else [z for z in ("middle", "top") if free(z, t0, t1)]
            if stacked:  # the word goes under the frame; still dropped when the frame celebrates the moment itself
                own = a.get("ok") and any(ad.zone_report(a, "middle", x)["spotted"] for x in (t0, (t0 + t1) / 2, t1))
                zone, why = (None, "frames show a graphic") if own else ("lower", "the band under the frame")
            else:
                zone, why = ad.choose(a, cands, (t0, t1)) if a.get("ok") else ((cands or [None])[0], "default position")
            if "frames show a graphic" in why:  # the broadcast already celebrates this moment: two graphics compete
                zone, why = None, "the video already has its own graphic for this moment (" + why.split(";")[0] + ")"
            if zone is None:
                plan.append({"what": f"payoff word “{stamp}”", "zone": "—", "at": f"{t0:.1f}s", "why": f"dropped: {why}"})
            else:
                heavy = (not stacked) and a.get("ok") and ad.light_background(a, zone, t0)
                p = outdir / "ov_stamp.png"
                cy = band_cy if stacked else ZONE_CENTRE[zone]
                render_stamp(stamp, s, w, h, p, -4 if stacked else -7, cy, heavy,
                             size=int((h * (1 - SAFE_BOTTOM) - layout["y1"]) * 0.75) if stacked else None)
                _animate(layers, p, t0, t1, cy)
                busy.append((zone, t0, t1))
                stamp_win = (t0, t1)
                words["payoff"] = stamp
                plan.append({"what": f"payoff word “{stamp}”", "zone": zone, "at": f"{t0:.1f}–{t1:.1f}s",
                             "why": why + ("; heavier stroke on a bright background" if heavy else "")})

    # 3. reaction word — only with a real reaction beat, clear of the payoff word
    if s.get("reaction") and payoff_at is not None:
        after = (stamp_win[1] + 0.4) if stamp_win else payoff_at + 1.4
        r = next((b for b in beats if b["kind"] == "reaction" and b["t"] >= after - 0.4), None)
        word = _pick(bank.get("reaction") or [], seed + ":reaction") if r else ""
        if word:
            t0 = max(r["t"], after)
            t1 = min(t0 + 1.2, duration - END_GUARD)
            if t1 - t0 < MIN_HOLD:
                plan.append({"what": f"reaction word “{word}”", "zone": "—", "at": f"{t0:.1f}s", "why": "dropped: too close to the end"})
            else:
                if stacked:
                    own = a.get("ok") and any(ad.zone_report(a, "middle", x)["spotted"] for x in (t0, (t0 + t1) / 2, t1))
                    zone, why = (None, "frames show a graphic") if own else (("lower", "the band under the frame")
                                                                             if free("lower", t0, t1) else (None, "band busy"))
                else:
                    cands = [z for z in ("middle", "top", "lower") if free(z, t0, t1)]
                    zone, why = ad.choose(a, cands, (t0, t1)) if a.get("ok") else ((cands or [None])[0], "default position")
                if "frames show a graphic" in why:
                    zone, why = None, "the video already has its own graphic here (" + why.split(";")[0] + ")"
                if zone is None:
                    plan.append({"what": f"reaction word “{word}”", "zone": "—", "at": f"{t0:.1f}s", "why": f"dropped: {why}"})
                else:
                    heavy = (not stacked) and a.get("ok") and ad.light_background(a, zone, t0)
                    p = outdir / "ov_reaction.png"
                    cy = band_cy if stacked else ZONE_CENTRE[zone]
                    render_stamp(word, {**s, "accent": s["text"]}, w, h, p, 4 if stacked else 6, cy, heavy,
                                 size=int((h * (1 - SAFE_BOTTOM) - layout["y1"]) * 0.75) if stacked else None)
                    _animate(layers, p, t0, t1, cy)
                    busy.append((zone, t0, t1))
                    words["reaction"] = word
                    plan.append({"what": f"reaction word “{word}”", "zone": zone, "at": f"{t0:.1f}–{t1:.1f}s", "why": why})

    # 4. captions — lower third, stepping aside while a word or the hook occupies it
    if s["captions"] in ("kinetic", "classic") and cues:
        cap_zone = "lower"
        if stacked:
            pass  # the band under the frame is ours: nothing to avoid
        elif a.get("ok") and ad.zone_report(a, "lower")["graphic"] >= ad.GRAPHIC_COVER:
            cap_zone = "middle"
            plan.append({"what": "captions", "zone": "middle", "at": "", "why": "lower third has an on-screen graphic"})
        n, skipped = 0, 0
        stamps = [(s0, e) for z, s0, e in busy if z != "top" or cap_zone == "top"]
        for ph in phrases(cues):
            spans = []
            if s["captions"] == "kinetic":
                for k, t0 in enumerate(ph["times"]):
                    spans.append((k, t0, ph["times"][k + 1] if k + 1 < len(ph["times"]) else ph["end"]))
            else:
                spans.append((None, ph["start"], ph["end"]))
            for lit, t0, t1 in spans:
                if any(t0 < e and s0 < t1 for s0, e in stamps) or not free(cap_zone, t0, t1):
                    skipped += 1
                    continue
                p = outdir / f"ov_cap_{n}.png"
                render_caption(ph["words"], lit, s, w, h, p, cap_zone)
                layers.append({"png": p, "start": t0, "end": t1})
                n += 1
            if n >= 120:  # cap the number of FFmpeg inputs
                break
        if skipped:
            plan.append({"what": "captions", "zone": cap_zone, "at": "", "why": f"{skipped} caption beat(s) held back while a word was on screen"})

    countdown = None
    if s.get("countdown") and payoff_at and payoff_at > 1.5 and hook_zone:
        from PIL import Image, ImageDraw
        bw, bh = int(w * 0.80), max(8, h // 200)
        bar = Image.new("RGBA", (2 * bw, bh), (0, 0, 0, 0))  # left half filled, right half clear: a sliding crop fills it
        ImageDraw.Draw(bar).rectangle([0, 0, bw, bh], fill=_rgba(s["accent"]))
        bar.save(outdir / "ov_countdown.png")
        countdown = {"end": payoff_at, "png": outdir / "ov_countdown.png", "x": int(w * 0.10), "y": hook_bottom + int(h * 0.015), "w": bw, "h": bh}
        plan.append({"what": "countdown bar", "zone": hook_zone, "at": f"0–{payoff_at:.1f}s", "why": "builds to the payoff"})
    return {"style": s, "style_id": style_id(s), "layers": layers, "countdown": countdown, "words": words, "plan": plan}


def ffmpeg_graph(base_label: str, first_input: int, layers: list[dict], countdown: dict | None, w: int, h: int) -> tuple[list[str], list[str], str]:
    """(extra input args, filter parts, final label). Each PNG is one still input overlaid in its window."""
    inputs, parts, cur = [], [], base_label
    for i, L in enumerate(layers):
        inputs += ["-i", str(L["png"])]
        nxt = f"ol{i}"
        parts.append(f"[{cur}][{first_input + i}:v]overlay=0:0:enable='between(t,{L['start']:.3f},{L['end']:.3f})'[{nxt}]")
        cur = nxt
    if countdown:
        T, bw, bh = countdown["end"], countdown["w"], countdown["h"]
        k = first_input + len(layers)
        inputs += ["-loop", "1", "-t", f"{T:.3f}", "-i", str(countdown["png"])]
        parts.append(f"[{k}:v]crop=w={bw}:h={bh}:x='{bw}-{bw}*min(t/{T:.3f},1)':y=0[cdbar]")
        parts.append(f"[{cur}]drawbox=x={countdown['x']}:y={countdown['y']}:w={bw}:h={bh}:color=black@0.45:t=fill:"
                     f"enable='lt(t,{T:.3f})'[cdbg]")
        parts.append(f"[cdbg][cdbar]overlay={countdown['x']}:{countdown['y']}:enable='lt(t,{T:.3f})'[olcd]")
        cur = "olcd"
    return inputs, parts, cur
