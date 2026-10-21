"""Designed on-screen text: each overlay is drawn as a transparent full-frame PNG (Pillow) and FFmpeg lays it
over the video for its time window. FFmpeg's drawtext can't cut letters out of a band or animate word by word;
this can.

Styles (config/overlay_styles.json, per brand; a post's treatment may override):
* hook      knockout — a solid brand-colour band with the letters cut out, so the footage shows through them
            pill     — text on a rounded brand-colour block
            outline  — heavy white type, thick dark stroke and a soft shadow
* captions  kinetic  — 2–4 word phrases, the spoken word lit in the accent colour as it's said
            classic  — the whole cue in white with a stroke
* stamp     a word ("TAP.", "ACE.") slammed onto the payoff moment, popping in for two frames
* countdown a thin bar filling up to the payoff, under the hook

Everything is placed inside the 9:16 safe zone (clear of the top 12% and bottom 20% platform UI).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import CONFIG_DIR, ROOT

SAFE_TOP, SAFE_BOTTOM = 0.12, 0.20


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


def render_hook(text: str, s: dict, w: int, h: int, out: Path) -> int:
    """Draw the hook; returns the y (px) of its bottom edge so other layers can sit under it."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    text = text.upper().strip()
    pad_x = int(w * 0.06)
    f, lines = _fit(text, s["font_path"], int(w * (0.72 if s["hook"] == "knockout" else 0.80)), int(w * 0.13))
    lh, block_h = _text_block(lines, f)
    top = int(h * (SAFE_TOP + 0.03))
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


def render_stamp(word: str, s: dict, w: int, h: int, out: Path, scale: float = 1.0, tilt: float = -7) -> Path:
    from PIL import ImageDraw
    img = _canvas(w, h)
    f, _ = _fit(word, s["font_path"], int(w * 0.86 * scale), int(w * 0.30 * scale), 60, max_lines=1)
    tmp = _canvas(w, h)
    td = ImageDraw.Draw(tmp)
    tw = f.getlength(word)
    asc, desc = f.getmetrics()
    td.text(((w - tw) / 2, h * 0.42 - (asc + desc) / 2), word, font=f, fill=_rgba(s["accent"]),
            stroke_width=max(6, int(w * 0.012)), stroke_fill=_rgba(s["ink"]))
    img.alpha_composite(tmp.rotate(tilt, center=(w / 2, h * 0.42)))
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


def render_caption(words: list[str], lit: int | None, s: dict, w: int, h: int, out: Path) -> Path:
    """One caption frame; `lit` is the index of the word being spoken (kinetic) or None (classic)."""
    from PIL import ImageDraw
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    text = " ".join(words).upper() if s["captions"] == "kinetic" else " ".join(words)
    f, lines = _fit(text, s["font_path"], int(w * 0.84), int(w * (0.085 if s["captions"] == "kinetic" else 0.06)), 34)
    lh, block_h = _text_block(lines, f)
    y = int(h * (1 - SAFE_BOTTOM) - block_h - h * 0.04)
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


def build(v: dict, cues: list[dict], duration: float, w: int, h: int, outdir: Path, payoff_at: float | None,
          beats: list[dict] | None = None) -> dict:
    """All overlay layers for one render: [{png, start, end}] plus an optional countdown bar spec.
    `beats` are the clip's moments on the edited timeline ({kind, t, intensity}). Brand names never go on screen:
    brand-name filler hooks and brand tags are replaced by bold words for what actually happens."""
    ov = (v.get("treatment") or {}).get("overlay") or {}
    s = style_for(v.get("brand_id", ""), ov)
    bank, seed = words_for(s), v.get("variant_id", "")
    beats = beats or []
    payoff_kind = next((b["kind"] for b in beats if payoff_at is not None and abs(b["t"] - payoff_at) < 0.6), "payoff")
    layers, hook_bottom, words = [], int(h * 0.20), {}
    for j, o in enumerate(v.get("onscreen_text") or []):
        text = (o.get("text") or "").strip()
        if (o.get("position") or "").endswith("small"):
            continue  # brand tags: cheap-looking, and the account already says who posted it
        if is_generic(text) and not ov.get("hook_custom"):
            text = _pick(bank.get("hook") or [], seed + ":hook")
        if not text:
            continue
        p = outdir / f"ov_text_{j}.png"
        hook_bottom = render_hook(text, s, w, h, p)
        words.setdefault("hook", text)
        layers.append({"png": p, "start": float(o.get("start", 0)), "end": float(o.get("end", 3))})
    if s["captions"] in ("kinetic", "classic"):
        n = 0
        for ph in phrases(cues):
            if s["captions"] == "kinetic":
                for k, t0 in enumerate(ph["times"]):
                    t1 = ph["times"][k + 1] if k + 1 < len(ph["times"]) else ph["end"]
                    p = outdir / f"ov_cap_{n}.png"
                    render_caption(ph["words"], k, s, w, h, p)
                    layers.append({"png": p, "start": t0, "end": t1})
                    n += 1
            else:
                p = outdir / f"ov_cap_{n}.png"
                render_caption(ph["words"], None, s, w, h, p)
                layers.append({"png": p, "start": ph["start"], "end": ph["end"]})
                n += 1
            if n >= 120:  # cap the number of FFmpeg inputs
                break
    stamp = (s.get("stamp") or "").strip()
    if stamp != "none" and payoff_at is not None and 0 < payoff_at < duration:
        stamp = stamp or _pick(bank.get("reversal" if payoff_kind == "reversal" else "payoff") or bank.get("payoff") or [],
                               seed + ":payoff")
        if stamp:
            words["payoff"] = stamp
            _slam(layers, stamp, s, w, h, outdir, "stamp", payoff_at, min(duration, payoff_at + 1.4))
    if s.get("reaction") and payoff_at is not None:
        r = next((b for b in beats if b["kind"] == "reaction" and b["t"] >= payoff_at + 1.4 and b["t"] < duration - 0.8), None)
        word = _pick(bank.get("reaction") or [], seed + ":reaction") if r else ""
        if word:
            words["reaction"] = word
            _slam(layers, word, {**s, "accent": s["text"]}, w, h, outdir, "reaction", r["t"], min(duration, r["t"] + 1.3), tilt=6)
    countdown = None
    if s.get("countdown") and payoff_at and payoff_at > 1.5:
        from PIL import Image, ImageDraw
        bw, bh = int(w * 0.80), max(8, h // 200)
        bar = Image.new("RGBA", (2 * bw, bh), (0, 0, 0, 0))  # left half filled, right half clear: a sliding crop fills it
        ImageDraw.Draw(bar).rectangle([0, 0, bw, bh], fill=_rgba(s["accent"]))
        bar.save(outdir / "ov_countdown.png")
        countdown = {"end": payoff_at, "png": outdir / "ov_countdown.png", "x": int(w * 0.10), "y": hook_bottom + int(h * 0.015), "w": bw, "h": bh}
    return {"style": s, "style_id": style_id(s), "layers": layers, "countdown": countdown, "words": words}


def _slam(layers: list, word: str, s: dict, w: int, h: int, outdir: Path, name: str, t0: float, t1: float, tilt: float = -7) -> None:
    """A bold word that pops in (two oversized frames) and holds."""
    big, norm = outdir / f"ov_{name}_pop.png", outdir / f"ov_{name}.png"
    render_stamp(word, s, w, h, big, 1.18, tilt)
    render_stamp(word, s, w, h, norm, 1.0, tilt)
    layers.append({"png": big, "start": t0, "end": t0 + 0.08})
    layers.append({"png": norm, "start": t0 + 0.08, "end": t1})


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
