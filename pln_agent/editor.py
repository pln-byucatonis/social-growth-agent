"""The editor: watches the footage before cutting it.

The moments a clip arrives with are rough — a label, a start and an end from the clip library — and cutting
straight from them gave Reels padded with commentary desks and scoreboards, and slow-motion replays of the wrong
second. So before a Reel is cut, the editor looks at the source itself: frames at 2 a second across the window
around the moment, tiled into contact sheets with each frame's time printed on it, shown to Claude with the brand,
the platform's length limits and the editing rules below. It answers with:

* whether the decisive action can actually be seen on a phone (bodies tangled in a dark car, a ball too small to
  follow: no) — an unreadable moment is dropped, never padded into a post;
* what happens, in one plain sentence (the critic and, later, the overlay writers use it);
* the shots: in and out times, each one a setup, the payoff or a reaction, with nothing in between that isn't action;
* the payoff frame, how strong it is visually, and whether it earns a slow-motion replay at all.

The cut and the reasons are stored on the post (treatment.editor) so its page shows why it was cut that way.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .db import DB, now_iso

FPS = 2.0  # defaults; config/editor.json overrides
PAD_BEFORE, PAD_AFTER = 4.0, 4.0  # look this far either side of the clip's own moments
MAX_WINDOW = 70.0  # seconds of source looked at, at most (2 fps → 140 frames, 9 sheets)
MIN_SHOT = 0.8
EDITOR_MODEL = os.environ.get("PLN_EDITOR_MODEL", "us.anthropic.claude-opus-4-6-v1")  # the strongest model this account can use
REPLAY_STRENGTH = 7  # payoff strength (0-10) a replay needs, as well as the editor saying it is worth one

SYSTEM = """You are the senior editor of a sports network's social team. You cut 9:16 Reels and TikToks from long
event footage for people scrolling with the sound off, who give a clip one second before swiping. You have watched
thousands of viral sports clips and you are ruthless about dead time. You only answer with JSON."""

RULES = """How to cut:
- A stranger must understand what happened from the pictures alone, on a phone. The frames are the full landscape
  picture shrunk down; the Reel shows a 9:16 crop on the action (about the middle third, at full phone width), so judge
  legibility as if zoomed in about three times. Judge it by the sport's own terms (below). If the decisive action
  still cannot be seen (the camera loses it, it is off-frame or blocked, nothing visibly changes), set
  "readable": false. Do not rescue it with padding.
- Start on motion that leads straight into the payoff (at most ~3 s of build-up). Never open on a commentary desk, an
  empty course, a scoreboard or a title card.
- Every second must show the action or a face reacting to it. Cut out commentators talking at a desk, scoreboards held
  for more than ~1.5 s, crowd and scenery filler, resets and walking around. Shots can jump across the footage.
- End within ~1.5 s of the reaction. The whole cut is usually 6-20 s; never pad it to reach a length.
- Shots are in story order, at least 0.8 s each, inside the window shown, not overlapping.
- "replay_worthy" is true only for one fast, clearly visible, dramatic action whose detail is better in slow motion
  (a long putt dropping, a reversal, a crash, a tap-out where the hand and the face are both visible). Never for talking,
  slow grappling, celebrations, or anything you had to squint at. "replay_window" is the 1.5-3 s of source to replay.
- Times are source seconds, read from the labels printed on the frames.

