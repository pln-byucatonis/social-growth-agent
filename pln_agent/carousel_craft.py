"""Carousels the editor makes: the moment told in stills, in one of three formats, checked by the critic.

The first carousels were three stills at fixed times (setup, payoff, reaction) shrunk over a blurred fill, wearing the
Reel's sticker words. They still out-reached the Reels, so this makes them on purpose:

* plan — Claude watches the moment at four frames a second (the frames the editor saw) and chooses a format:
    frame_by_frame  5-7 frames that build to the payoff, so the swipe plays it in slow motion;
    question_first  the cover asks the question the moment answers ("Does this count as a tap?"), two or three frames
                    show it, and an end card asks the comments to argue;
    explainer       for people who've never seen the sport: "CarJitsu in five slides", each slide one thing a newcomer
                    needs, said over a real frame, then follow;
  and picks the exact frames, where to crop each to 4:5 (full bleed, on the action, not the blurred fill), and the line
  each slide carries. The cover is built like a thumbnail; slide two has to stop someone too, because Instagram shows
  carousels again from slide two to people who didn't swipe. Words the account used lately can't come back.
* sharpness — every chosen frame is measured, and moved to the sharpest frame within a quarter second (no model call);
* render — full-bleed 4:5 slides, the cover hook big, a short line on the others, the end card for question/explainer;
* critic — Claude looks at the finished slides in order: does the cover stop the scroll, does each slide earn the next
  swipe, is anything blurry or repeated, does the text read, is the format right for the moment. One redo with its
  notes; a second miss goes to "needs fix" with the reasons (a curated pick goes to Approvals with them instead).

The plan, the critic's scores and the format are kept on the post (treatment.carousel), so formats can be compared.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .db import DB, now_iso

W, H = 1080, 1350
FPS = 4.0
FORMATS = {
    "frame_by_frame": {"min": 5, "max": 7, "end_card": False,
                       "what": "5-7 frames that build to the payoff, one step each, so swiping plays it in slow motion; "
                               "the last frames are the payoff and the reaction"},
    "question_first": {"min": 3, "max": 4, "end_card": True,
                       "what": "the cover asks the question the moment answers (a frame just before the payoff), 2-3 frames "
                               "show what happened, then an end card asks the comments to argue (a short question)"},
    "explainer": {"min": 4, "max": 6, "end_card": True,
                  "what": "for people who have never seen the sport: the cover names it (e.g. 'CarJitsu in five slides'), "
                          "then one thing a newcomer needs per slide, said over a real frame that shows it, then an end "
                          "card to follow for more"},
}
PASS_MIN, COVER_MIN, SWIPE_MIN = 3, 4, 4
SHARP_TRIES = (-0.375, -0.25, -0.125, 0.125, 0.25, 0.375)

SYSTEM = """You are the senior editor of a sports network's social team, making Instagram carousels from event footage.
You only answer with JSON."""

PLAN = """The frames are one moment from a {brand} recording at four a second, each with its time in seconds. {sport}
What happens, as the editor saw it: "{what}".
This carousel goes on {account}{discovery}.

Choose ONE format and make the carousel:
{formats}
How to choose: explainer only when the account's audience mostly doesn't know the sport{explainer_hint}; question_first when
the payoff is something people would argue about (did he tap, was it in, was it fair); otherwise frame_by_frame.

Rules:
- Pick each slide's exact frame by its time label. Only sharp frames where the action reads; never a blurred, dark or
  in-between frame; no two slides that look the same.
- Each slide is cropped to 4:5 from the full frame: give "focus" (x, y from 0 to 1) at the centre of the action.
- Each slide's line goes at the "top" or the "bottom" of the slide: "text_at" is the side with no scoreboard, score bug,
  clock, logo or broadcast text in that crop, and off the faces and the action. The cover's line is always at the top,
  so pick the cover's focus to leave the top clear.
- Slide 1 is the cover: the biggest face or action, with a 2-6 word line that makes people swipe. Slide 2 must also
  work on its own (Instagram shows carousels again from slide 2).
