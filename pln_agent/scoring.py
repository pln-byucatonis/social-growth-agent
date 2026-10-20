"""Explainable ranking of creative variants.

Inputs are only things we actually know: annotated source moments (with timecodes) and whether the media
file exists, platform fit of the real clip geometry/duration, timeliness from event dates + calendar,
empirical results already imported for this account/series, rights state and the editable allocation.
Unknown empirical data → neutral prior 0.5, labeled "no data" (never a guess).
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime

from .config import CONFIG_DIR
from .db import DB
from .metrics import NORTH_STAR, post_performance

WEIGHTS = {"source": 0.30, "platform_fit": 0.20, "timeliness": 0.15, "empirical": 0.20, "rights": 0.15}


def allocation() -> dict:
    return json.loads((CONFIG_DIR / "allocation.json").read_text())


def _source(asset: dict, variant: dict) -> tuple[float, str]:
    segs = (variant.get("edl") or {}).get("segments") or []
    moments = asset.get("moments") or []
    if not moments:
        return 0.2, "no annotated moments: footage strength unknown"
    used = [m for m in moments if any(s["moment"] and m.get("label", "") in s["moment"] for s in segs)]
    inten = max((m.get("intensity", 0.5) for m in (used or moments)), default=0.5)
    if not asset.get("media_present"):
        return round(inten * 0.5, 3), f"peak intensity {inten} (annotated) but media file missing → halved"
    return round(inten, 3), f"peak intensity {inten} from annotated moments at " + \
        ", ".join(s["source_timecode"] for s in segs)


def _fit(asset: dict, variant: dict) -> tuple[float, str]:
    w, h = asset.get("width"), asset.get("height")
    out = (variant.get("edl") or {}).get("output") or {}
    if not out:
        return 0.6, "no video output (text/live)"
    if not (w and h):
        return 0.5, "source geometry unknown (no probe)"
    src_vertical = h > w
    tgt_vertical = out.get("h", 0) > out.get("w", 0)
    if src_vertical == tgt_vertical:
        return 1.0, f"source {w}x{h} matches target orientation"
    return 0.7, f"source {w}x{h} must be reframed to {out.get('w')}x{out.get('h')} (crop risk: check subject framing)"


def _timeliness(asset: dict, calendar: list[dict], today: date) -> tuple[float, str]:
    score, why = 0.5, "evergreen (no event date)"
    if asset.get("event_date"):
        age = (today - date.fromisoformat(asset["event_date"])).days
        if age < 0:
            score, why = 0.6, f"pre-event asset ({-age}d before event)"
        elif age <= 3:
            score, why = 1.0, f"event {age}d ago"
        elif age <= 14:
            score, why = 0.75, f"event {age}d ago"
        else:
            score, why = 0.45, f"event {age}d ago (archive)"
    up = [c for c in calendar if c["brand_id"] == asset["brand_id"] and 0 <= (date.fromisoformat(c["date"]) - today).days <= 7]
    if up:
        score = min(1.0, score + 0.15)
        why += f"; upcoming {up[0]['event_name']} on {up[0]['date']}"
    return round(score, 3), why


def _empirical(db: DB, account_id: str, series: str, platform: str) -> tuple[float, str]:
    ns = NORTH_STAR.get(platform, {}).get("metric")
    rows = db.query("SELECT variant_id, series FROM variants WHERE account_id=? AND state IN ('published') ", [account_id])
    vals_series, vals_all = [], []
    for r in rows:
        perf = post_performance(db, r["variant_id"])
        v = perf.get(ns)
        if v is None:
            continue
        vals_all.append(v)
        if r["series"] == series:
            vals_series.append(v)
    if len(vals_series) >= 3 and vals_all:
        med = statistics.median(vals_all) or 1e-9
        rel = statistics.median(vals_series) / med
        return round(max(0.0, min(1.0, 0.5 * rel)), 3), f"series median {ns} = {rel:.2f}x account median ({len(vals_series)} posts)"
    return 0.5, f"no data: fewer than 3 measured '{series}' posts on this account (neutral prior)"


def _rights(rights: dict) -> tuple[float, str]:
    if rights["blockers"]:
        return 0.0, "blocked: " + "; ".join(rights["blockers"])
    if rights["review_flags"]:
        return 0.5, "manual review: " + ", ".join(rights["review_flags"])
    return 1.0, "clear"


def score_variant(db: DB, asset: dict, variant: dict, rights: dict, calendar: list[dict], today: date | None = None) -> dict:
    today = today or datetime.now().date()
    parts = {"source": _source(asset, variant), "platform_fit": _fit(asset, variant),
             "timeliness": _timeliness(asset, calendar, today),
             "empirical": _empirical(db, variant["account_id"], variant.get("series", ""), variant["platform"]),
             "rights": _rights(rights)}
    total = sum(WEIGHTS[k] * v[0] for k, v in parts.items())
    share = allocation()["creative_effort_share"].get(variant["brand_id"], 0.05)
    total = round(total * (0.85 + share), 4)  # editable planning weight nudges, never dominates
    return {"total": total, "allocation_weight": share,
            "parts": {k: {"score": v[0], "why": v[1], "weight": WEIGHTS[k]} for k, v in parts.items()}}
