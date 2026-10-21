"""Approval workflow, scheduling, and the publish gate/publisher.

Variant states:  needs_fix → in_review → approved → scheduled → (publishing) → published | simulated | failed
                 in_review → rejected;  any pre-publish state → cancelled.  Editing text drops back to in_review.
Approvals bind to the exact caption hash; sensitive content requires two distinct reviewers.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from .accounts import publish_eligibility
from .adapters import AdapterError, get_adapter
from .config import CONFIG_DIR, Settings
from .db import DB, now_iso
from .rights import evaluate
from .scoring import allocation
from .strategists import STRATEGISTS, CreativeContext, caption_hash

EDITABLE = {"hook", "caption", "title", "hashtags", "cta", "voiceover"}
PRE_PUBLISH = {"needs_fix", "in_review", "approved", "scheduled"}


class WorkflowError(Exception):
    pass


def kill_switch_on(db: DB) -> bool:
    return db.kv_get("kill_switch", "off") == "on"


def set_kill_switch(db: DB, on: bool, actor: str, reason: str = "") -> None:
    db.kv_set("kill_switch", "on" if on else "off", actor)
    db.audit(actor, "emergency_stop_" + ("on" if on else "off"), "settings_kv", "kill_switch", {"reason": reason})
    if on:  # pause everything not yet done; jobs keep their state history
        for j in db.all("publish_jobs", "state IN ('queued','publishing')"):
            db.update("publish_jobs", "job_id", j["job_id"], {"state": "halted", "last_error": "emergency stop", "updated_at": now_iso()})


def _variant(db: DB, vid: str) -> dict:
    v = db.get("variants", "variant_id", vid)
    if not v:
        raise WorkflowError(f"unknown variant {vid}")
    return v


def revalidate(db: DB, vid: str) -> dict:
    v = _variant(db, vid)
    asset = db.get("assets", "asset_id", v["asset_id"])
    acc = db.get("accounts", "account_id", v["account_id"])
    strat = STRATEGISTS[v["platform"]]
    ctx = CreativeContext(asset=asset, account=acc, brand={}, platform=v["platform"], route_role=v["route_role"],
                          display_brand="", canonical_handle="", utm_url=v.get("utm_url") or "")
    # rebuild grounding with the same extras the generator used
    from .accounts import load_brands
    from .routing import Route, route_context_extras
    brands = {b["brand_id"]: b for b in load_brands()}
    ex = route_context_extras(db, asset, Route(acc, v["route_role"], "", 0), brands)
    ctx.display_brand, ctx.canonical_handle = ex["display_brand"], ex["canonical_handle"]
    errs = strat.validate(v, ctx)
    rights = evaluate(asset).as_dict()
    return {"errors": errs, "rights": rights}


def refresh_validation(db: DB, vid: str) -> str:
    """Re-run the platform checks after the video changed (e.g. a new cut); needs_fix <-> in_review follows."""
    v = _variant(db, vid)
    if v["state"] not in ("in_review", "needs_fix"):
        return v["state"]
    res = revalidate(db, vid)
    state = "needs_fix" if (res["errors"] or res["rights"]["blockers"]) else "in_review"
    val = dict(v["validation"] or {})
    val["errors"] = res["errors"]
    db.update("variants", "variant_id", vid, {"state": state, "validation": val, "risk_flags": res["rights"], "updated_at": now_iso()})
    return state


def edit_variant(db: DB, vid: str, changes: dict, actor: str) -> dict:
    v = _variant(db, vid)
    if v["state"] not in PRE_PUBLISH - {"scheduled"}:
        raise WorkflowError(f"cannot edit a variant in state {v['state']}")
    bad = set(changes) - EDITABLE
    if bad:
        raise WorkflowError(f"not editable: {bad}")
    before = {k: v.get(k) for k in changes}
    upd = dict(changes)
    if "caption" in upd:
        upd["caption_hash"] = caption_hash(upd["caption"])
    db.update("variants", "variant_id", vid, {**upd, "updated_at": now_iso()})
    res = revalidate(db, vid)
    state = "needs_fix" if (res["errors"] or res["rights"]["blockers"]) else "in_review"
    val = v["validation"] or {}
    val["errors"] = res["errors"]
    db.update("variants", "variant_id", vid, {"state": state, "validation": val, "risk_flags": res["rights"]})
    if v["state"] == "approved":
        db.insert("approvals", {"variant_id": vid, "decision": "revoke", "reviewer": actor, "note": "edited after approval",
                                "caption_hash": v["caption_hash"], "at": now_iso()})
    db.audit(actor, "variant_edited", "variants", vid, {"before": before, "after": changes, "new_state": state})
    return _variant(db, vid)


def _current_approvers(db: DB, v: dict) -> set[str]:
    approvers: set[str] = set()
    for a in db.all("approvals", "variant_id=?", [v["variant_id"]], order="id"):
        if a["decision"] in ("revoke", "reject", "request_changes"):
            approvers = set()
        elif a["decision"] == "approve" and a["caption_hash"] == v["caption_hash"]:
            approvers.add(a["reviewer"])
    return approvers


def approve(db: DB, vid: str, reviewer: str, note: str = "") -> dict:
    v = _variant(db, vid)
    if v["state"] not in ("in_review", "approved"):
        raise WorkflowError(f"cannot approve from state {v['state']} (fix validation/rights first)")
    res = revalidate(db, vid)
    if res["errors"]:
        raise WorkflowError("validation errors: " + "; ".join(res["errors"]))
    if res["rights"]["blockers"]:
        raise WorkflowError("rights blockers: " + "; ".join(res["rights"]["blockers"]))
    if reviewer.startswith("policy:") and res["rights"]["review_flags"]:
        raise WorkflowError("policy auto-approval is never allowed for flagged content")
    db.insert("approvals", {"variant_id": vid, "decision": "approve", "reviewer": reviewer, "note": note,
                            "caption_hash": v["caption_hash"], "at": now_iso()})
    approvers = _current_approvers(db, v)
    needed = 2 if res["rights"]["two_person_signoff"] else 1
    new_state = "approved" if len(approvers) >= needed else "in_review"
    db.update("variants", "variant_id", vid, {"state": new_state, "updated_at": now_iso()})
    db.audit(reviewer, "variant_approval", "variants", vid,
             {"approvers": sorted(approvers), "needed": needed, "state": new_state, "note": note})
    return {"state": new_state, "approvers": sorted(approvers), "needed": needed}


def reject(db: DB, vid: str, reviewer: str, note: str) -> None:
    v = _variant(db, vid)
    if v["state"] not in ("in_review", "approved", "needs_fix"):
        raise WorkflowError(f"cannot reject from state {v['state']}")
    db.insert("approvals", {"variant_id": vid, "decision": "reject", "reviewer": reviewer, "note": note,
                            "caption_hash": v["caption_hash"], "at": now_iso()})
    db.update("variants", "variant_id", vid, {"state": "rejected", "updated_at": now_iso()})
    db.audit(reviewer, "variant_rejected", "variants", vid, {"note": note})


BULK_MAX = 100


def bulk_review(db: DB, picks: list[tuple[str, str]], reviewer: str, decision: str, note: str = "") -> dict:
    """Approve or reject several posts at once from the queue. Each pick carries the caption hash the
    reviewer saw: a post whose copy changed since the page loaded is skipped (approval binds to copy).
    Posts needing two-person sign-off are never bulk-approved; they need their own review page."""
    if decision not in ("approve", "reject"):
        raise WorkflowError(f"unknown bulk decision {decision!r}")
    if decision == "reject" and not note.strip():
        raise WorkflowError("a note is required to reject")
    if len(picks) > BULK_MAX:
        raise WorkflowError(f"select at most {BULK_MAX} posts per bulk action")
    done, skipped = [], {}
    for vid, seen_hash in picks:
        v = db.get("variants", "variant_id", vid)
        if not v:
            skipped[vid] = "not found"
            continue
        if seen_hash and v["caption_hash"] != seen_hash:
            skipped[vid] = "copy changed since you loaded the queue; review it again"
            continue
        try:
            if decision == "approve":
                asset = db.get("assets", "asset_id", v["asset_id"])
                if evaluate(asset).as_dict()["two_person_signoff"]:
                    skipped[vid] = "sensitive content: needs two-person review on its own page"
                    continue
                approve(db, vid, reviewer, note or "bulk approval from queue")
            else:
                reject(db, vid, reviewer, note)
            done.append(vid)
        except WorkflowError as e:
            skipped[vid] = str(e)
    db.audit(reviewer, f"bulk_{decision}", "variants", "", {"done": done, "skipped": skipped, "note": note})
    return {"done": done, "skipped": skipped}


def idempotency_key(v: dict) -> str:
    return hashlib.sha256(f"{v['account_id']}|{v['asset_id']}|{v['caption_hash']}".encode()).hexdigest()[:32]


def schedule(db: DB, vid: str, slot_at: datetime, actor: str) -> dict:
    v = _variant(db, vid)
    if v["state"] != "approved":
        raise WorkflowError(f"only approved variants can be scheduled (state={v['state']})")
    key = idempotency_key(v)
    if db.one("SELECT job_id FROM publish_jobs WHERE idempotency_key=? AND state NOT IN ('cancelled')", [key]):
        raise WorkflowError("duplicate: an identical post for this account already has a publish job")
    dup = db.one("SELECT job_id FROM publish_jobs j WHERE j.account_id=? AND j.state NOT IN ('cancelled') AND "
                 "j.variant_id IN (SELECT variant_id FROM variants WHERE asset_id=?)", [v["account_id"], v["asset_id"]])
    if dup:
        raise WorkflowError("duplicate: this asset is already queued/published on this account")
    recent = db.one("SELECT j.job_id FROM publish_jobs j JOIN variants x ON x.variant_id=j.variant_id WHERE j.account_id=? "
                    "AND x.caption_hash=? AND j.state NOT IN ('cancelled')", [v["account_id"], v["caption_hash"]])
    if recent:
        raise WorkflowError("duplicate: identical caption already used on this account")
    job = {"job_id": f"job-{uuid.uuid4().hex[:10]}", "variant_id": vid, "account_id": v["account_id"],
           "idempotency_key": key, "state": "queued", "mode": "pending", "attempts": 0,
           "next_attempt_at": slot_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
           "created_at": now_iso(), "updated_at": now_iso()}
    db.insert("publish_jobs", job)
    db.insert("schedules", {"schedule_id": f"sch-{uuid.uuid4().hex[:10]}", "variant_id": vid, "account_id": v["account_id"],
                            "slot_at": job["next_attempt_at"], "state": "scheduled", "reason": "approved + scheduled",
                            "created_at": now_iso()})
    db.update("variants", "variant_id", vid, {"state": "scheduled", "updated_at": now_iso()})
    db.audit(actor, "variant_scheduled", "variants", vid, {"job_id": job["job_id"], "slot_at": job["next_attempt_at"]})
    return job


def cancel(db: DB, vid: str, actor: str, reason: str) -> None:
    v = _variant(db, vid)
    for j in db.all("publish_jobs", "variant_id=? AND state IN ('queued','halted')", [vid]):
        db.update("publish_jobs", "job_id", j["job_id"], {"state": "cancelled", "updated_at": now_iso()})
    db.update("variants", "variant_id", vid, {"state": "cancelled" if v["state"] != "published" else v["state"]})
    db.audit(actor, "variant_cancelled", "variants", vid, {"reason": reason})


def _published_today(db: DB, account_id: str, now: datetime) -> int:
    start = datetime.combine(now.astimezone(timezone.utc).date(), datetime.min.time(), timezone.utc)
    r = db.one("SELECT COUNT(*) AS n FROM publish_jobs WHERE account_id=? AND state IN ('published','simulated','publishing') "
               "AND updated_at>=? AND updated_at<?", [account_id, start.isoformat(), (start + timedelta(days=1)).isoformat()])
    return r["n"] if r else 0


def run_publisher(db: DB, settings: Settings, now: datetime | None = None, transport=None, actor: str = "publisher") -> list[dict]:
    """Process due jobs once. Safe to run repeatedly (cron/worker loop)."""
    now = now or datetime.now(timezone.utc)
    results = []
    if kill_switch_on(db):
        db.audit(actor, "publisher_skipped", "publish_jobs", "", {"reason": "emergency stop"})
        return [{"skipped": "emergency stop is ON"}]
    recover_stuck_jobs(db, settings, now, transport)
    caps = allocation()["max_posts_per_account_per_day"]
    due = db.all("publish_jobs", "state='queued' AND next_attempt_at<=?",
                 [now.astimezone(timezone.utc).replace(microsecond=0).isoformat()], order="next_attempt_at")
    for job in due:
        if kill_switch_on(db):
            break
        v = db.get("variants", "variant_id", job["variant_id"])
        acc = db.get("accounts", "account_id", job["account_id"])
        asset = db.get("assets", "asset_id", v["asset_id"])
        approvers = _current_approvers(db, v)
        rights = evaluate(asset, at=now, require_media=v["platform"] != "twitch")
        problems = []
        if settings.require_approval and not approvers:
            problems.append("no valid approval for current caption")
        if v["state"] != "scheduled":
            problems.append(f"variant state is {v['state']}")
        if rights.blockers:
            problems.append("rights: " + "; ".join(rights.blockers))
        qc = v.get("render_qc") or {}
        if qc and not qc.get("passed", True):
            problems.append("render failed quality checks: " + "; ".join(qc.get("errors", [])))
        if _published_today(db, acc["account_id"], now) >= caps.get(acc["platform"], 1):
            db.update("publish_jobs", "job_id", job["job_id"], {"next_attempt_at": (now + timedelta(hours=12)).replace(microsecond=0).isoformat(),
                                                               "last_error": "daily rate cap reached; deferred", "updated_at": now_iso()})
            results.append({"job_id": job["job_id"], "state": "deferred", "reason": "rate cap"})
            continue
        if problems:
            _finish(db, job, v, "blocked", {"problems": problems}, "; ".join(problems))
            results.append({"job_id": job["job_id"], "state": "blocked", "reason": problems})
            continue
        live_ok, reasons = publish_eligibility(acc, settings, kill_switch_on(db))
        adapter = get_adapter(acc["platform"], transport=transport, settings=settings)
        if not live_ok:
            res = adapter.simulate(v, acc, job["idempotency_key"])
            db.update("publish_jobs", "job_id", job["job_id"], {"mode": "dry_run"})
            _finish(db, job, v, "simulated", {**res.raw, "why_not_live": reasons}, "", res)
            results.append({"job_id": job["job_id"], "state": "simulated", "why_not_live": reasons})
            continue
        # LIVE path — make sure pull-from-URL platforms have the approved render's public link
        if acc["platform"] in ("instagram", "facebook", "tiktok") and not v.get("public_media_url"):
            from .public_media import PublicMediaError, publish_render
            try:
                v["public_media_url"] = publish_render(db, v["variant_id"], actor)
            except PublicMediaError as e:
                _finish(db, job, v, "blocked", {"problems": [str(e)]}, str(e))
                results.append({"job_id": job["job_id"], "state": "blocked", "reason": [str(e)]})
                continue
        # LIVE path — lock, attempt, record
        db.update("publish_jobs", "job_id", job["job_id"], {"state": "publishing", "mode": "live", "locked_at": now_iso(),
                                                           "attempts": job["attempts"] + 1})
        try:
            res = adapter.publish(v, acc, job["idempotency_key"])
            _attempt(db, job["job_id"], "ok", None, res.raw)
            _finish(db, job, v, "published", res.raw, "", res)
            if res.platform_post_id:  # Creative Intelligence DB: the agent's decisions + where it went
                from .creative_db import upsert_agent_post
                upsert_agent_post(db, v, res.platform_post_id, res.permalink, now.isoformat(), settings.timezone)
            results.append({"job_id": job["job_id"], "state": "published", "permalink": res.permalink})
        except AdapterError as e:
            _attempt(db, job["job_id"], "error", e, e.response)
            attempts = job["attempts"] + 1
            if e.retryable and attempts < settings.max_publish_attempts:
                delay = settings.backoff_base_seconds * (2 ** (attempts - 1))
                nxt = (now + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
                db.update("publish_jobs", "job_id", job["job_id"], {"state": "queued", "next_attempt_at": nxt,
                                                                   "last_error": str(e), "locked_at": None, "updated_at": now_iso()})
                results.append({"job_id": job["job_id"], "state": "retry_scheduled", "next_attempt_at": nxt, "error": str(e)})
            else:
                _finish(db, job, v, "failed", {"error": str(e), "kind": e.kind,
                                               "guidance": adapter.rollback_guidance("")}, str(e))
                results.append({"job_id": job["job_id"], "state": "failed", "error": str(e), "kind": e.kind})
    return results


def _attempt(db: DB, job_id: str, outcome: str, err: AdapterError | None, resp: dict | None) -> None:
    db.insert("publish_attempts", {"job_id": job_id, "at": now_iso(), "outcome": outcome,
                                   "error_kind": err.kind if err else None, "error": str(err) if err else None,
                                   "response": resp or {}})


def _finish(db: DB, job: dict, v: dict, state: str, receipt: dict, err: str, res=None) -> None:
    upd = {"state": state, "receipt": receipt, "last_error": err, "locked_at": None, "updated_at": now_iso()}
    if res:
        upd.update({"platform_post_id": res.platform_post_id, "permalink": res.permalink, "remote_status": res.status})
    db.update("publish_jobs", "job_id", job["job_id"], upd)
    vstate = {"published": "published", "simulated": "simulated", "failed": "failed", "blocked": "needs_fix"}[state]
    db.update("variants", "variant_id", v["variant_id"], {"state": vstate, "updated_at": now_iso()})
    db.update("schedules", "variant_id", v["variant_id"], {"state": "done" if state in ("published", "simulated") else "missed"})
    db.audit("publisher", f"job_{state}", "publish_jobs", job["job_id"],
             {"variant_id": v["variant_id"], "error": err, "permalink": upd.get("permalink", "")})


def recover_stuck_jobs(db: DB, settings: Settings, now: datetime, transport=None, stale_minutes: int = 10) -> list[str]:
    """A job left in 'publishing' (crash mid-call) is never blindly retried: ask the platform first."""
    recovered = []
    cutoff = (now - timedelta(minutes=stale_minutes)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    for j in db.all("publish_jobs", "state='publishing' AND locked_at<=?", [cutoff]):
        acc = db.get("accounts", "account_id", j["account_id"])
        adapter = get_adapter(acc["platform"], transport=transport, settings=settings)
        found = adapter.find_existing(acc, j["idempotency_key"])
        v = db.get("variants", "variant_id", j["variant_id"])
        if found:
            _finish(db, j, v, "published", found.raw, "", found)
        else:
            db.update("publish_jobs", "job_id", j["job_id"], {"state": "needs_manual_check", "updated_at": now_iso(),
                                                             "last_error": "crashed mid-publish; confirm on platform before retry"})
        recovered.append(j["job_id"])
    return recovered


def poll_statuses(db: DB, settings: Settings, transport=None) -> list[dict]:
    out = []
    for j in db.all("publish_jobs", "state='published' AND remote_status='processing'"):
        acc = db.get("accounts", "account_id", j["account_id"])
        try:
            r = get_adapter(acc["platform"], transport=transport, settings=settings).status(acc, j["platform_post_id"])
            db.update("publish_jobs", "job_id", j["job_id"], {"remote_status": r.status, "permalink": r.permalink or j["permalink"]})
            out.append({"job_id": j["job_id"], "remote_status": r.status})
        except AdapterError as e:
            out.append({"job_id": j["job_id"], "error": str(e)})
    return out


# --- opt-in evergreen auto-approval policy ---------------------------------------------------------

def auto_policy() -> dict:
    return json.loads((CONFIG_DIR / "auto_publish_policy.json").read_text())


def apply_evergreen_policy(db: DB) -> list[str]:
    pol = auto_policy()
    if not pol.get("enabled"):
        return []
    approved = []
    for v in db.all("variants", "state='in_review'"):
        if v["series"] not in pol["series_allowlist"] or v["account_id"] not in pol["account_allowlist"]:
            continue
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
        n = db.one("SELECT COUNT(*) AS n FROM approvals a JOIN variants x ON x.variant_id=a.variant_id WHERE "
                   "a.reviewer='policy:evergreen' AND x.account_id=? AND a.at>=?", [v["account_id"], week_ago])["n"]
        if n >= pol.get("max_per_account_per_week", 0):
            continue
        try:
            r = approve(db, v["variant_id"], "policy:evergreen", "auto-approved under opt-in evergreen policy")
            if r["state"] == "approved":
                approved.append(v["variant_id"])
        except WorkflowError:
            continue
    return approved