- Every other slide carries a short line (2-7 words) that moves the story on; the lines read as one sequence. Plain,
  specific to what's in the frame, no names, no numbers or digits, no hashtags, no emojis, nothing the frames and the
  editor's description don't support.
- Never say how many slides there are (no "in four slides").
- Never use these words or openings, they are worn out on this account or are stock filler: {avoid}.
{notes}
Reply with JSON only:
{{"format": "frame_by_frame", "why": "one sentence",
 "slides": [{{"t": 1234.5, "focus": {{"x": 0.5, "y": 0.45}}, "text": "...", "text_at": "top", "role": "cover"}}, ...],
 "end_card": "a short question or follow line, or empty"}}"""

CRITIC = """These are the finished slides of an Instagram carousel for {brand}, in order, left to right then down. The
format is {format}: {format_what}. The lines on the slides are: {lines}.
This account's recent openings and words: {recent}.
Score 0-5:
- "cover_stops": slide 1 would stop someone scrolling (a clear face or action, a line that makes you want the next slide)
- "swipe_pull": each slide earns the next swipe; slide 2 also works on its own
- "sharp": no blurred, dark, near-duplicate or in-between frames
- "text_reads": every line is legible on a phone and sits off the faces and the action
- "fresh": nothing repeats this account's recent posts
- "format_fit": this format suits this moment
Verdict "pass", "redo" (say exactly what to change: which slide, which frame, which line) or "kill" (the moment can't
carry a carousel). Reply with JSON only:
{{"scores": {{"cover_stops": 4, ...}}, "verdict": "pass", "problems": ["..."], "fix": "..."}}"""


class CraftError(RuntimeError):
    pass


def _brand_name(brand_id: str) -> str:
    from .accounts import load_brands
    return next((b["name"] for b in load_brands() if b["brand_id"] == brand_id), brand_id)


def window(parent: dict, asset: dict) -> tuple[float, float]:
    """The moment: the Reel's cut as the editor made it, else its segments, else the clip's moments."""
    ed = (parent.get("treatment") or {}).get("editor") or (parent.get("treatment") or {}).get("editor_verdict") or {}
    shots = ed.get("shots") or []
    if shots:
        return max(0.0, shots[0]["in"] - 0.5), shots[-1]["out"] + 0.5
    segs = (parent.get("edl") or {}).get("segments") or []
    if segs:
        return max(0.0, segs[0]["source_in"]), segs[-1]["source_out"]
    ms = asset.get("moments") or []
    if ms:
        return ms[0]["start"], ms[-1]["end"]
    raise CraftError("no moment to make a carousel from")


def recent_lines(db: DB, v: dict, n: int = 10) -> list[str]:
    """Words this account used lately: the Reels' (overlay_copy.recent) and the lines of carousels it has posted or
    committed to (approved / scheduled). Carousels still being made or reviewed don't count, so a batch made together
    isn't marked down for sounding like itself."""
    from .overlay_copy import recent
    rec = recent(db, v)
    out = rec["hooks"] + rec["words"]
    for r in db.all("variants", "account_id=? AND variant_id<>? AND post_type='carousel' AND "
                                "state IN ('approved','scheduled','published','simulated')", [v["account_id"], v["variant_id"]],
                    order="updated_at DESC", limit=n):
        out += [s.get("text") for s in ((r.get("treatment") or {}).get("carousel") or {}).get("slides") or [] if s.get("text")]
    return [x for x in out if x][: n * 4]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]+", "", (s or "").lower()).strip()


def _clean(text: str, banned: set[str], max_words: int = 7) -> str:
    t = re.sub(r"[*_#`\"]+", "", text or "").strip()
    if not t or any(ch.isdigit() for ch in t) or len(t.split()) > max_words or _norm(t) in banned:
        return ""
    return t


