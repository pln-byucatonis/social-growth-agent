"""Edit effects: the replay, the punch-in and the sound that make a sports Reel land (config/edit_fx.json).

* Replay — after the main edit, the payoff again: ~2 s of source around it at half speed, cropped in (punch-in) on
  the action, with a small REPLAY tag and the commentary turned down. Skipped when there is no payoff moment, when
  the post would run past the platform's limit, or when the post or brand switches it off.
* Punch-in on the payoff — a quick zoom (1.12x) as the decisive moment hits, held under half a second.
* Sound — an impact hit on the payoff and a whoosh into the replay. Both are synthesised by FFmpeg here, so there is
  no third-party audio to clear. Music beds come only from the Music library (approved by ear, licence cleared for the
  platform; pln_agent/music.py) or a licensed bed listed in the config, ducked under the commentary.

Every choice is written to the render's detail so the post page and the Creative DB can see what each post had.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import CONFIG_DIR

FPS = 30


def config() -> dict:
    return json.loads((CONFIG_DIR / "edit_fx.json").read_text())


def settings_for(v: dict) -> dict:
    """The config, with the post's own switches (treatment.edit_fx = {replay, punch_in, sfx, music}) applied."""
    cfg = config()
    own = ((v.get("treatment") or {}).get("edit_fx") or {}) if isinstance(v.get("treatment"), dict) else {}
    out = {}
    for k in ("replay", "punch_in", "sfx", "music"):
        c = dict(cfg.get(k) or {})
        c["enabled"] = bool(c.get("enabled")) and own.get(k, True) is not False
        out[k] = c
    if v.get("brand_id") in (out["replay"].get("skip_brands") or []):
        out["replay"]["enabled"] = False
    for k, val in ((out["replay"].get("brands") or {}).get(v.get("brand_id") or "") or {}).items():
        if not k.startswith("_"):  # per-brand replay window (a putt lands seconds after the stroke)
            out["replay"][k] = val
    return out


def payoff_time(v: dict, asset: dict, segs: list[dict], cut: dict | None) -> tuple[float | None, list[dict]]:
    """The payoff on the edited timeline (the Creative Director's cut says it; otherwise the strongest action moment),
    and the clip's moments mapped onto that timeline."""
    label = (v.get("edl") or {}).get("variant_label")
    dv = cut or next((c for c in (v.get("treatment") or {}).get("duration_variants") or [] if c["label"] == label), None)
    payoff = (dv or {}).get("payoff_at_s")
    beats, t_out = [], 0.0
    for sg in segs:
        for m in asset.get("moments") or []:
            if m.get("kind") != "setup" and sg["source_in"] <= m.get("start", -1) < sg["source_out"]:
                beats.append({"kind": m.get("kind"), "t": t_out + m["start"] - sg["source_in"], "intensity": m.get("intensity") or 0})
        t_out += sg["source_out"] - sg["source_in"]
    hits = [b for b in beats if b["kind"] != "reaction"]
    if payoff is None and hits:
        payoff = max(hits, key=lambda b: b["intensity"])["t"]
    return payoff, beats


def to_source(segs: list[dict], t_out: float) -> float | None:
    t = 0.0
    for sg in segs:
        n = sg["source_out"] - sg["source_in"]
        if t <= t_out <= t + n:
            return sg["source_in"] + (t_out - t)
        t += n
    return None


def replay_plan(segs: list[dict], payoff: float | None, fx: dict, source_duration: float | None, max_total: float | None) -> dict | None:
    r = fx["replay"]
    if not r["enabled"] or payoff is None:
        return None
    p = to_source(segs, payoff)
    if p is None:
        return None
    a = max(0.0, p - float(r["source_before_s"]))
    b = p + float(r["source_after_s"])
    if source_duration:
        b = min(b, source_duration)
    if b - a < 0.8:
        return None
    speed = float(r["speed"])
    out_len = round((b - a) / speed, 3)
    edit = sum(sg["source_out"] - sg["source_in"] for sg in segs)
    if max_total and edit + out_len > max_total:
        return None
    return {"src_in": round(a, 3), "src_out": round(b, 3), "speed": speed, "zoom": float(r["zoom"]), "out_len": out_len,
            "starts_at": round(edit, 3), "volume": float(r["volume"]), "label": r.get("label") or ""}


def punch_in_filter(payoff: float | None, fx: dict, w: int, h: int) -> str:
    """zoompan on the finished 9:16 base: ramps to `zoom` as the payoff hits, holds, then snaps back."""
    pi = fx["punch_in"]
    if not pi["enabled"] or payoff is None:
        return ""
    z, ramp, hold = float(pi["zoom"]), float(pi["ramp_s"]), float(pi["hold_s"])
    t0, t1 = max(0.0, payoff - ramp), payoff + hold
    zexpr = f"if(between(it,{t0:.3f},{payoff:.3f}),1+({z - 1:.3f})*(it-{t0:.3f})/{ramp:.3f},if(between(it,{payoff:.3f},{t1:.3f}),{z:.3f},1))"
    return (f"fps={FPS},zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={FPS},setsar=1")


def make_sfx(outdir: Path) -> dict:
    """Two short sounds made from nothing but FFmpeg's generators: a low impact hit and a noise whoosh."""
    hit, whoosh = outdir / "sfx_hit.wav", outdir / "sfx_whoosh.wav"
    common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(common + ["-f", "lavfi", "-i", "sine=frequency=55:duration=0.6:sample_rate=48000",
                             "-f", "lavfi", "-i", "anoisesrc=color=brown:duration=0.6:sample_rate=48000:amplitude=0.8",
                             "-filter_complex", "[0]volume=1.6[s];[1]lowpass=f=220,volume=0.9[n];[s][n]amix=inputs=2:normalize=0,"
                             "afade=t=out:st=0.04:d=0.56:curve=exp,volume=11dB,alimiter=limit=0.89:level=false,"
                             "aformat=channel_layouts=stereo", str(hit)],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(common + ["-f", "lavfi", "-i", "anoisesrc=color=pink:duration=0.7:sample_rate=48000:amplitude=0.6",
                             "-af", "highpass=f=600,lowpass=f=5000,afade=t=in:d=0.45:curve=qsin,afade=t=out:st=0.45:d=0.25,"
                                    "volume=17dB,alimiter=limit=0.89:level=false,aformat=channel_layouts=stereo", str(whoosh)],
                   check=True, capture_output=True, timeout=60)
    return {"hit": hit, "whoosh": whoosh}


def music_bed(brand_id: str, fx: dict, db=None, platform: str = "", key: str = "") -> dict | None:
    """An approved, licence-cleared bed from the Music library (pln_agent/music.py), else one listed in the config."""
    m = fx["music"]
    if not m["enabled"]:
        return None
    if db is not None:
        from . import music
        bed = music.pick(db, brand_id, platform, key)
        if bed:
            return bed
    return next((b for b in m.get("beds") or [] if b.get("path") and b.get("license")
                 and (not b.get("brands") or brand_id in b["brands"])), None)