Reply with JSON only:
{"readable": true, "why": "one sentence", "what_happens": "one plain sentence of the visible action, no hype",
 "payoff": {"t": 914.2, "what": "the ball drops", "strength": 8, "replay_worthy": true, "replay_window": [913.0, 915.4]},
 "shots": [{"in": 910.5, "out": 912.0, "role": "setup", "why": "lines up the putt"},
           {"in": 912.0, "out": 915.6, "role": "payoff", "why": "the putt"},
           {"in": 931.0, "out": 932.5, "role": "reaction", "why": "partner's reaction"}],
 "cut_out": ["912-930 commentary desk"]}"""


class EditorError(RuntimeError):
    pass


def config() -> dict:
    from .config import CONFIG_DIR
    try:
        return json.loads((CONFIG_DIR / "editor.json").read_text())
    except FileNotFoundError:
        return {}


def _llm(system: str, messages: list[dict], max_tokens: int) -> str:
    """The editor and the critic judge pictures, so they use the strongest model (PLN_EDITOR_MODEL); tests swap team._llm."""
    from . import team
    if team._llm is not team.bedrock_call:
        return team._llm(system, messages, max_tokens)
    return team.bedrock_call(system, messages, max_tokens, model=EDITOR_MODEL)


def frames(src: str, t0: float, t1: float, outdir: Path, fps: float = FPS, width: int = 320) -> list[tuple[float, Path]]:
    """Frames at `fps` between t0 and t1 (input-side seek, so only those seconds of a long master are read)."""
    outdir.mkdir(parents=True, exist_ok=True)
    pat = outdir / "f_%04d.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t0):.3f}", "-t", f"{t1 - t0:.3f}",
                    "-i", src, "-vf", f"fps={fps},scale={width}:-2", "-q:v", "4", str(pat)], capture_output=True, timeout=300)
    files = sorted(outdir.glob("f_*.jpg"))
    return [(round(max(0.0, t0) + i / fps, 2), f) for i, f in enumerate(files)]


def sheets(shots: list[tuple[float, Path]], cols: int = 4, rows: int = 4) -> list[tuple[str, list[float]]]:
    """Tile frames into JPEG contact sheets with each frame's time printed on it: [(base64 jpeg, [times])]."""
    from PIL import Image, ImageDraw
    out = []
    per = cols * rows
    for k in range(0, len(shots), per):
        group = shots[k:k + per]
        imgs = [(t, Image.open(p).convert("RGB")) for t, p in group]
        w, h = imgs[0][1].size
        sheet = Image.new("RGB", (cols * w, ((len(imgs) + cols - 1) // cols) * h), "black")
        d = ImageDraw.Draw(sheet)
        for i, (t, im) in enumerate(imgs):
            x, y = (i % cols) * w, (i // cols) * h
            sheet.paste(im.resize((w, h)), (x, y))
            d.rectangle([x, y, x + 62, y + 18], fill="black")
            d.text((x + 4, y + 3), f"{t:.1f}s", fill="yellow")
        buf = io.BytesIO()
        sheet.save(buf, "JPEG", quality=72)
        out.append((base64.b64encode(buf.getvalue()).decode(), [t for t, _ in group]))
    return out


def images(sh: list[tuple[str, list[float]]]) -> list[dict]:
    content = []
    for i, (b64, ts) in enumerate(sh):
        content += [{"type": "text", "text": f"Sheet {i + 1}: frames {ts[0]:.1f}s to {ts[-1]:.1f}s, left to right, top to bottom"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
    return content


def window(v: dict, asset: dict) -> tuple[float, float]:
    """The stretch of source to watch: the post's current segments and the clip's moments, with room either side."""
    segs = (v.get("edl") or {}).get("segments") or []
    pts = [s["source_in"] for s in segs] + [s["source_out"] for s in segs]
    dur = asset.get("duration_s") or 0
    if not pts:
        pts = [m["start"] for m in asset.get("moments") or []] + [m["end"] for m in asset.get("moments") or []]
    if not pts:
        raise EditorError("no moment to look at")
    a, b = max(0.0, min(pts) - PAD_BEFORE), max(pts) + PAD_AFTER
    if dur:
        b = min(b, dur)
    if b - a > MAX_WINDOW:  # centre on the strongest moment inside the window
        ms = [m for m in asset.get("moments") or [] if a <= m.get("start", -1) <= b and m.get("kind") != "setup"]
        c = max(ms, key=lambda m: m.get("intensity") or 0)["start"] if ms else (a + b) / 2
        a = max(a, c - MAX_WINDOW * 0.6)
        b = min(b, a + MAX_WINDOW)
    return round(a, 2), round(b, 2)


def _brief(db: DB, v: dict, asset: dict, a: float, b: float, notes: str) -> str:
    spec = (v.get("validation") or {}).get("spec") or {}
    from .accounts import load_brands
    brand = next((x for x in load_brands() if x["brand_id"] == v.get("brand_id")), None)
    moments = [f"- {m.get('label') or m.get('kind')}: {m['start']:.1f}-{m['end']:.1f}s ({m.get('kind')})"
               for m in asset.get("moments") or [] if a <= m.get("start", -1) <= b]
    sport = (config().get("sports") or {}).get(asset.get("brand_id") or v.get("brand_id") or "", "")
    parts = [f"Brand: {brand['name'] if brand else v.get('brand_id')}" + (f"\n{sport}" if sport else ""),
             f"Clip: {asset.get('title') or ''}. {asset.get('description') or ''}".strip(),
             f"Platform: {v.get('platform')} ({spec.get('min_s', 5)}-{spec.get('max_s', 60)} s allowed)",
             f"The clip library's rough moments in this window (may be wrong):\n" + ("\n".join(moments) or "- none"),
             f"You are looking at source {a:.1f}s to {b:.1f}s at {config().get('fps', FPS):g} frames a second."]
    if notes:
        parts.append("The critic watched your last cut and said this, fix it:\n" + notes)
    return "\n\n".join(parts) + "\n\n" + RULES


def check(ed: dict, a: float, b: float, spec: dict) -> dict:
    """Validate and tidy the editor's answer; raise if the cut can't be used."""
    if not ed.get("readable"):
        return {**ed, "shots": []}
    shots = sorted(({"in": round(max(a, float(s["in"])), 2), "out": round(min(b + 1.0, float(s["out"])), 2),
                     "role": s.get("role") or "shot", "why": s.get("why") or ""} for s in ed.get("shots") or []),
                   key=lambda s: s["in"])
    clean = []
    for s in shots:
        if clean and s["in"] < clean[-1]["out"]:
            s["in"] = clean[-1]["out"]
        if s["out"] - s["in"] >= MIN_SHOT:
            clean.append(s)
    if not clean:
        raise EditorError("the editor returned no usable shots")
    total = sum(s["out"] - s["in"] for s in clean)
    if spec.get("max_s") and total > spec["max_s"]:
        raise EditorError(f"cut is {total:.1f}s, over the {spec['max_s']}s limit")
    p = ed.get("payoff") or {}
    if p.get("t") is not None and not any(s["in"] - 0.3 <= float(p["t"]) <= s["out"] + 0.3 for s in clean):
        raise EditorError("the payoff isn't inside any shot")
    return {**ed, "shots": clean, "length_s": round(total, 2)}


def edit(db: DB, v: dict, asset: dict, notes: str = "", src: str | None = None) -> dict:
    """Watch the window around the moment and decide the cut."""
    from .media import resolve_media
    from .team import _json
    a, b = window(v, asset)
    src = src or resolve_media(asset["media_path"])
    with tempfile.TemporaryDirectory() as d:
        cfg = config()
        fr = frames(src, a, b, Path(d), fps=float(cfg.get("fps", FPS)), width=int(cfg.get("frame_width", 320)))
        if len(fr) < 4:
            raise EditorError("couldn't read frames from the source")
        while len(fr) > 162:  # at most 18 sheets in one request
            fr = fr[::2]
        sh = sheets(fr, cols=3 if (asset.get("width") or 16) >= (asset.get("height") or 9) else 6, rows=3)
    content = images(sh) + [{"type": "text", "text": _brief(db, v, asset, a, b, notes)}]
    last = None
    for _ in range(2):  # one retry when the answer can't be used
        raw = _llm(SYSTEM, [{"role": "user", "content": content}], 1500)
        try:
            ed = check(_json(raw), a, b, (v.get("validation") or {}).get("spec") or {})
            return {**ed, "window": [a, b], "frames": len(fr), "at": now_iso()}
        except (EditorError, ValueError, KeyError, TypeError) as e:
            last = e
            content = content + [{"type": "text", "text": f"That answer couldn't be used ({e}). Reply again with valid JSON."}]
    raise EditorError(f"editor answer unusable: {last}")


def apply(db: DB, variant_id: str, ed: dict, actor: str = "editor") -> dict:
    """Put the editor's shots on the post, with the payoff and replay decision on the edited timeline."""
    from .strategists.base import tc
    v = db.get("variants", "variant_id", variant_id)
    segs = [{"source_in": s["in"], "source_out": s["out"], "purpose": s["role"], "moment": s["why"][:60],
             "source_timecode": f"{tc(s['in'])}–{tc(s['out'])}"} for s in ed["shots"]]
    p = ed.get("payoff") or {}
    t_out, payoff_at = 0.0, None
    for s in ed["shots"]:
        if p.get("t") is not None and s["in"] - 0.3 <= float(p["t"]) <= s["out"] + 0.3:
            payoff_at = round(t_out + min(max(float(p["t"]), s["in"]), s["out"]) - s["in"], 2)
        t_out += s["out"] - s["in"]
    worthy = bool(p.get("replay_worthy")) and (p.get("strength") or 0) >= REPLAY_STRENGTH and payoff_at is not None
    rw = p.get("replay_window") if worthy else None
    edl = {**(v.get("edl") or {}), "segments": segs, "variant_label": None,
           "notes": "Cut by the editor from the frames: " + (ed.get("why") or "")}
    t = dict(v.get("treatment") or {})
    t["editor"] = {"what_happens": ed.get("what_happens", ""), "why": ed.get("why", ""), "payoff": p, "payoff_at_s": payoff_at,
                   "replay_worthy": worthy, "replay_window": rw, "shots": ed["shots"], "cut_out": ed.get("cut_out") or [],
                   "length_s": ed.get("length_s"), "window": ed.get("window"), "at": ed.get("at") or now_iso()}
    thumb = {**(v.get("thumbnail") or {}), "source_time": float(p["t"])} if p.get("t") is not None else v.get("thumbnail")
    db.update("variants", "variant_id", variant_id, {"edl": edl, "treatment": t, "thumbnail": thumb, "updated_at": now_iso()})
    db.audit(actor, "editor_cut", "variants", variant_id, {"shots": len(segs), "length_s": ed.get("length_s"),
                                                            "payoff_at_s": payoff_at, "replay": worthy})
    return t["editor"]