def check(plan: dict, a: float, b: float, banned: set[str]) -> dict:
    """Tidy the plan and refuse one that can't be used."""
    fmt = plan.get("format")
    if fmt not in FORMATS:
        raise CraftError(f"unknown format {fmt!r}")
    spec = FORMATS[fmt]
    slides, seen = [], []
    for s in plan.get("slides") or []:
        t = float(s.get("t", -1))
        if not (a - 0.5 <= t <= b + 0.5) or any(abs(t - x) < 0.2 for x in seen):
            continue  # outside the moment, or the same frame twice
        f = s.get("focus") or {}
        slides.append({"t": round(t, 2), "focus": {"x": min(1.0, max(0.0, float(f.get("x", 0.5)))),
                                                   "y": min(1.0, max(0.0, float(f.get("y", 0.5))))},
                       "text": _clean(s.get("text", ""), banned, 6 if not slides else 7), "role": s.get("role") or "",
                       "text_at": "top" if not slides else ("top" if s.get("text_at") == "top" else "bottom")})
        seen.append(t)
    slides = slides[: spec["max"]]
    if len(slides) < spec["min"]:
        raise CraftError(f"{fmt} needs {spec['min']}+ usable slides, got {len(slides)}")
    if not slides[0]["text"]:
        raise CraftError("the cover has no usable line (missing, a number, or one the account used lately)")
    end = _clean(plan.get("end_card", ""), banned, 12) if spec["end_card"] else ""
    if spec["end_card"] and not end:
        end = "Follow for the next one" if fmt == "explainer" else ""
        if not end:
            raise CraftError("a question-first carousel needs its closing question")
    return {"format": fmt, "why": (plan.get("why") or "")[:300], "slides": slides, "end_card": end}


def plan(db: DB, v: dict, parent: dict, asset: dict, src: str, notes: str = "", llm=None) -> dict:
    """One look at the moment: the format, the frames, the crops and the lines."""
    from .editor import _llm, config, frames, images, sheets
    from .overlay_copy import stock
    from .team import _json
    a, b = window(parent, asset)
    if b - a > 25:
        mid = ((parent.get("treatment") or {}).get("editor") or {}).get("payoff", {}).get("t") or (a + b) / 2
        a, b = max(a, float(mid) - 15), min(b, float(mid) + 10)
    with tempfile.TemporaryDirectory() as d:
        fr = frames(src, a, b, Path(d), fps=FPS, width=int(config().get("frame_width", 480)))
        if len(fr) < 6:
            raise CraftError("couldn't read frames from the source")
        sh = sheets(fr[:108], cols=3, rows=3)
    recent = recent_lines(db, v)
    banned = {_norm(x) for x in recent} | {_norm(x) for x in stock()}
    ed = (parent.get("treatment") or {}).get("editor") or {}
    acc = db.get("accounts", "account_id", v["account_id"]) or {}
    discovery = v.get("route_role") == "pln_amplification" or (v.get("account_id") or "").startswith("pln.")
    from .moment_finder import _sport
    prompt = PLAN.format(
        brand=_brand_name(asset.get("brand_id") or v["brand_id"]), sport=_sport(asset.get("brand_id") or v["brand_id"]),
        what=ed.get("what_happens") or asset.get("description") or "unknown",
        account=acc.get("username_original") or v["account_id"],
        discovery=" (a network account: most of its audience has never seen this sport)" if discovery else
        " (the league's own account: its audience knows the sport)",
        formats="\n".join(f"- {k}: {f['what']}" for k, f in FORMATS.items()),
        explainer_hint=" (yes for this account)" if discovery else " (not this account, as a rule)",
        avoid=", ".join(sorted({x.upper() for x in recent} | set(stock()))) or "none",
        notes=("The critic looked at the last version and said, fix it:\n" + notes) if notes else "")
    content = images(sh) + [{"type": "text", "text": prompt}]
    last = None
    for _ in range(2):
        raw = (llm or _llm)(SYSTEM, [{"role": "user", "content": content}], 1600)
        try:
            return {**check(_json(raw), a, b, banned), "window": [a, b], "at": now_iso()}
        except (CraftError, ValueError, KeyError, TypeError) as e:
            last = e
            content = content + [{"type": "text", "text": f"That plan couldn't be used ({e}). Reply again with valid JSON."}]
    raise CraftError(f"plan unusable: {last}")


