"""The Creative Director's eye: look at the actual frames before putting text on them.

Text that lands on a broadcast score bug, a "HOLE IN ONE" animation or a busy crowd reads as cheap and is hard to
read. So before overlays are laid on, the clean render is sampled and each placement zone is scored:

* static graphics — cells that barely change across the clip but have hard edges (score bugs, timers, logos)
* sudden graphics — a zone much busier at a moment than it usually is (a broadcast animation at the payoff)
* busyness and brightness — so light text never sits on a light background

Zones are horizontal bands inside the 9:16 safe area. The plan (what went where, and why) is recorded with the post.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ZONES = {"top": (0.13, 0.34), "middle": (0.34, 0.56), "lower": (0.56, 0.78)}
GRID_W, GRID_H = 9, 16
STATIC_STD, STATIC_EDGE = 7.0, 22.0  # luminance std across the clip / mean edge strength (0–255)
SUDDEN = 2.0  # a zone this many times busier than its median at a moment = a transient graphic
GRAPHIC_COVER = 0.30  # share of a zone covered by static graphics that rules it out (a score bug covers far more)


def _frames(video: str, times: list[float], outdir: Path) -> list:
    from PIL import Image
    out = []
    for i, t in enumerate(times):
        p = outdir / f"af_{i:02d}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video,
                        "-frames:v", "1", "-vf", "scale=180:320", str(p)], capture_output=True, timeout=60)
        out.append(Image.open(p).convert("RGB") if p.exists() else None)
    return out


def _cells(img) -> tuple[list[float], list[float]]:
    from PIL import ImageFilter, ImageStat
    g = img.convert("L")
    e = g.filter(ImageFilter.FIND_EDGES)
    cw, ch = g.width // GRID_W, g.height // GRID_H
    lum, edge = [], []
    for r in range(GRID_H):
        for c in range(GRID_W):
            box = (c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)
            lum.append(ImageStat.Stat(g.crop(box)).mean[0])
            edge.append(ImageStat.Stat(e.crop(box)).mean[0])
    return lum, edge


def analyze(video: str, duration: float, moments: list[float] | None = None, n: int = 10) -> dict:
    """Sample the clean render: n frames across the clip plus one at each given moment (payoff, reaction)."""
    span = max(duration - 0.4, 0.1)
    base = [0.2 + span * i / max(n - 1, 1) for i in range(n)]
    extra = [t for t in (moments or []) if 0 <= t < duration]
    with tempfile.TemporaryDirectory() as d:
        frames = _frames(video, base + extra, Path(d))
    good = [(t, f) for t, f in zip(base + extra, frames) if f is not None]
    if not good:
        return {"ok": False}
    stats = {round(t, 2): _cells(f) for t, f in good}
    base_keys = [round(t, 2) for t in base if round(t, 2) in stats]
    ncell = GRID_W * GRID_H
    stds, edges = [], []
    for i in range(ncell):
        lums = [stats[k][0][i] for k in base_keys]
        mean = sum(lums) / len(lums)
        stds.append((sum((x - mean) ** 2 for x in lums) / len(lums)) ** 0.5)
        edges.append(sum(stats[k][1][i] for k in base_keys) / len(base_keys))
    med_std, med_edge = sorted(stds)[ncell // 2], sorted(edges)[ncell // 2]
    # a graphic is much steadier than the rest of the frame and has real edges (not a flat bar or sky): a locked-off camera (everything
    # steady) doesn't count, a score bug over moving footage does
    graphic = [stds[i] < STATIC_STD and stds[i] < 0.35 * med_std and edges[i] > STATIC_EDGE and edges[i] > 0.6 * med_edge
               for i in range(ncell)]
    return {"ok": True, "stats": stats, "base": base_keys, "graphic": graphic}


def _rows(zone: str) -> range:
    y0, y1 = ZONES[zone]
    return range(int(y0 * GRID_H), max(int(y0 * GRID_H) + 1, int(round(y1 * GRID_H))))


def zone_report(a: dict, zone: str, t: float | None = None) -> dict:
    """Static-graphic cover, busyness at t vs the zone's usual level, and brightness (0–1)."""
    if not a.get("ok"):
        return {"graphic": 0.0, "busy": 1.0, "sudden": False, "lum": 0.5}
    idx = [r * GRID_W + c for r in _rows(zone) for c in range(GRID_W)]
    cover = sum(a["graphic"][i] for i in idx) / len(idx)

    def edge_at(k):
        return sum(a["stats"][k][1][i] for i in idx) / len(idx)

    usual = sorted(edge_at(k) for k in a["base"])
    median = usual[len(usual) // 2] or 1.0
    key = min(a["stats"], key=lambda k: abs(k - t)) if t is not None else None
    now = edge_at(key) if key is not None else median
    lum = (sum(a["stats"][key][0][i] for i in idx) / len(idx) if key is not None
           else sum(sum(a["stats"][k][0][i] for i in idx) / len(idx) for k in a["base"]) / len(a["base"])) / 255
    return {"graphic": round(cover, 2), "busy": round(now / median, 2), "sudden": now / median > SUDDEN, "lum": round(lum, 2)}


def choose(a: dict, candidates: list[str], t: float | None = None) -> tuple[str | None, str]:
    """First clear zone among the candidates (in order of preference), with the reason; None if none is clear."""
    notes = []
    for z in candidates:
        r = zone_report(a, z, t)
        if r["graphic"] >= GRAPHIC_COVER:
            notes.append(f"{z}: on-screen graphic ({int(r['graphic'] * 100)}% of the zone)")
            continue
        if r["sudden"]:
            notes.append(f"{z}: a graphic or flash appears at that moment")
            continue
        return z, ("; ".join(notes + [f"{z} is clear"]) if notes else f"{z} is clear")
    return None, "; ".join(notes) or "no clear zone"


def light_background(a: dict, zone: str, t: float | None) -> bool:
    return zone_report(a, zone, t)["lum"] > 0.62
