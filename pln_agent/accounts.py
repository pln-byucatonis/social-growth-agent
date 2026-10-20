"""Account portfolio: load the 28 user-supplied handles verbatim, classify roles, gate publishing.

Brand IDs and account IDs are separate. Handles are kept exactly as supplied (`username_original`).
Nothing in the CSV is treated as verified; verification comes only from config/account_state.json or
an audited CLI/dashboard action after a human confirms ownership / connects OAuth.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR, Settings
from .db import DB, now_iso

PLATFORMS = {"instagram", "facebook", "x", "tiktok", "youtube", "twitch", "linkedin"}
CORE_PLATFORMS = ["linkedin", "facebook", "instagram", "tiktok"]
EXTENDED_PLATFORMS = ["youtube", "x", "twitch"]

VERIFICATION_ORDER = ["unverified", "ownership_confirmed", "oauth_connected"]


def load_brands() -> list[dict]:
    return json.loads((CONFIG_DIR / "brands.json").read_text())["brands"]


def brand_by_name() -> dict[str, dict]:
    out = {}
    for b in load_brands():
        for alias in [b["name"], *b.get("aliases", [])]:
            out[alias.lower()] = b
    return out


def resolve_brand(name: str) -> dict:
    b = brand_by_name().get((name or "").strip().lower())
    if not b:
        raise KeyError(f"unknown brand {name!r}; add it to config/brands.json")
    return b


def canonical_brand_id(brand: dict) -> str:
    return brand.get("legacy_of") or brand["brand_id"]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def normalize_username(u: str) -> str:
    return (u or "").strip().lstrip("@").strip().lower()


def classify(row: dict, brand: dict) -> tuple[str, str]:
    """Return (role, hard_block_reason). Hard blocks require explicit human clearance."""
    platform = row["platform"].lower()
    uname = normalize_username(row["username"])
    if brand.get("is_legacy"):
        return "legacy_bridge", ""
    if brand["brand_id"] == "pln" and platform == "tiktok" and uname != "proleaguenetwork":
        return ("secondary_unverified",
                "Ambiguous secondary PLN TikTok: confirm owner, purpose and correct spelling before any posting")
    if brand["brand_id"] == "world_footgolf_tour":
        return ("creator_unverified",
                "Possibly a creator-owned account: confirm brand association, ownership and rights first")
    return "canonical", ""


@dataclass
class InventoryReport:
    supplied_rows: int
    loaded: int
    placeholders: int
    ids: list[str]
    warnings: list[str]


def read_inventory_csv(path: Path | None = None) -> list[dict]:
    path = path or CONFIG_DIR / "accounts.csv"
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_accounts(db: DB, path: Path | None = None, actor: str = "system") -> InventoryReport:
    rows = read_inventory_csv(path)
    state = json.loads((CONFIG_DIR / "account_state.json").read_text()).get("accounts", {})
    ids: list[str] = []
    warnings: list[str] = []
    seen_keys: dict[tuple[str, str], str] = {}
    for i, row in enumerate(rows, start=2):  # csv line numbers (header = 1)
        brand = resolve_brand(row["brand"])
        platform = row["platform"].strip().lower()
        if platform not in PLATFORMS:
            raise ValueError(f"row {i}: unknown platform {row['platform']!r}")
        role, block = classify(row, brand)
        uname_norm = normalize_username(row["username"])
        base = f"{brand['brand_id']}.{platform}.{_slug(uname_norm)}"
        acc_id, n = base, 2
        while acc_id in ids:  # never silently dedupe: suffix instead
            acc_id, n = f"{base}_{n}", n + 1
        key = (platform, uname_norm)
        if key in seen_keys:
            warnings.append(f"row {i}: {row['username']} on {platform} also supplied as {seen_keys[key]}; kept both")
        seen_keys[key] = acc_id
        notes = row.get("notes", "") or ""
        if platform == "facebook" and " " in row["username"]:
            notes = (notes + " | " if notes else "") + "Looks like a Page display name, not a handle: resolve Page ID via OAuth"
        rec = {
            "account_id": acc_id, "brand_id": brand["brand_id"], "canonical_brand_id": canonical_brand_id(brand),
            "source_brand_name": row["brand"], "platform": platform, "username_original": row["username"],
            "username_normalized": uname_norm, "stable_platform_id": "", "priority": row.get("priority", ""),
            "role": role, "supplied_status": row.get("account_status", ""), "verification_status": "unverified",
            "ownership_owner": "", "credential_ref": "", "publishing_authorized": False,
            "publishing_policy": "approval_required", "hard_block_reason": block,
            "redirect_to_account_id": "", "campaign": "", "notes": notes, "source": "user_csv", "csv_row": i,
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        existing = db.get("accounts", "account_id", acc_id)
        if existing:  # preserve human-verified fields across reloads
            for k in ("verification_status", "ownership_owner", "credential_ref", "publishing_authorized",
                      "stable_platform_id", "publishing_policy", "redirect_to_account_id", "campaign", "created_at"):
                rec[k] = existing[k]
            if existing.get("hard_block_reason", "") == "" and block:
                rec["hard_block_reason"] = ""  # already cleared by a human
        rec.update({k: v for k, v in state.get(acc_id, {}).items() if k in rec and k not in ("account_id", "username_original")})
        db.insert("accounts", rec, upsert_key="account_id")
        ids.append(acc_id)

    # PLN LinkedIn company page: strategically required, but NO handle/page id was supplied.
    li = {
        "account_id": "pln.linkedin.pending", "brand_id": "pln", "canonical_brand_id": "pln",
        "source_brand_name": "Pro League Network", "platform": "linkedin", "username_original": "",
        "username_normalized": "", "stable_platform_id": "", "priority": "P0", "role": "pending_placeholder",
        "supplied_status": "not_supplied", "verification_status": "unverified", "ownership_owner": "",
        "credential_ref": "", "publishing_authorized": False, "publishing_policy": "approval_required",
        "hard_block_reason": "No LinkedIn company Page ID supplied: connect via OAuth with Page admin role first",
        "redirect_to_account_id": "", "campaign": "", "source": "system_placeholder", "csv_row": None,
        "notes": "Planning-only placeholder. Handle intentionally left blank (not fabricated).",
        "created_at": now_iso(), "updated_at": now_iso(),
    }
    existing = db.get("accounts", "account_id", li["account_id"])
    if existing:
        for k in ("verification_status", "credential_ref", "publishing_authorized", "stable_platform_id",
                  "username_original", "username_normalized", "hard_block_reason", "created_at"):
            li[k] = existing[k]
    li.update({k: v for k, v in state.get(li["account_id"], {}).items() if k in li})
    db.insert("accounts", li, upsert_key="account_id")
    db.audit(actor, "inventory_loaded", "accounts", "", {"supplied_rows": len(rows), "warnings": warnings})
    return InventoryReport(len(rows), len(ids), 1, ids, warnings)


def list_accounts(db: DB, **filters) -> list[dict]:
    where, params = [], []
    for k, v in filters.items():
        where.append(f"{k}=?")
        params.append(v)
    return db.all("accounts", " AND ".join(where), params, order="canonical_brand_id, platform, account_id")


# --- verification lifecycle (audited, human-initiated) ------------------------------------------

def set_verification(db: DB, account_id: str, status: str, actor: str, owner: str = "", note: str = "") -> dict:
    if status not in VERIFICATION_ORDER:
        raise ValueError(f"status must be one of {VERIFICATION_ORDER}")
    acc = _require(db, account_id)
    db.update("accounts", "account_id", account_id,
              {"verification_status": status, "ownership_owner": owner or acc["ownership_owner"], "updated_at": now_iso()})
    db.audit(actor, "account_verification_changed", "accounts", account_id,
             {"from": acc["verification_status"], "to": status, "owner": owner, "note": note})
    return db.get("accounts", "account_id", account_id)


def clear_hard_block(db: DB, account_id: str, actor: str, justification: str) -> None:
    acc = _require(db, account_id)
    if acc["verification_status"] == "unverified":
        raise PermissionError("confirm ownership before clearing a hard block")
    if not justification.strip():
        raise ValueError("justification required")
    db.update("accounts", "account_id", account_id, {"hard_block_reason": "", "updated_at": now_iso()})
    db.audit(actor, "account_hard_block_cleared", "accounts", account_id,
             {"previous_reason": acc["hard_block_reason"], "justification": justification})


def connect_credentials(db: DB, account_id: str, credential_ref: str, stable_platform_id: str, actor: str) -> None:
    """Record that OAuth was completed. Only a *reference* (env var / vault key) is stored."""
    if not re.match(r"^(env|vault):[A-Za-z0-9_./-]+$", credential_ref):
        raise ValueError("credential_ref must look like env:NAME or vault:path — never paste a raw token")
    _require(db, account_id)
    db.update("accounts", "account_id", account_id, {"credential_ref": credential_ref, "stable_platform_id": stable_platform_id,
                                                     "verification_status": "oauth_connected", "updated_at": now_iso()})
    db.audit(actor, "account_credentials_connected", "accounts", account_id,
             {"credential_ref": credential_ref, "stable_platform_id": stable_platform_id})


def authorize_publishing(db: DB, account_id: str, actor: str, enabled: bool = True) -> None:
    acc = _require(db, account_id)
    if enabled and acc["verification_status"] != "oauth_connected":
        raise PermissionError("account must be oauth_connected before publishing can be authorized")
    if enabled and acc["hard_block_reason"]:
        raise PermissionError(f"hard block: {acc['hard_block_reason']}")
    db.update("accounts", "account_id", account_id, {"publishing_authorized": enabled, "updated_at": now_iso()})
    db.audit(actor, "account_publishing_authorized" if enabled else "account_publishing_revoked", "accounts", account_id)


def _require(db: DB, account_id: str) -> dict:
    acc = db.get("accounts", "account_id", account_id)
    if not acc:
        raise KeyError(account_id)
    return acc


def publish_eligibility(acc: dict, settings: Settings, kill_switch: bool) -> tuple[bool, list[str]]:
    """Can a LIVE publish go to this account right now? Returns (ok, blocking reasons)."""
    reasons = []
    if kill_switch:
        reasons.append("emergency stop is ON")
    if settings.dry_run:
        reasons.append("global dry-run is ON (PLN_DRY_RUN)")
    if not settings.publishing_enabled:
        reasons.append("global publishing disabled (PLN_PUBLISHING_ENABLED)")
    if acc["hard_block_reason"]:
        reasons.append(f"hard block: {acc['hard_block_reason']}")
    if acc["verification_status"] != "oauth_connected":
        reasons.append(f"account is {acc['verification_status']}, not oauth_connected")
    if not acc["credential_ref"]:
        reasons.append("no credential reference configured")
    if not acc["publishing_authorized"]:
        reasons.append("publishing not authorized for this account")
    return (not reasons), reasons


def planning_eligible(acc: dict) -> bool:
    """Accounts may be PLANNED for even when unverified (drafts only); creator/secondary accounts are excluded
    from routing entirely until ownership is confirmed, so we don't waste creative on them."""
    if acc["role"] in ("secondary_unverified", "creator_unverified") and acc["hard_block_reason"]:
        return False
    return True