# ---- render ---------------------------------------------------------------------------------------

def sharpness(path: Path) -> float:
    """Edge energy of the frame (higher = sharper)."""
    from PIL import Image, ImageFilter, ImageStat
    g = Image.open(path).convert("L").resize((480, 270))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def sharpest(src: str, t: float, workdir: Path, grab=None) -> tuple[float, Path]:
    """The chosen frame, or the sharpest one within a quarter second of it."""
    from .carousel import _frame
    grab = grab or _frame
    best = None
    for dt in (0.0, *SHARP_TRIES):
        p = grab(src, t + dt, workdir / f"s_{t + dt:.3f}.png")
        if p:
            s = sharpness(p) * (1.05 if dt == 0 else 1.0)  # the editor's own pick wins only a very close call
            if best is None or s > best[0]:
                best = (s, t + dt, p)
    if not best:
        raise CraftError(f"no frame at {t:.1f}s")
    return round(best[1], 3), best[2]


def crop45(frame: Path, focus: dict):
    """A full-bleed 4:5 slide cut from the frame around the action."""
    from PIL import Image
    im = Image.open(frame).convert("RGB")
    fw, fh = im.size
    if fw / fh > W / H:  # wider than 4:5: full height, a 4:5 window across
        cw, ch = int(fh * W / H), fh
    else:  # taller: full width, a 4:5 window down
        cw, ch = fw, int(fw * H / W)
    x0 = int(min(max(focus["x"] * fw - cw / 2, 0), fw - cw))
    y0 = int(min(max(focus["y"] * fh - ch / 2, 0), fh - ch))
    return lift(im.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H)))


DARK = 80  # mean brightness (0-255) under which a frame is lifted: in-car shots are often murky


def lift(img):
    """A gentle brightness and contrast lift for a dark frame; bright frames are left alone."""
    from PIL import ImageEnhance, ImageOps, ImageStat
    mean = ImageStat.Stat(img.convert("L")).mean[0]
    if mean >= DARK:
        return img
    img = ImageOps.autocontrast(img, cutoff=1)
    return ImageEnhance.Brightness(img).enhance(min(1.6, max(1.0, DARK / max(ImageStat.Stat(img.convert("L")).mean[0], 1))))


def too_alike(a, b) -> bool:
    """Two slides that would read as the same frame on a phone."""
    from PIL import ImageChops, ImageStat
    x, y = a.convert("L").resize((64, 80)), b.convert("L").resize((64, 80))
    return ImageStat.Stat(ImageChops.difference(x, y)).mean[0] < DUPLICATE


DUPLICATE = 9.0  # mean difference (0-255) between two small greyscale versions under which slides count as the same


def render(v: dict, p: dict, src: str, outdir: Path, style: str, grab=None) -> list[dict]:
    """Draw the planned slides into outdir: [{kind, file, role, text, t}]."""
    from . import overlays
    from .carousel import _cta_card, _layer
    s = overlays.style_for(v["brand_id"], {"hook": "outline" if style == "knockout" else style})  # cut-outs need motion
    out = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec, prev = FORMATS[p["format"]], None
        kept = 0
        for i, sl in enumerate(p["slides"]):
            t, fr = sharpest(src, sl["t"], td, grab)
            img = crop45(fr, sl["focus"])
            left = len(p["slides"]) - i - 1
            if prev is not None and too_alike(prev, img) and kept + left >= spec["min"]:
                continue  # the same picture as the slide before: swipe momentum dies; skip it while enough remain
            prev, kept = img, kept + 1
            if sl["text"]:
                png = td / f"txt{i}.png"
                overlays.render_hook(sl["text"], s if i == 0 else {**s, "hook": "outline"}, W, H, png,
                                     "top" if i == 0 else sl.get("text_at") or "bottom")  # clear of the broadcast graphics
                img = _layer(img, png)
            f = outdir / f"slide_{len(out) + 1}.jpg"
            img.save(f, "JPEG", quality=92)
            out.append({"kind": "image", "file": str(f), "role": sl.get("role") or ("cover" if i == 0 else "step"),
                        "text": sl["text"], "t": t, "placed": "full-bleed 4:5"})
        if p.get("end_card"):
            f = _cta_card(p["end_card"], s, W, H, outdir / f"slide_{len(out) + 1}.jpg")
            out.append({"kind": "image", "file": str(f), "role": "end", "text": p["end_card"], "t": None, "placed": "card"})
    return out


