"""Creative Director: one moment → a distinct execution per destination, not five copies of one post.

For every destination the strategist already wrote copy for, the Creative Director adds the decisions that
make it creative rather than a caption exercise:

  hook        — what happens in the first 1–3 seconds (opening beat + on-screen line)
  edit        — where the cut starts/ends and how fast it moves
  context     — does a new viewer understand what they're watching, and how we give them that
  payoff      — the reason to keep watching, and when it lands
  conversion  — why someone follows after watching

plus three duration variants cut around the payoff — A ≈12s immediate action, B ≈25s action + setup,
C ≈40s full competitive storyline — and which one to run. The pick comes from PLN evidence on
duration for that brand/platform when it exists; otherwise from the platform's starting recipe, labelled as
a hypothesis. Every package lists the Knowledge Engine entries it relied on.
"""
from __future__ import annotations

import uuid

from .db import DB, now_iso
from .strategists.base import tc

VARIANTS = [("A", 12.0, "immediate action", 1.0), ("B", 25.0, "action plus setup", 15.0),
            ("C", 40.0, "complete competitive storyline", 25.0)]  # (label, target s, idea, seconds before payoff)
BUCKET_FOR = {"A": "≤15s", "B": "16–30s", "C": "31–60s"}

RECIPES = {
    "tiktok": {"opening": "the payoff itself — open on the most surprising frame, no preamble", "pace": "fast: hard cut in, no slow-mo before the payoff",
               "context": "one on-screen line naming the sport; no explanation before the action",
               "conversion": "a reason to see the next one (series tag, 'part 2' or the next match)", "default": "A"},
    "instagram": {"opening": "tight tension build — the moment just before it turns", "pace": "tighter editing, one beat of anticipation, then the payoff",
                  "context": "strong caption carries the context so the video can stay tight",
                  "conversion": "save/share prompt and the account's series promise", "default": "B"},
    "facebook": {"opening": "a context beat — who, what's at stake — then the action", "pace": "steadier, lets the competition read",
                 "context": "slightly more competition context than short-form platforms",
                 "conversion": "invite discussion (a real question about the moment), community follows", "default": "C"},
    "youtube": {"opening": "set up the challenge in the first seconds", "pace": "setup → clear payoff → short reaction",
                "context": "the setup is the context: the viewer knows what has to happen",
                "conversion": "subscribe for the full events / next Short in the series", "default": "B"},
    "x": {"opening": "the payoff, immediately", "pace": "shortest cut", "context": "the post text frames it in one line",
          "conversion": "reply prompt during live windows", "default": "A"},
    "linkedin": {"opening": "a business insight, not a highlight", "pace": "one short supporting clip at most",
                 "context": "why this format works as a short-form entertainment product (audience, retention, sponsor fit)",
                 "conversion": "follow PLN for sports-media innovation; partnership conversations", "default": "A"},
}


def _payoff(moments: list) -> object | None:
    cands = [m for m in moments if m.kind != "setup"]
    return max(cands or moments, key=lambda m: m.intensity) if moments else None


def duration_variants(ctx, payoff) -> list[dict]:
    dur = ctx.asset.get("duration_s") or (max(m.end for m in ctx.moments) if ctx.moments else None)
    out = []
    for label, target, idea, lead in VARIANTS:
        if not payoff or not dur:
            out.append({"label": label, "target_s": target, "idea": idea, "available": False,
                        "why": "no annotated payoff moment / unknown source length"})
            continue
        start = max(0.0, payoff.start - lead)
        end = min(dur, start + target)
        start = max(0.0, min(start, end - target)) if end - start < target else start
        actual = round(end - start, 2)
        ok = actual >= 0.75 * target
        out.append({"label": label, "target_s": target, "idea": idea, "available": ok, "actual_s": actual,
                    "why": "" if ok else f"source is only {dur:.0f}s long; a {target:.0f}s cut needs more footage",
                    "segments": [{"source_in": round(start, 2), "source_out": round(end, 2), "purpose": f"variant {label}: {idea}",
                                  "moment": payoff.label, "source_timecode": f"{tc(start)}–{tc(end)}"}] if ok else [],
                    "payoff_at_s": round(payoff.start - start, 2) if ok else None})
    return out


def _evidence_pick(db: DB, brand_id: str, platform: str, available: set[str]) -> tuple[str | None, str, list[str]]:
    rows = db.all("knowledge", "level='pln_evidence' AND status='current' AND topic='duration_bucket' AND brand_id=? AND platform=?",
                  [brand_id, platform])
    best, why, refs = None, "", []
    for e in sorted(rows, key=lambda e: -((e.get("evidence") or {}).get("lift") or 0)):
        ev = e.get("evidence") or {}
        label = next((k for k, b in BUCKET_FOR.items() if b == ev.get("value")), None)
        if label in available and (ev.get("lift") or 0) > 0:
            best, refs = label, [e["entry_id"]]
            why = f"PLN evidence: {ev['value']} cuts {ev['lift']:+.0%} on {ev.get('outcome')} ({ev.get('evidence')}, n={ev.get('n')})"
            break
    return best, why, refs


