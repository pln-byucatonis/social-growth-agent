"""World Putting League → Putt Tour rebrand / audience-migration manager.

Principles enforced in code:
* No follower merge is assumed or modeled; outcomes are reported as *migration proxies*.
* Nothing renames, deletes or edits a profile. Profile changes are *proposals* a human admin executes in
  the native app and then marks done. There is deliberately no adapter method for profile edits.
* Decisions require verified evidence. Without imported analytics the sheet says "audit required" and the
  planner uses the lowest-risk reversible option (no renames) as a provisional route.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import urlencode

from .config import CONFIG_DIR
from .db import DB, now_iso
from .metrics import baseline, fmt

OPTIONS = {
    "keep_legacy_temporarily": "Keep the WPL identity for now; post Putt Tour content with transitional copy.",
    "inplace_rebrand": "Rebrand the existing WPL account in place (display name/bio/handle) — keeps its audience & history.",
    "bridge_to_canonical": "Keep the canonical Putt Tour account as home; WPL becomes a 60–90-day low-frequency bridge.",
    "redirect_to_new_account": "Point WPL to a different (new) Putt Tour account. Only with evidence the legacy account is unhealthy.",
}
ROUTE_ROLE = {"keep_legacy_temporarily": "legacy_inplace", "inplace_rebrand": "legacy_inplace",
              "bridge_to_canonical": "legacy_bridge", "redirect_to_new_account": "legacy_bridge"}

ANNOUNCEMENTS = {
    "founder_story": {
        "label": "Founder / tour story",
        "copy": "World Putting League is now Putt Tour. Same players, same pressure, a name that says exactly what we do. "
                "Everything you followed WPL for is here, and there's a lot more coming.",
        "edit": "Talking head (founder/commissioner, approved) intercut with archive WPL moments; 20–40s.",
    },
    "athlete_led": {
        "label": "Athlete-led announcement",
        "copy": "We asked the players to tell you themselves: World Putting League is now Putt Tour.",
        "edit": "2–4 verified athletes (signed releases) each deliver one line to camera; 15–25s.",
    },
    "rules_highlights": {
        "label": "Fast rules + highlights",
        "copy": "New name, same rules: one putt, real pressure. World Putting League is now Putt Tour.",
        "edit": "Rules in 20 Seconds format over the strongest licensed highlight; 15–20s.",
    },
}

PINNED_TEMPLATE = "📌 World Putting League is now Putt Tour. {where}"
BIO_TEMPLATES = {
    "bridge_to_canonical": "World Putting League is now Putt Tour → follow {canonical}",
    "keep_legacy_temporarily": "Putt Tour (formerly World Putting League) · pro putting, real pressure",
    "inplace_rebrand": "Putt Tour (formerly World Putting League) · pro putting, real pressure",
    "redirect_to_new_account": "World Putting League is now Putt Tour → {canonical}",
}


def load_config() -> dict:
    return json.loads((CONFIG_DIR / "migration.json").read_text())


def sync_decisions(db: DB, cfg: dict | None = None) -> None:
    """Mirror editable config into the DB (keeps any approved status recorded in DB)."""
    cfg = cfg or load_config()
    for platform, d in cfg["platforms"].items():
        existing = db.get("migration_decisions", "platform", platform)
        row = {"platform": platform, "option": d["option"], "canonical_account_id": d["canonical_account_id"],
               "legacy_account_id": d["legacy_account_id"],
               "status": existing["status"] if existing and existing["option"] == d["option"] else d.get("status", "proposed"),
               "rationale": OPTIONS[d["option"]], "evidence": existing["evidence"] if existing else {},
               "decided_by": existing["decided_by"] if existing else "", "decided_at": existing["decided_at"] if existing else ""}
        db.insert("migration_decisions", row, upsert_key="platform")


def decision_for(db: DB, platform: str) -> dict | None:
    return db.get("migration_decisions", "platform", platform)


def approve_decision(db: DB, platform: str, actor: str, note: str = "") -> None:
    d = decision_for(db, platform)
    if not d:
        raise KeyError(platform)
    db.update("migration_decisions", "platform", platform, {"status": "approved", "decided_by": actor, "decided_at": now_iso()})
    db.audit(actor, "migration_decision_approved", "migration_decisions", platform, {"option": d["option"], "note": note})


def _engagement(b: dict) -> float | None:
    r, s = b.get("median_reach_per_post"), b.get("median_shares_per_post")
    if r and s is not None:
        return s / r
    return None


def decision_sheet(db: DB) -> list[dict]:
    """Per-platform evidence comparison + recommendation. Uses only imported analytics."""
    cfg = load_config()
    sheets = []
    for platform, d in cfg["platforms"].items():
        legacy = db.get("accounts", "account_id", d["legacy_account_id"])
        canon_candidates = [a for a in db.all("accounts", "canonical_brand_id=? AND platform=? AND brand_id=?",
                                              ["putt_tour", platform, "putt_tour"])]
        canon = canon_candidates[0] if canon_candidates else None
        bl = baseline(db, legacy["account_id"]) if legacy else {}
        bc = baseline(db, canon["account_id"]) if canon else None
        evidence = {"legacy": bl, "putt_tour": bc}
        missing = []
        if bl.get("followers") is None:
            missing.append(f"{legacy['username_original']} follower count")
        if canon and bc.get("followers") is None:
            missing.append(f"{canon['username_original']} follower count")
        if bl.get("median_reach_per_post") is None:
            missing.append(f"{legacy['username_original']} reach/post")
        checks = ["admin/ownership confirmed for every account",
                  "handle availability checked (target handle free or owned)",
                  "archive + backlinks + partner obligations catalogued",
                  "platform's current rename/merge policy re-read (no follower merge assumed)"]
        if canon:  # e.g. Instagram: both WPL and @putttour exist
            if missing:
                rec, conf = "bridge_to_canonical", "provisional — audit required"
                why = ("Evidence missing (" + ", ".join(missing) + "). Lowest-risk reversible default: keep @putttour as "
                       "canonical and use WPL as a 60–90-day bridge. Re-evaluate once analytics are imported.")
            else:
                ratio = bl["followers"] / max(1.0, bc["followers"])
                el, ec = _engagement(bl), _engagement(bc)
                if ratio >= 2.0 and (el is None or ec is None or el >= ec * 0.8):
                    rec, conf = "inplace_rebrand", "evidence-based"
                    why = (f"WPL has {ratio:.1f}x the followers of {canon['username_original']} with comparable engagement: "
                           "option A (repurpose WPL as Putt Tour) preserves more audience. Requires a handle plan, since "
                           f"{canon['username_original']} already holds the target handle.")
                else:
                    rec, conf = "bridge_to_canonical", "evidence-based"
                    why = (f"WPL/Putt Tour follower ratio {ratio:.1f}x does not justify an in-place rebrand; keep "
                           f"{canon['username_original']} canonical and bridge WPL (option B).")
        else:  # WPL-only platforms: TikTok, YouTube, X
            if bl.get("followers") is None:
                rec, conf = "keep_legacy_temporarily", "provisional — audit required"
                why = ("No Putt Tour account exists on this platform and no analytics are imported. Keep the WPL identity, "
                       "post Putt Tour content with transitional copy, and evaluate an in-place rebrand rather than "
                       "starting a competing fresh account.")
            elif bl["followers"] < 100 and not bl.get("posts_observed"):
                rec, conf = "redirect_to_new_account", "evidence-based (weak legacy account)"
                why = "Legacy account appears inactive with a negligible audience; a fresh Putt Tour account may be cleaner."
            else:
                rec, conf = "inplace_rebrand", "evidence-based"
                why = (f"Legacy account has {fmt(bl['followers'])} followers and history; rebranding in place keeps them. "
                       "A new account would start from zero.")
        sheets.append({"platform": platform, "legacy_account": legacy["account_id"] if legacy else None,
                       "legacy_handle": legacy["username_original"] if legacy else None,
                       "putt_tour_account": canon["account_id"] if canon else None,
                       "putt_tour_handle": canon["username_original"] if canon else "(none supplied — not presumed)",
                       "configured_option": d["option"], "status": (decision_for(db, platform) or {}).get("status", d["status"]),
                       "recommended_option": rec, "confidence": conf, "rationale": why, "missing_evidence": missing,
                       "pre_decision_checks": checks, "evidence": evidence,
                       "follower_transfer_assumed": False, "auto_rename": False, "auto_delete": False})
    return sheets


def utm(platform: str, account_id: str, content: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    params = urlencode({"utm_source": platform, "utm_medium": "social", "utm_campaign": cfg["campaign"],
                        "utm_content": f"{account_id.split('.')[-1]}_{content}"})
    base = cfg.get("link_hub_url") or ""
    return f"{base}?{params}" if base else ""


def utm_params(platform: str, account_id: str, content: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    return urlencode({"utm_source": platform, "utm_medium": "social", "utm_campaign": cfg["campaign"],
                      "utm_content": f"{account_id.split('.')[-1]}_{content}"})


def propose_profile_changes(db: DB, actor: str = "migration_planner") -> list[dict]:
    """Create PROPOSALS (never executed) for legacy WPL profiles. Idempotent per account+field."""
    cfg = load_config()
    out = []
    for platform, d in cfg["platforms"].items():
        legacy = db.get("accounts", "account_id", d["legacy_account_id"])
        canon = db.get("accounts", "account_id", d["canonical_account_id"])
        canon_handle = canon["username_original"] if canon and canon["brand_id"] == "putt_tour" else "Putt Tour"
        props = [("display_name", "Putt Tour (formerly World Putting League)" if d["option"] != "bridge_to_canonical"
                  else "World Putting League → now Putt Tour"),
                 ("bio", BIO_TEMPLATES[d["option"]].format(canonical=canon_handle)),
                 ("pinned_post", PINNED_TEMPLATE.format(
                     where=f"Follow {canon_handle} for everything new." if d["option"] == "bridge_to_canonical"
                     else "Same account, same players, new name.")),
                 ("link", "Tracked link hub: " + (utm(platform, legacy["account_id"], "bio", cfg) or
                                                  "(set link_hub_url in config/migration.json) ?" + utm_params(platform, legacy["account_id"], "bio", cfg)))]
        if d["option"] == "inplace_rebrand":
            props.append(("username", "Candidate Putt Tour handle — ONLY after availability check + sign-off; never automated"))
        for field, val in props:
            ex = db.one("SELECT id FROM profile_change_proposals WHERE account_id=? AND field=? AND status IN ('proposed','approved_for_manual_change')",
                        [legacy["account_id"], field])
            if ex:
                continue
            db.insert("profile_change_proposals", {"account_id": legacy["account_id"], "field": field,
                                                   "current_value": "(unknown — read from platform after OAuth)" if field != "username" else legacy["username_original"],
                                                   "proposed_value": val, "status": "proposed", "created_at": now_iso(),
                                                   "note": "Proposal only. A human admin applies it in the native app.", "reviewed_by": ""})
            out.append({"account_id": legacy["account_id"], "field": field, "proposed_value": val})
    if out:
        db.audit(actor, "profile_changes_proposed", "profile_change_proposals", "", {"count": len(out)})
    return out


def review_proposal(db: DB, proposal_id: int, decision: str, actor: str) -> None:
    if decision not in ("approved_for_manual_change", "rejected", "done_manually"):
        raise ValueError("decision must be approved_for_manual_change | rejected | done_manually")
    p = db.get("profile_change_proposals", "id", proposal_id)
    if not p:
        raise KeyError(proposal_id)
    plat = db.get("accounts", "account_id", p["account_id"])["platform"]
    d = decision_for(db, plat)
    if decision != "rejected" and (not d or d["status"] != "approved"):
        raise PermissionError(f"migration decision for {plat} must be approved before profile changes")
    db.update("profile_change_proposals", "id", proposal_id, {"status": decision, "reviewed_by": actor})
    db.audit(actor, f"profile_proposal_{decision}", "profile_change_proposals", str(proposal_id), {"field": p["field"]})


def ninety_day_plan(start: date | None = None) -> list[dict]:
    cfg = load_config()
    start = start or date.fromisoformat(cfg["planning_start_date"])
    D = lambda n: (start + timedelta(days=n)).isoformat()  # noqa: E731
    plan = [
        {"day": 0, "date": D(0), "phase": "audit", "task": "Confirm admin/ownership of all 4 WPL accounts + @putttour Instagram", "gate": "ownership confirmed"},
        {"day": 1, "date": D(1), "phase": "audit", "task": "Export platform-native analytics (followers, 30/90d reach, top posts, geo, watch-through, link clicks) → import CSV", "gate": None},
        {"day": 5, "date": D(5), "phase": "audit", "task": "Check handle availability; catalogue archives, backlinks, badges, partner obligations", "gate": None},
        {"day": 10, "date": D(10), "phase": "audit", "task": "Run decision sheet with real baselines; choose canonical account per platform", "gate": None},
        {"day": 14, "date": D(14), "phase": "audit", "task": "SIGN-OFF: migration decisions per platform (CLI: migration approve <platform>)", "gate": "APPROVAL CHECKPOINT 1"},
        {"day": 15, "date": D(15), "phase": "communicate", "task": "Admin applies approved display name/bio/banner/pinned proposals manually", "gate": "APPROVAL CHECKPOINT 2 (each proposal)"},
        {"day": 16, "date": D(16), "phase": "communicate", "task": "Announcement A — founder/tour story (all WPL accounts + @putttour, native edits)", "gate": "post approval"},
        {"day": 20, "date": D(20), "phase": "communicate", "task": "Announcement B — athlete-led (releases required)", "gate": "post approval"},
        {"day": 25, "date": D(25), "phase": "communicate", "task": "Announcement C — fast rules + highlights", "gate": "post approval"},
        {"day": 30, "date": D(30), "phase": "communicate", "task": "Website/link-hub updated with UTM-tagged migration links", "gate": None},
        {"day": 31, "date": D(31), "phase": "funnel", "task": f"WPL Instagram bridge stream ≤{cfg['legacy_bridge_max_posts_per_week']}/week teasers → @putttour; originals first on canonical", "gate": None},
        {"day": 45, "date": D(45), "phase": "funnel", "task": "Mid-point review: hook variants, legacy response, reply to genuine questions", "gate": "APPROVAL CHECKPOINT 3"},
        {"day": 60, "date": D(60), "phase": "funnel", "task": "Evaluate in-place rebrand for WPL-only platforms (TikTok/YouTube/X)", "gate": None},
        {"day": 75, "date": D(75), "phase": "consolidate", "task": "Baseline vs outcome comparison per platform (migration proxies)", "gate": None},
        {"day": 90, "date": D(90), "phase": "consolidate", "task": "Decide signpost vs in-place for each legacy account; NO deletion without backup + rights review + explicit authorization", "gate": "APPROVAL CHECKPOINT 4"},
    ]
    for w in range(13):
        plan.append({"day": w * 7 + 6, "date": D(w * 7 + 6), "phase": "report", "task": f"Weekly migration report #{w + 1}", "gate": None})
    return sorted(plan, key=lambda x: x["day"])


def announcement_variants(platform: str) -> list[dict]:
    native = {"instagram": "Reel + pinned to grid (up to 3 pins)", "tiktok": "native vertical + pin to profile",
              "youtube": "Short + community post + channel trailer update", "x": "post + pin to profile"}
    return [{"key": k, **v, "platform": platform, "format": native.get(platform, "native"),
             "requires": "post approval; athlete releases for athlete_led"} for k, v in ANNOUNCEMENTS.items()]


def weekly_report(db: DB) -> dict:
    """Migration proxies. Individual follower movement can't be measured; we report what can be."""
    cfg = load_config()
    rows = []
    for platform, d in cfg["platforms"].items():
        legacy = baseline(db, d["legacy_account_id"])
        canon_id = d["canonical_account_id"]
        canon = baseline(db, canon_id) if canon_id != d["legacy_account_id"] else None
        clicks = db.query("SELECT SUM(value) AS v FROM metrics WHERE account_id=? AND metric='migration_link_clicks'",
                          [d["legacy_account_id"]])[0]["v"]
        rows.append({
            "platform": platform, "option": d["option"],
            "legacy_followers": legacy["followers"], "canonical_followers": canon["followers"] if canon else None,
            "combined_followers": (legacy["followers"] + canon["followers"]) if canon and None not in (legacy["followers"], canon["followers"]) else None,
            "legacy_median_reach": legacy["median_reach_per_post"],
            "migration_link_clicks_from_legacy": clicks,
            "note": "Combined followers is NOT unique people (overlap unknown). Follower transfer is not measurable; "
                    "use link clicks, canonical follows during bridge posts, and retained reach as proxies.",
        })
    return {"campaign": cfg["campaign"], "generated_at": now_iso(), "platforms": rows}