# ---- critic ---------------------------------------------------------------------------------------

def judge(res: dict) -> dict:
    keys = ("cover_stops", "swipe_pull", "sharp", "text_reads", "fresh", "format_fit")
    scores = {k: max(0, min(5, int((res.get("scores") or {}).get(k, 0)))) for k in keys}
    said = res.get("verdict") if res.get("verdict") in ("pass", "redo", "kill") else "redo"
    low = [k for k, x in scores.items() if x < PASS_MIN]
    # the scores are the bar: the critic always finds something to improve and says "redo" even when every score
    # clears it, so its notes travel with the post to Approvals and a person decides. Only "kill" is taken at its word.
    clears = bool(res.get("scores")) and not low and scores["cover_stops"] >= COVER_MIN and scores["swipe_pull"] >= SWIPE_MIN
    verdict = "kill" if said == "kill" else ("pass" if clears else "redo")
    return {"scores": scores, "verdict": verdict, "said": said, "problems": res.get("problems") or [], "fix": res.get("fix") or "",
            "below_bar": low, "total": sum(scores.values()), "at": now_iso()}


def review(db: DB, v: dict, slides: list[dict], p: dict, llm=None) -> dict:
    import base64
    import io

    from PIL import Image

    from .editor import _llm
    from .team import _json
    thumbs = [Image.open(sl["file"]).convert("RGB").resize((360, 450)) for sl in slides]
    cols = 4
    sheet = Image.new("RGB", (cols * 360, ((len(thumbs) + cols - 1) // cols) * 450), "black")
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * 360, (i // cols) * 450))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    lines = " / ".join(f"{i + 1}: \"{sl['text']}\"" for i, sl in enumerate(slides) if sl.get("text")) or "none"
    prompt = CRITIC.format(brand=_brand_name(v["brand_id"]), format=p["format"], format_what=FORMATS[p["format"]]["what"],
                           lines=lines, recent="; ".join(recent_lines(db, v)[:20]) or "none yet")
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(buf.getvalue()).decode()}},
               {"type": "text", "text": prompt}]
    try:
        return judge(_json((llm or _llm)(SYSTEM, [{"role": "user", "content": content}], 900)))
    except (ValueError, KeyError, TypeError) as e:
        return judge({"verdict": "redo", "problems": [f"critic answer unusable: {e}"]})


def make(db: DB, v: dict, src: str, outdir: Path, grab=None, llm=None) -> dict:
    """Plan → render → critic, with one redo on the critic's notes. Returns {slides, carousel, passed}."""
    from .overlay_copy import pick_style, recent
    parent = db.get("variants", "variant_id", (v.get("treatment") or {}).get("parent") or "") or {}
    asset = db.get("assets", "asset_id", v["asset_id"]) or {}
    style = pick_style(recent(db, v), "pill")
    notes, history = "", []
    for attempt in (1, 2):
        p = plan(db, v, parent, asset, src, notes, llm)
        for f in outdir.glob("slide_*.jpg"):
            f.unlink()
        slides = render(v, p, src, outdir, style, grab)
        c = review(db, v, slides, p, llm)
        history.append({"attempt": attempt, "format": p["format"], "verdict": c["verdict"], "total": c["total"],
                        "problems": c["problems"][:4]})
        if c["verdict"] == "pass" or c["verdict"] == "kill":
            break
        notes = "; ".join(c["problems"][:5]) + (f". Fix: {c['fix']}" if c["fix"] else "")
    curated = bool(((asset.get("probe") or {}).get("curated")))
    return {"slides": slides, "passed": c["verdict"] == "pass" or curated,
            "carousel": {**p, "critic": c, "history": history, "curated": curated, "style": style}}
