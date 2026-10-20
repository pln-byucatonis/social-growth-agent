"""Editorial pack generation: asset → routes → per-platform strategist → validate → score → state."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from .accounts import load_brands
from .config import Settings
from .db import DB, now_iso
from .ingest import load_calendar
from .llm import get_provider
from .rights import evaluate
from .routing import route_context_extras, routes_for_asset
from .scoring import allocation, score_variant
from .strategists import STRATEGISTS, CreativeContext, similarity

LOCKED_STATES = {"approved", "scheduled", "publishing", "published", "simulated", "failed"}
SIMILARITY_MAX = 0.45


def _upcoming(calendar: list[dict], brand_id: str, today: date) -> dict | None:
    for c in sorted(calendar, key=lambda c: c["date"]):
        if c["brand_id"] == brand_id and (date.fromisoformat(c["date"]) - today).days >= 0:
            return c
    return None


def generate_pack(db: DB, asset_id: str, settings: Settings, today: date | None = None,
                  include_extended: bool = True, actor: str = "planner") -> dict:
    today = today or datetime.now().date()
    asset = db.get("assets", "asset_id", asset_id)
    if not asset:
        raise KeyError(asset_id)
    brands = {b["brand_id"]: b for b in load_brands()}
    calendar = load_calendar(settings.content_calendar_csv)
    rights = evaluate(asset).as_dict()
    provider = get_provider(settings.llm_provider)
    routes, skipped = routes_for_asset(db, asset, include_extended)
    pack_id = f"pack-{asset_id}-{uuid.uuid4().hex[:6]}"
    amp = allocation()["pln_amplification"]
    made, notes = [], list(skipped)
    for r in routes:
        strat = STRATEGISTS.get(r.account["platform"])
        if not strat:
            continue
        vid = f"{asset_id}:{r.account['account_id']}"
        existing = db.get("variants", "variant_id", vid)
        if existing and existing["state"] in LOCKED_STATES:
            notes.append(f"{vid}: kept existing {existing['state']} variant (not regenerated)")
            continue
        extras = route_context_extras(db, asset, r, brands)
        ctx = CreativeContext(asset=asset, account=r.account, brand=brands[asset["brand_id"]], platform=r.account["platform"],
                              route_role=r.role, upcoming_event=_upcoming(calendar, asset["brand_id"], today), **extras)
        if not ctx.moments and strat.spec.get("needs_video", True):
            notes.append(f"{vid}: no annotated in-range moments → no video creative (won't guess footage)")
            continue
        v = strat.build(ctx)
        v = provider.refine(v, ctx, strat)
        v.update({"variant_id": vid, "pack_id": pack_id, "asset_id": asset_id, "account_id": r.account["account_id"],
                  "brand_id": asset["brand_id"], "route_role": r.role})
        sc = score_variant(db, asset, v, rights, calendar, today)
        if r.role == "pln_amplification" and sc["total"] < amp["min_score"]:
            notes.append(f"{vid}: PLN amplification skipped (score {sc['total']} < {amp['min_score']})")
            continue
        made.append((v, sc, ctx, strat))

    # distinctness: no two destinations of one asset may carry near-duplicate copy
    for i, (v, _, _, _) in enumerate(made):
        for w, _, _, _ in made[:i]:
            sim = similarity(v["caption"], w["caption"])
            if sim > SIMILARITY_MAX:
                v["validation"]["errors"].append(f"caption {sim:.2f} similar to {w['account_id']} (max {SIMILARITY_MAX})")

    out = []
    for v, sc, ctx, strat in made:
        errors = v["validation"]["errors"]
        state = "needs_fix" if (errors or rights["blockers"]) else "in_review"
        row = {k: v.get(k) for k in ("variant_id", "pack_id", "asset_id", "account_id", "platform", "brand_id", "route_role",
                                      "series", "format", "hook", "hook_candidates", "title", "caption", "hashtags",
                                      "hashtag_rationale", "voiceover", "onscreen_text", "edl", "thumbnail", "cta", "angle",
                                      "test_hypothesis", "caption_hash")}
        row.update({"utm_url": ctx.utm_url, "score": sc["total"], "score_breakdown": sc,
                    "risk_flags": rights, "validation": v["validation"], "state": state,
                    "render_status": "not_rendered", "render_path": "", "created_at": now_iso(), "updated_at": now_iso()})
        db.insert("variants", row, upsert_key="variant_id")
        out.append(row)
    db.insert("editorial_packs", {"pack_id": pack_id, "asset_id": asset_id, "created_at": now_iso(),
                                  "summary": {"variants": [o["variant_id"] for o in out], "notes": notes, "rights": rights}})
    db.audit(actor, "editorial_pack_generated", "assets", asset_id, {"pack_id": pack_id, "variants": len(out), "notes": notes})
    return {"pack_id": pack_id, "variants": out, "notes": notes, "rights": rights}