def treat(db: DB, ctx, v: dict) -> dict:
    platform = ctx.platform
    r = RECIPES.get(platform)
    if not r:
        return {}
    p = _payoff(ctx.moments)
    variants = duration_variants(ctx, p) if platform != "linkedin" else duration_variants(ctx, p)[:1]
    available = {x["label"] for x in variants if x["available"]}
    pick, why, refs = _evidence_pick(db, ctx.asset["brand_id"], platform, available)
    if not pick:
        default = r["default"] if r["default"] in available else (sorted(available)[0] if available else None)
        pick = default
        why = ("starting recipe for " + platform + f" (cut {default}); hypothesis hyp-duration-15-vs-30 — "
               "no PLN duration evidence yet") if default else "no cut available"
        refs = ["hyp-duration-15-vs-30"]
    from .knowledge import guidance
    kn = [e["entry_id"] for e in guidance(db, platform=platform, brand_id=ctx.asset["brand_id"])
          if e["topic"] in ("originality", "analytics", "hook", "duration", "branding", "audio", "personality")][:6]
    if ctx.route_role == "pln_amplification":
        kn = ["meta-facebook-originality-2026", *[k for k in kn if k != "meta-facebook-originality-2026"]]
    on_screen = (v.get("onscreen_text") or [{}])[0]
    return {
        "platform": platform, "route_role": ctx.route_role,
        "hook": {"opening_beat": r["opening"], "first_seconds": (on_screen.get("text") if isinstance(on_screen, dict) else "") or v.get("hook", ""),
                 "hook_line": v.get("hook", "")},
        "edit": {"pace": r["pace"], "segments": (v.get("edl") or {}).get("segments", [])},
        "context": {"approach": r["context"], "needed": platform in ("facebook", "youtube", "linkedin")},
        "payoff": {"what": (p.description if p else ""), "label": p.label if p else "", "intensity": p.intensity if p else None},
        "conversion": {"reason_to_follow": r["conversion"], "cta": v.get("cta", "")},
        "duration_variants": variants, "recommended_variant": pick, "recommended_why": why,
        "knowledge_refs": sorted(set(refs + kn)),
        "note": ("Business treatment: the clip supports an insight post; it is not the highlight re-posted."
                 if platform == "linkedin" else ""),
    }


def save_package(db: DB, asset: dict, treatments: dict[str, dict], actor: str) -> str:
    pid = f"cpkg-{asset['asset_id']}-{uuid.uuid4().hex[:6]}"
    refs = sorted({r for t in treatments.values() for r in t.get("knowledge_refs", [])})
    moment = next((t["payoff"] for t in treatments.values() if t.get("payoff", {}).get("label")), {})
    db.insert("creative_packages", {"package_id": pid, "asset_id": asset["asset_id"], "moment": moment,
                                    "treatments": treatments, "knowledge_refs": refs, "created_by": actor,
                                    "created_at": now_iso()})
    return pid


def use_cut(db: DB, variant_id: str, label: str, actor: str) -> dict:
    """Switch a draft to cut A/B/C. The video changes, so any approval and render are invalidated."""
    v = db.get("variants", "variant_id", variant_id)
    t = (v or {}).get("treatment") or {}
    alt = next((x for x in t.get("duration_variants", []) if x["label"] == label and x.get("available")), None)
    if not alt:
        raise ValueError(f"cut {label} isn't available for this post")
    if v["state"] in ("scheduled", "publishing", "published", "simulated"):
        raise ValueError(f"can't change the cut of a {v['state']} post")
    edl = dict(v.get("edl") or {})
    if "editor_cut" not in edl:
        edl["editor_cut"] = edl.get("segments", [])
    edl["segments"] = alt["segments"]
    edl["variant_label"] = label
    state = "in_review" if v["state"] == "approved" else v["state"]
    db.update("variants", "variant_id", variant_id, {"edl": edl, "state": state, "render_status": "not_rendered",
                                                     "render_path": "", "public_media_url": "", "updated_at": now_iso()})
    if v["state"] == "approved":  # approvals bind to what was reviewed: a new cut needs a new review
        db.insert("approvals", {"variant_id": variant_id, "decision": "revoke", "reviewer": actor,
                                "note": f"cut changed to {label}", "caption_hash": v["caption_hash"], "at": now_iso()})
    db.audit(actor, "variant_cut_changed", "variants", variant_id, {"cut": label, "previous_state": v["state"]})
    return db.get("variants", "variant_id", variant_id)
