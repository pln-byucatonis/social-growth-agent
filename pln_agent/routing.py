"""Asset → account routing. Canonical brand first, then selective PLN amplification, then legacy bridges.

One source asset yields a *different* native edit + copy per destination. A destination is only eligible if:
the platform has an account for that brand in the inventory (never presumed), the account isn't
hard-blocked from planning, and the route role allows it (e.g. WPL Instagram = teaser bridge only).
"""
from __future__ import annotations

from dataclasses import dataclass

from .accounts import planning_eligible
from .db import DB
from .migration import ROUTE_ROLE, decision_for, utm


@dataclass
class Route:
    account: dict
    role: str  # canonical | pln_amplification | pln_b2b | legacy_bridge | legacy_inplace
    reason: str
    priority: int  # lower = first


def _display_brand(acc: dict, role: str, brands: dict) -> str:
    if role in ("legacy_inplace",):
        return "Putt Tour (formerly World Putting League)"
    if role == "legacy_bridge":
        return "Putt Tour"
    return brands.get(acc["canonical_brand_id"], {}).get("name", acc["canonical_brand_id"])


def routes_for_asset(db: DB, asset: dict, include_extended: bool = True) -> tuple[list[Route], list[str]]:
    brand_id = asset["brand_id"]
    accounts = db.all("accounts")
    skipped: list[str] = []
    routes: list[Route] = []
    core = {"linkedin", "facebook", "instagram", "tiktok"}

    def ok_platform(p: str) -> bool:
        return include_extended or p in core

    # 1) canonical brand accounts
    for a in accounts:
        if a["canonical_brand_id"] != brand_id or not ok_platform(a["platform"]):
            continue
        if not planning_eligible(a):
            skipped.append(f"{a['account_id']}: excluded from routing ({a['hard_block_reason']})")
            continue
        if a["brand_id"] == "world_putting_league":
            d = decision_for(db, a["platform"])
            role = ROUTE_ROLE[d["option"]] if d else "legacy_bridge"
            routes.append(Route(a, role, f"WPL legacy account; migration option '{d['option'] if d else 'none'}'",
                                2 if role == "legacy_bridge" else 0))
        elif a["role"] == "pending_placeholder" and brand_id != "pln":
            continue
        else:
            routes.append(Route(a, "canonical", "canonical brand account", 0))

    # 2) PLN amplification (different angle) + PLN LinkedIn B2B for every property — except gambling-adjacent brands
    #    (PREDCT / PLN Picks): market content stays on its own accounts, never on the network's
    if brand_id != "pln" and not (asset.get("flags") or {}).get("gambling"):
        for a in accounts:
            if a["brand_id"] != "pln" or not ok_platform(a["platform"]) or not planning_eligible(a):
                if a["brand_id"] == "pln" and not planning_eligible(a):
                    skipped.append(f"{a['account_id']}: excluded from routing ({a['hard_block_reason']})")
                continue
            if a["platform"] == "linkedin":
                routes.append(Route(a, "pln_b2b", "PLN company page: B2B framing of a property moment", 1))
            elif a["platform"] in ("instagram", "tiktok", "facebook", "youtube"):
                routes.append(Route(a, "pln_amplification", "selective PLN amplification (gated by score + spacing)", 1))
            # X/Twitch: PLN amplifies live/event context only; not per-clip
    else:
        pass

    # de-dup skipped messages
    skipped = sorted(set(skipped))
    # brands with no account on a platform are simply not routed (never presumed)
    return sorted(routes, key=lambda r: (r.priority, r.account["platform"])), skipped


def canonical_handle(db: DB, brand_id: str, platform: str) -> str:
    rows = db.all("accounts", "brand_id=? AND platform=? AND role='canonical'", [brand_id, platform])
    if rows:
        return rows[0]["username_original"]
    rows = db.all("accounts", "brand_id=? AND role='canonical'", [brand_id], order="platform")
    ig = [r for r in rows if r["platform"] == "instagram"]
    return (ig or rows or [{"username_original": ""}])[0]["username_original"]


def route_context_extras(db: DB, asset: dict, route: Route, brands: dict) -> dict:
    p = route.account["platform"]
    if route.role == "legacy_bridge":
        handle = canonical_handle(db, "putt_tour", p) or canonical_handle(db, "putt_tour", "instagram")
    elif route.role in ("pln_amplification", "pln_b2b"):
        handle = canonical_handle(db, asset["brand_id"], p)
    else:
        handle = route.account["username_original"]
    link = ""
    if asset["brand_id"] == "putt_tour" and route.account["brand_id"] in ("world_putting_league", "putt_tour"):
        link = utm(p, route.account["account_id"], asset["asset_id"])
    return {"display_brand": _display_brand(route.account, route.role, brands), "canonical_handle": handle, "utm_url": link}
