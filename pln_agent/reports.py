"""Platform- and brand-specific reporting. Platform metric definitions are kept separate."""
from __future__ import annotations

from .accounts import publish_eligibility
from .config import Settings
from .db import DB
from .metrics import NORTH_STAR, baseline, fmt
from .workflow import kill_switch_on


def portfolio_report(db: DB, settings: Settings) -> dict:
    ks = kill_switch_on(db)
    rows = []
    for a in db.all("accounts", order="canonical_brand_id, platform"):
        ok, reasons = publish_eligibility(a, settings, ks)
        counts = {r["state"]: r["n"] for r in db.query("SELECT state, COUNT(*) AS n FROM variants WHERE account_id=? GROUP BY state",
                                                        [a["account_id"]])}
        b = baseline(db, a["account_id"])
        rows.append({"account_id": a["account_id"], "brand": a["brand_id"], "canonical_brand": a["canonical_brand_id"],
                     "platform": a["platform"], "handle": a["username_original"] or "(pending — not supplied)",
                     "role": a["role"], "verification": a["verification_status"], "live_publish_ok": ok,
                     "blocking": reasons, "variants": counts, "followers": fmt(b["followers"]),
                     "north_star": NORTH_STAR.get(a["platform"], {}).get("label"),
                     "median_reach": fmt(b["median_reach_per_post"])})
    return {"kill_switch": ks, "dry_run": settings.dry_run, "publishing_enabled": settings.publishing_enabled, "accounts": rows}


def platform_report_text(db: DB, settings: Settings) -> str:
    rep = portfolio_report(db, settings)
    out = [f"PLATFORM REPORT  dry_run={rep['dry_run']} publishing_enabled={rep['publishing_enabled']} "
           f"emergency_stop={'ON' if rep['kill_switch'] else 'off'}", ""]
    by_p: dict[str, list[dict]] = {}
    for r in rep["accounts"]:
        by_p.setdefault(r["platform"], []).append(r)
    for p in ["linkedin", "facebook", "instagram", "tiktok", "youtube", "x", "twitch"]:
        if p not in by_p:
            continue
        out.append(f"== {p.upper()} — north star: {NORTH_STAR[p]['label']} (not comparable across platforms)")
        for r in by_p[p]:
            v = r["variants"]
            out.append(f"  {r['handle']:<26} {r['brand']:<22} {r['role']:<20} followers={r['followers']:<8} "
                       f"review={v.get('in_review', 0)} fix={v.get('needs_fix', 0)} appr={v.get('approved', 0)} "
                       f"sched={v.get('scheduled', 0)} sim={v.get('simulated', 0)} pub={v.get('published', 0)} "
                       f"live={'YES' if r['live_publish_ok'] else 'no'}")
        out.append("")
    return "\n".join(out)
