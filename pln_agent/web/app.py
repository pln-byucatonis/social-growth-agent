"""Human approval dashboard (FastAPI + server-rendered Jinja).

Two ways to run it:
* Local: binds 127.0.0.1. Optional HTTP Basic auth via PLN_DASHBOARD_PASSWORD; reviewers type their name.
* Behind the ops portal (ops.proleaguenetwork.com → /api/social-agent/*): PLN_AGENT_API_KEY is set, every
  request must carry it as a bearer token (only the ops proxy has it), and the signed-in Cognito user arrives
  in X-PLN-User-Email / X-PLN-User-Admin. Reviewer identity comes from that header, not a form field, and
  changes require an ops admin. X-Forwarded-Prefix makes every link resolve under the proxy path.

Every action goes through the same workflow functions as the CLI, so gates and audit are identical.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import migration as mig
from ..accounts import publish_eligibility
from ..adapters import get_adapter
from ..calendar_planner import plan_week
from ..config import load_settings
from ..db import DB
from ..editorial import generate_pack
from ..experiments import recommendations
from ..media import render_variant
from ..metrics import NORTH_STAR
from ..rights import evaluate
from ..workflow import (WorkflowError, approve, cancel, edit_variant, kill_switch_on, reject, run_publisher, schedule,
                        set_kill_switch)

HERE = Path(__file__).parent
settings = load_settings()
app = FastAPI(title="PLN Social Growth Agent", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(HERE / "templates"))
settings.render_dir.mkdir(parents=True, exist_ok=True)
app.mount("/renders", StaticFiles(directory=str(settings.render_dir)), name="renders")
_db: DB | None = None
QUEUE_LIMIT = 300
_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._-]+)*$")


def db() -> DB:
    global _db
    if _db is None:
        _db = DB(settings.database_url)
        if not _db.one("SELECT account_id FROM accounts LIMIT 1"):  # first boot of a fresh database
            from ..accounts import load_accounts
            load_accounts(_db, actor="bootstrap")
            mig.sync_decisions(_db)
        if os.environ.get("PLN_WORKER", "").lower() in ("1", "on", "true"):
            from ..worker import start_background
            start_background(DB(settings.database_url), settings)  # own connection for the thread
    return _db


@app.on_event("startup")
def _warm():
    if os.environ.get("PLN_WORKER", "").lower() in ("1", "on", "true"):
        db()


@app.middleware("http")
async def auth(request: Request, call_next):
    request.state.user = None
    request.state.admin = False
    if request.url.path == "/healthz":
        return await call_next(request)
    if request.url.path == "/internal/tick":  # scheduler → service; key only, no user identity
        key = settings.agent_api_key or os.environ.get("PLN_AGENT_API_KEY", "")
        hdr = request.headers.get("authorization", "")
        if not (key and hdr.startswith("Bearer ") and secrets.compare_digest(hdr[7:], key)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
    key = settings.agent_api_key or os.environ.get("PLN_AGENT_API_KEY", "")
    if key:
        hdr = request.headers.get("authorization", "")
        if not (hdr.startswith("Bearer ") and secrets.compare_digest(hdr[7:], key)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        email = (request.headers.get("x-pln-user-email") or "").strip().lower()
        if not email:
            return JSONResponse({"error": "missing user identity"}, status_code=401)
        request.state.user = email
        request.state.admin = request.headers.get("x-pln-user-admin") == "1"
        if request.method not in ("GET", "HEAD") and not request.state.admin:
            return JSONResponse({"error": "ops admin required for changes"}, status_code=403)
        return await call_next(request)
    pw = os.environ.get("PLN_DASHBOARD_PASSWORD")
    if pw:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, pw)
            except Exception:
                ok = False
        if not ok:
            return HTMLResponse("auth required", status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


def base(request: Request) -> str:
    p = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    return p if _PREFIX_RE.match(p) else ""


def who(request: Request, typed: str) -> str:
    """The acting reviewer: the ops-portal user when proxied, else the typed name (local mode)."""
    name = request.state.user or (typed or "").strip()
    if not name:
        raise WorkflowError("Enter your name (reviewer identity is recorded in the audit log).")
    return name


def ctx(request: Request, **kw) -> dict:
    return {"request": request, "dry_run": settings.dry_run, "publishing_enabled": settings.publishing_enabled,
            "kill": kill_switch_on(db()), "base": base(request), "user": request.state.user,
            "ops_connected": bool(settings.ops_database_url),
            # framed inside the ops portal: the portal sidebar is the nav, so hide ours
            "embedded": request.headers.get("sec-fetch-dest") == "iframe",
            "msg": request.query_params.get("msg"), "err": request.query_params.get("err"), **kw}


def back(request: Request, url: str, msg: str = "", err: str = "") -> RedirectResponse:
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(base(request) + url + ("?" + q if q else ""), status_code=303)


def page(request: Request, name: str, **kw):
    return templates.TemplateResponse(request, name, ctx(request, **kw))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    d = db()
    counts = {r["state"]: r["n"] for r in d.query("SELECT state, COUNT(*) AS n FROM variants GROUP BY state")}
    jobs = d.all("publish_jobs", order="updated_at DESC")[:15]
    return page(request, "home.html", counts=counts, jobs=jobs, recs=recommendations(d)[:25])


@app.post("/kill")
def kill(request: Request, state: str = Form(...), actor: str = Form(""), reason: str = Form("")):
    try:
        set_kill_switch(db(), state == "on", who(request, actor), reason)
    except WorkflowError as e:
        return back(request, "/", err=str(e))
    return back(request, "/", f"Emergency stop {state.upper()}")


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, platform: str = "", state: str = ""):
    d = db()
    where, params = ["1=1"], []
    if platform:
        where.append("platform=?"), params.append(platform)
    if state:
        where.append("state=?"), params.append(state)
    cond = " AND ".join(where)
    total = d.one(f"SELECT COUNT(*) AS n FROM variants WHERE {cond}", params)["n"]
    vs = [d.decode("variants", r) for r in d.query(
        "SELECT variant_id, asset_id, account_id, platform, state, hook, score, risk_flags, validation, caption_hash "
        "FROM variants "
        f"WHERE {cond} ORDER BY platform, state, score DESC LIMIT {QUEUE_LIMIT}", params)]
    accs = {a["account_id"]: a for a in d.all("accounts")}
    perm = {k: publish_eligibility(a, settings, kill_switch_on(d)) for k, a in accs.items()}
    platforms = [r["platform"] for r in d.query("SELECT DISTINCT platform FROM variants ORDER BY platform")]
    ids = sorted({v["asset_id"] for v in vs})
    thumbs = {}
    if ids:
        from ..media import asset_thumb_url
        marks = ",".join("?" for _ in ids)
        for a in d.query(f"SELECT asset_id, probe FROM assets WHERE asset_id IN ({marks})", ids):
            a = d.decode("assets", a)
            thumbs[a["asset_id"]] = asset_thumb_url(a)
    return page(request, "queue.html", variants=vs, accs=accs, perm=perm, platform=platform, state=state, thumbs=thumbs,
                platforms=platforms, total=total, limit=QUEUE_LIMIT)


@app.post("/queue/bulk")
def queue_bulk(request: Request, pick: list[str] = Form([]), decision: str = Form(...), reviewer: str = Form(""),
               note: str = Form(""), back_to: str = Form("/queue")):
    """Bulk approve/reject. Each decision is a separate audited approval; runs in the background because
    a full page of posts takes longer than the portal proxy's 25s window."""
    import threading
    from ..workflow import BULK_MAX, bulk_review
    back_to = back_to if back_to.startswith("/queue") else "/queue"
    picks = [tuple(x.split("|", 1)) if "|" in x else (x, "") for x in pick]
    if not picks:
        return back(request, back_to, err="Select at least one post.")
    if len(picks) > BULK_MAX:
        return back(request, back_to, err=f"Select at most {BULK_MAX} posts per bulk action.")
    if decision == "reject" and not note.strip():
        return back(request, back_to, err="Add a note to reject.")
    try:
        name = who(request, reviewer)
    except WorkflowError as e:
        return back(request, back_to, err=str(e))

    def work():
        own = DB(settings.database_url, init=False)
        try:
            bulk_review(own, picks, name, decision, note)
        except Exception as e:
            own.audit(name, "bulk_review_failed", "variants", "", {"error": str(e)[:300]})

    if len(picks) <= 10:  # small batches finish well inside the window: show the outcome right away
        r = bulk_review(db(), picks, name, decision, note)
        skipped = "; ".join(f"{k.split(':')[-1]}: {v}" for k, v in list(r["skipped"].items())[:5])
        return back(request, back_to, f"{decision.capitalize()}d {len(r['done'])} post(s)." +
                    (f" Skipped {len(r['skipped'])}: {skipped}" if r["skipped"] else ""))
    threading.Thread(target=work, daemon=True, name="bulk-review").start()
    return back(request, back_to, f"Recording {decision} for {len(picks)} posts in the background — refresh in "
                "a moment. Sensitive posts and posts whose copy changed are skipped (see Audit log).")


@app.get("/variant/{vid:path}", response_class=HTMLResponse)
def variant(request: Request, vid: str):
    d = db()
    v = d.get("variants", "variant_id", vid)
    if not v:
        raise HTTPException(404)
    acc = d.get("accounts", "account_id", v["account_id"])
    asset = d.get("assets", "asset_id", v["asset_id"])
    ok, reasons = publish_eligibility(acc, settings, kill_switch_on(d))
    approvals = d.all("approvals", "variant_id=?", [vid], order="id")
    audit = d.all("audit_log", "entity_id=?", [vid], order="id DESC")
    jobs = d.all("publish_jobs", "variant_id=?", [vid])
    planned = d.one("SELECT slot_at FROM schedules WHERE variant_id=? AND state='planned'", [vid])
    render_url = ""
    if (v.get("render_path") or "").startswith("s3://"):
        from ..media import resolve_media
        render_url = resolve_media(v["render_path"])
    elif v.get("render_path"):
        try:
            render_url = base(request) + "/renders/" + str(Path(v["render_path"]).relative_to(settings.render_dir))
        except ValueError:
            render_url = ""
    preview = get_adapter(acc["platform"], settings=settings).simulate(v, acc, "preview")
    from ..media import asset_preview
    source = asset_preview(asset, (v.get("edl") or {}).get("segments"))
    return page(request, "variant.html", v=v, acc=acc, asset=asset, live_ok=ok, reasons=reasons, approvals=approvals,
                source=source,
                audit=audit, jobs=jobs, planned=planned, render_url=render_url,
                would_send=preview.raw["would_send"], north=NORTH_STAR.get(v["platform"], {}))


@app.post("/variant/{vid:path}/edit")
def do_edit(request: Request, vid: str, actor: str = Form(""), hook: str = Form(...), caption: str = Form(...),
            cta: str = Form(...)):
    try:
        v = edit_variant(db(), vid, {"hook": hook, "caption": caption, "cta": cta}, who(request, actor))
        return back(request, f"/variant/{vid}", f"Saved. State: {v['state']} (any prior approval was invalidated)")
    except WorkflowError as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/approve")
def do_approve(request: Request, vid: str, reviewer: str = Form(""), note: str = Form("")):
    try:
        name = who(request, reviewer)
        r = approve(db(), vid, name, note)
        return back(request, f"/variant/{vid}", f"Recorded approval by {name}: state={r['state']} ({len(r['approvers'])}/{r['needed']})")
    except WorkflowError as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/reject")
def do_reject(request: Request, vid: str, reviewer: str = Form(""), note: str = Form(...)):
    try:
        reject(db(), vid, who(request, reviewer), note)
        return back(request, f"/variant/{vid}", "Rejected")
    except WorkflowError as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/schedule")
def do_schedule(request: Request, vid: str, actor: str = Form(""), slot_at: str = Form(...)):
    try:
        at = datetime.fromisoformat(slot_at)
        at = at if at.tzinfo else at.replace(tzinfo=ZoneInfo(settings.timezone))
        j = schedule(db(), vid, at, who(request, actor))
        return back(request, f"/variant/{vid}", f"Scheduled job {j['job_id']}")
    except (WorkflowError, ValueError) as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/cancel")
def do_cancel(request: Request, vid: str, actor: str = Form(""), reason: str = Form("cancelled from dashboard")):
    cancel(db(), vid, request.state.user or actor or "dashboard", reason)
    return back(request, f"/variant/{vid}", "Cancelled")


@app.post("/variant/{vid:path}/render")
def do_render(request: Request, vid: str):
    """Renders take longer than the portal proxy's 25s window, so run them in the background."""
    import threading
    d = db()
    v = d.get("variants", "variant_id", vid)
    if not v:
        raise HTTPException(404)
    if v.get("render_status") == "rendering":
        return back(request, f"/variant/{vid}", "Already rendering — refresh in a minute.")
    d.update("variants", "variant_id", vid, {"render_status": "rendering"})
    actor = request.state.user or "dashboard"

    def work():
        own = DB(settings.database_url, init=False)  # separate connection for the worker thread
        try:
            render_variant(own, vid, settings, actor=actor)
        except Exception as e:  # never leave a variant stuck in "rendering"
            own.update("variants", "variant_id", vid, {"render_status": "render_failed"})
            own.audit(actor, "variant_render_failed", "variants", vid, {"error": str(e)[:300]})

    threading.Thread(target=work, daemon=True, name=f"render-{vid[:20]}").start()
    return back(request, f"/variant/{vid}", "Rendering started — this takes about a minute. Refresh to see it.")


@app.post("/variant/{vid:path}/public-link")
def do_public_link(request: Request, vid: str):
    from ..public_media import PublicMediaError, publish_render
    try:
        url = publish_render(db(), vid, request.state.user or "dashboard")
        return back(request, f"/variant/{vid}", f"Public link ready: {url}")
    except PublicMediaError as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/publish/run")
def do_publish(request: Request):
    res = run_publisher(db(), settings)
    return back(request, "/", f"Publisher ran: {res or 'nothing due'}")


# --- assets + rights ------------------------------------------------------------------------------

@app.get("/assets", response_class=HTMLResponse)
def assets(request: Request, brand: str = "", rights: str = ""):
    d = db()
    rows = d.all("assets", "brand_id=?" if brand else "", [brand] if brand else [], order="ingested_at DESC")
    counts = {r["asset_id"]: r["n"] for r in d.query("SELECT asset_id, COUNT(*) AS n FROM variants GROUP BY asset_id")}
    out = []
    for a in rows:
        r = evaluate(a)
        if rights == "blocked" and r.ok or rights == "clear" and not r.ok:
            continue
        out.append({**a, "rights_eval": r.as_dict(), "variants": counts.get(a["asset_id"], 0)})
    brands = [r["brand_id"] for r in d.query("SELECT DISTINCT brand_id FROM assets ORDER BY brand_id")]
    return page(request, "assets.html", assets=out[:300], total=len(out), brand=brand, rights=rights, brands=brands)


@app.get("/asset/{aid}", response_class=HTMLResponse)
def asset_detail(request: Request, aid: str):
    d = db()
    a = d.get("assets", "asset_id", aid)
    if not a:
        raise HTTPException(404)
    return page(request, "asset.html", a=a, rights_eval=evaluate(a).as_dict(),
                rights_row=d.get("asset_rights", "asset_id", aid) or {},
                variants=d.all("variants", "asset_id=?", [aid], order="platform"),
                audit=d.all("audit_log", "entity_id=?", [aid], order="id DESC")[:30])


def _rights_fields(license: str, owner: str, territory: str, expires: str, rights_verified: str, people_verified: str,
                   music_cleared: str, sponsor_approved: str, note: str) -> dict:
    return {"license": license.strip(), "owner": owner.strip(),
            "territory": [t.strip().upper() for t in territory.split(",") if t.strip()],
            "rights_expires_at": expires.strip() or None, "rights_verified": rights_verified == "on",
            "people_verified": people_verified == "on", "music_cleared": music_cleared == "on",
            "sponsor_approved": sponsor_approved == "on", "note": note.strip()}


@app.post("/asset/{aid}/rights")
def asset_rights(request: Request, aid: str, actor: str = Form(""), license: str = Form(""), owner: str = Form(""),
                 territory: str = Form(""), expires: str = Form(""), rights_verified: str = Form(""),
                 people_verified: str = Form(""), music_cleared: str = Form(""), sponsor_approved: str = Form(""),
                 note: str = Form(""), regenerate: str = Form("")):
    from ..ops_sources import set_asset_rights
    try:
        name = who(request, actor)
        set_asset_rights(db(), aid, name, **_rights_fields(license, owner, territory, expires, rights_verified,
                                                           people_verified, music_cleared, sponsor_approved, note))
        msg = "Rights record saved"
        if regenerate:
            p = generate_pack(db(), aid, settings, actor=name)
            msg += f"; regenerated {len(p['variants'])} variants"
        return back(request, f"/asset/{aid}", msg)
    except (WorkflowError, ValueError, KeyError) as e:
        return back(request, f"/asset/{aid}", err=str(e))


@app.post("/assets/rights-bulk")
def assets_rights_bulk(request: Request, brand: str = Form(...), actor: str = Form(""), license: str = Form(...),
                       owner: str = Form(""), territory: str = Form(...), expires: str = Form(""),
                       rights_verified: str = Form(""), music_cleared: str = Form(""), note: str = Form("")):
    """Apply one rights record (e.g. 'PLN-owned production, WORLDWIDE') to every asset of a brand.
    People verification is deliberately NOT bulk-settable: identities are checked clip by clip."""
    from ..ops_sources import set_asset_rights
    try:
        name = who(request, actor)
        ids = [a["asset_id"] for a in db().all("assets", "brand_id=?", [brand])]
        fields = _rights_fields(license, owner, territory, expires, rights_verified, "", music_cleared, "", note)
        fields.pop("people_verified"), fields.pop("sponsor_approved")
        for aid in ids:
            set_asset_rights(db(), aid, name, **fields)
        return back(request, f"/assets?brand={brand}", f"Rights applied to {len(ids)} {brand} assets")
    except (WorkflowError, ValueError) as e:
        return back(request, f"/assets?brand={brand}", err=str(e))


@app.post("/assets/plan")
def assets_plan(request: Request, brand: str = Form("")):
    """(Re)generate editorial packs for rights-clear assets."""
    d = db()
    n = 0
    for a in d.all("assets", "brand_id=?" if brand else "", [brand] if brand else []):
        if evaluate(a).ok:
            generate_pack(d, a["asset_id"], settings, actor=request.state.user or "dashboard")
            n += 1
    return back(request, "/assets" + (f"?brand={brand}" if brand else ""), f"Planned {n} rights-clear assets")


@app.post("/ops/import")
def ops_import(request: Request, limit: int = Form(100), min_score: int = Form(60), property: str = Form("")):
    from ..ops_sources import import_ops_accounts, import_ops_clips
    if not settings.ops_database_url:
        return back(request, "/assets", err="PLN_OPS_DATABASE_URL is not configured")
    ops = DB(settings.ops_database_url, init=False)
    actor = request.state.user or "dashboard"
    a = import_ops_accounts(db(), ops, actor)
    c = import_ops_clips(db(), ops, settings.ops_assets_bucket, limit=limit, min_score=min_score,
                         properties=[property] if property else None, actor=actor)
    return back(request, "/assets", f"Accounts: {len(a['matched'])} matched, {len(a['added'])} added · "
                                    f"Clips imported: {c['imported']} ({len(c['skipped'])} skipped)")


# --- calendar, accounts, migration, audit ---------------------------------------------------------

@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, start: str = ""):
    s = date.fromisoformat(start) if start else date.today() + timedelta(days=1)
    plan = plan_week(db(), settings, s, states=("in_review", "approved", "scheduled"), persist=False)
    days: dict[str, list] = {}
    for p in plan["placed"]:
        days.setdefault(p["slot"].strftime("%a %b %d"), []).append(p)
    return page(request, "calendar.html", plan=plan, days=days, start=s.isoformat())


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    from .. import oauth
    d = db()
    rows = []
    for a in d.all("accounts", order="canonical_brand_id, platform"):
        ok, reasons = publish_eligibility(a, settings, kill_switch_on(d))
        rows.append({**a, "live_ok": ok, "reasons": reasons, "provider": oauth.PLATFORM_PROVIDER.get(a["platform"], "")})
    caps = {p: get_adapter(p, settings=settings).capabilities() for p in sorted({a["platform"] for a in rows})}
    apps = []
    for p in oauth.PROVIDERS.values():
        try:
            status = oauth.app_status(p.key)
        except Exception as e:  # secret store unreachable: show it, don't 500
            status = {"configured": False, "client_id_hint": "", "error": str(e)[:120]}
        apps.append({"p": p, "redirect_uri": oauth.redirect_uri(p.key), **status,
                     "accounts": [r for r in rows if r["provider"] == p.key]})
    try:
        meta_user = oauth.meta_user_token_status()
    except Exception as e:  # secret store unreachable
        meta_user = {"connected": False, "error": str(e)[:120]}
    return page(request, "accounts.html", rows=rows, caps=caps, apps=apps, meta_user=meta_user)


def _admin(request: Request) -> None:
    if settings.agent_api_key and not request.state.admin:
        raise HTTPException(403, "ops admin required")


@app.post("/oauth/app/{provider}")
def oauth_save_app(request: Request, provider: str, client_id: str = Form(""), client_secret: str = Form("")):
    from .. import oauth
    _admin(request)
    try:
        oauth.save_app(provider, client_id, client_secret, db(), request.state.user or "dashboard")
    except (KeyError, ValueError) as e:
        return back(request, "/accounts", err=str(e))
    return back(request, "/accounts", f"Saved the {oauth.PROVIDERS[provider].label} app credentials")


@app.post("/oauth/app/meta/config")
def oauth_meta_config(request: Request, config_id: str = Form("")):
    from .. import oauth
    _admin(request)
    try:
        oauth.save_meta_config(config_id, db(), request.state.user or "dashboard")
    except ValueError as e:
        return back(request, "/accounts", err=str(e))
    return back(request, "/accounts", "Saved the Meta login configuration" if config_id.strip() else "Cleared the Meta configuration")


@app.get("/oauth/start/{provider}")
def oauth_start(request: Request, provider: str, account: str = ""):
    """Top-level navigation (platform consent screens refuse iframes)."""
    from .. import oauth
    _admin(request)
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404)
    try:
        url = oauth.start(db(), provider, account, request.state.user or "dashboard")
    except (LookupError, ValueError) as e:
        return page(request, "oauth_result.html", result=None, error=str(e), portal=_portal_accounts())
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/callback/{provider}")
def oauth_callback(request: Request, provider: str):
    from .. import oauth
    _admin(request)
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404)
    try:
        result = oauth.callback(db(), provider, dict(request.query_params), request.state.user or "dashboard")
        return page(request, "oauth_result.html", result=result, error="", portal=_portal_accounts(),
                    label=oauth.PROVIDERS[provider].label)
    except (PermissionError, LookupError) as e:
        return page(request, "oauth_result.html", result=None, error=str(e), portal=_portal_accounts())


def _portal_accounts() -> str:
    return os.environ.get("PLN_PORTAL_ACCOUNTS_URL", "https://ops.proleaguenetwork.com/social/growth/accounts")


@app.post("/accounts/confirm-ownership")
def confirm_ownership(request: Request, account_id: str = Form("")):
    """'These are ours': record PLN ownership (all unverified accounts, or one). Doesn't connect or authorize."""
    from ..accounts import set_verification
    _admin(request)
    d = db()
    actor = request.state.user or "dashboard"
    ids = [account_id] if account_id else [a["account_id"] for a in d.all("accounts", "verification_status='unverified'")]
    for i in ids:
        set_verification(d, i, "ownership_confirmed", actor, owner=f"Pro League Network (confirmed by {actor})")
    return back(request, "/accounts", f"Ownership confirmed for {len(ids)} account(s)")


# --- Phase 1: audience intelligence, creative DB, knowledge, cadence -----------------------------

@app.get("/intelligence", response_class=HTMLResponse)
def intelligence(request: Request, days: int = 28):
    from .. import audience
    d = db()
    days = max(7, min(days, 120))
    rows = audience.portfolio(d, days)
    trends = {r["account_id"]: audience.follower_trend(d, r["account_id"])[-60:] for r in rows}
    return page(request, "intelligence.html", rows=rows, labels=audience.KPI_LABELS, days=days, trends=trends,
                by_brand=audience.breakdown(d, "brand_id"), by_type=audience.breakdown(d, "content_type"),
                by_format=audience.breakdown(d, "post_type"), overlap=audience.OVERLAP_QUESTIONS,
                matrix=audience.MEASURABILITY)


@app.get("/creative", response_class=HTMLResponse)
def creative(request: Request, brand: str = "", platform: str = "", outcome: str = "shares_per_1k_views", fmt: str = ""):
    from ..audience import post_outcomes
    from ..creative_db import EVIDENCE_RULE, OUTCOMES, what_works
    d = db()
    res = what_works(d, brand, platform, outcome if outcome in OUTCOMES else "shares_per_1k_views", fmt) \
        if (brand or platform) else None
    accs = [a["account_id"] for a in d.all("accounts") if (not brand or a["brand_id"] == brand)
            and (not platform or a["platform"] == platform)]
    recent = sorted(post_outcomes(d, accs), key=lambda p: str(p["published_at"] or ""), reverse=True)[:60]
    brands = [r["brand_id"] for r in d.query("SELECT DISTINCT brand_id FROM posts ORDER BY brand_id")]
    platforms = [r["platform"] for r in d.query("SELECT DISTINCT platform FROM posts ORDER BY platform")]
    total = d.one("SELECT COUNT(*) AS n FROM posts")["n"]
    return page(request, "creative.html", res=res, recent=recent, brands=brands, platforms=platforms, brand=brand,
                platform=platform, outcome=outcome, fmt=fmt, outcomes=OUTCOMES, rule=EVIDENCE_RULE, total=total)


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request, level: str = "", platform: str = ""):
    from .. import knowledge as ke
    d = db()
    if not d.one("SELECT entry_id FROM knowledge LIMIT 1"):
        ke.seed(d)
    rows = [e for e in d.all("knowledge", order="level, platform, entry_id")
            if (not level or e["level"] == level) and (not platform or e["platform"] in ("", platform))]
    rows.sort(key=lambda e: (ke.LEVELS.index(e["level"]) if e["level"] in ke.LEVELS else 9, e["status"] != "needs_review"))
    return page(request, "knowledge.html", rows=rows, levels=ke.LEVELS, labels=ke.LEVEL_LABELS, level=level,
                platform=platform, sources=d.all("knowledge_sources", order="last_checked_at DESC"))


@app.post("/knowledge/add")
def knowledge_add(request: Request, level: str = Form(...), statement: str = Form(...), platform: str = Form(""),
                  topic: str = Form(""), source_url: str = Form(""), source_title: str = Form(""),
                  publisher: str = Form(""), source_date: str = Form(""), quote: str = Form(""),
                  applicability: str = Form("")):
    from .. import knowledge as ke
    _admin(request)
    try:
        ke.add_entry(db(), {"level": level, "statement": statement, "platform": platform, "topic": topic,
                            "source_url": source_url.strip(), "source_title": source_title, "publisher": publisher,
                            "source_date": source_date, "quote": quote, "applicability": applicability},
                     request.state.user or "dashboard")
    except ValueError as e:
        return back(request, "/knowledge", err=str(e))
    return back(request, "/knowledge", "Added")


@app.post("/knowledge/{eid}/status")
def knowledge_status(request: Request, eid: str, status: str = Form(...), note: str = Form("")):
    from .. import knowledge as ke
    _admin(request)
    try:
        ke.set_status(db(), eid, status, request.state.user or "dashboard", note)
    except ValueError as e:
        return back(request, "/knowledge", err=str(e))
    return back(request, "/knowledge", f"{eid}: {status}")


@app.post("/knowledge/review")
def knowledge_review(request: Request):
    """Re-check every cited source now (runs in the background; the worker also does this weekly)."""
    import threading
    from .. import knowledge as ke
    _admin(request)
    threading.Thread(target=lambda: ke.review_sources(DB(settings.database_url, init=False), force=True),
                     daemon=True).start()
    return back(request, "/knowledge", "Source review started — refresh in a minute.")


@app.post("/insights/collect")
def insights_collect(request: Request):
    """Collect Instagram + YouTube insights for every connected account now (background; also every refresh)."""
    import threading
    from ..insights import collect_instagram, collect_youtube
    _admin(request)
    actor = request.state.user or "dashboard"

    def work():
        own = DB(settings.database_url, init=False)
        collect_instagram(own, actor=actor)
        collect_youtube(own, settings.timezone, actor=actor)

    threading.Thread(target=work, daemon=True).start()
    return back(request, "/intelligence", "Collecting insights — refresh in a minute.")


@app.get("/growth", response_class=HTMLResponse)
def growth_page(request: Request):
    from .. import growth_officer as cgo
    d = db()
    brief = cgo.latest_brief(d) or cgo.generate_brief(d, request.state.user or "dashboard")
    return page(request, "growth.html", b=brief, shares=cgo.current_shares(d))


@app.get("/competitors", response_class=HTMLResponse)
def competitors_page(request: Request, brand: str = ""):
    from .. import competitive as ci
    d = db()
    hist = {}
    for r in d.all("competitor_snapshots", order="captured_at"):
        hist.setdefault(r["handle"], []).append(r)
    return page(request, "competitors.html", rows=ci.latest(d), hist=hist, gaps=ci.gaps(d), standouts=ci.standouts(d, brand),
                cfg=ci.config(), brand=brand)


@app.post("/competitors/refresh")
def competitors_refresh(request: Request):
    from .. import competitive as ci
    _admin(request)
    r = ci.snapshot(db(), actor=request.state.user or "dashboard")
    return back(request, "/competitors", f"Captured {len(r['captured'])} benchmark account(s)",
                "; ".join(f"@{k}: {v}" for k, v in list(r["errors"].items())[:4]))


@app.get("/community", response_class=HTMLResponse)
def community_page(request: Request, status: str = ""):
    import json as _json
    from .. import community as cm
    d = db()
    sts = tuple(x for x in status.split(",") if x) or ("escalated", "needs_answer", "drafted", "approved")
    rows = cm.queue(d, sts)
    accs = {a["account_id"]: a for a in d.all("accounts")}
    raw = d.kv_get("community:faqs")
    return page(request, "community.html", rows=rows, accs=accs, s=cm.summary(d), faqs=_json.loads(raw) if raw else cm.faqs(d),
                status=",".join(sts), cfg=cm.config())


@app.post("/community/refresh")
def community_refresh(request: Request):
    from .. import community as cm
    _admin(request)
    r = cm.refresh(db(), settings, actor=request.state.user or "dashboard")
    errs = "; ".join(f"{k}: {v}" for k, v in list(r["errors"].items())[:3])
    return back(request, "/community", f"Fetched {r['new']} new comment(s)", errs)


@app.post("/community/{comment_id:path}/decide")
def community_decide(request: Request, comment_id: str, action: str = Form(...), text: str = Form(None),
                     note: str = Form(""), actor: str = Form("")):
    from .. import community as cm
    try:
        cm.decide(db(), comment_id, action, who(request, actor), text, note)
    except (KeyError, ValueError, WorkflowError) as e:
        return back(request, "/community", err=str(e))
    return back(request, "/community", f"{action}: done" + (" — it sends on the next tick (simulated unless live publishing is on)" if action == "approve" else ""))


@app.post("/growth/generate")
def growth_generate(request: Request):
    from .. import growth_officer as cgo
    _admin(request)
    cgo.generate_brief(db(), request.state.user or "dashboard")
    return back(request, "/growth", "Brief regenerated from the latest data")


@app.post("/growth/{brief_id}/apply-allocation")
def growth_apply(request: Request, brief_id: str):
    from .. import growth_officer as cgo
    _admin(request)
    try:
        cgo.apply_allocation(db(), brief_id, request.state.user or "dashboard")
    except KeyError:
        return back(request, "/growth", err="That brief no longer exists — regenerate it")
    return back(request, "/growth", "Applied: new drafts are scored with the approved creative-effort split")


@app.get("/cadence", response_class=HTMLResponse)
def cadence_page(request: Request):
    d = db()
    accs = {a["account_id"]: a for a in d.all("accounts")}
    rows = [{**r, "acc": accs.get(r["account_id"], {})} for r in d.all("cadence", order="account_id")]
    order = {"increase": 0, "decrease": 1, "production_limited": 2, "hold": 3, "collect_baseline": 4}
    rows.sort(key=lambda r: (order.get(r["action"], 9), r["account_id"]))
    import json as _json
    from ..calendar_planner import slot_hypotheses
    from ..publishing import account_slots
    hyp = slot_hypotheses()
    for r in rows:
        raw = d.kv_get(f"slots:{r['account_id']}")
        r["slots_learned"] = _json.loads(raw) if raw else None
        r["slots_used"], r["slots_why"] = account_slots(d, r["account_id"], r["acc"].get("platform", ""), hyp)
    tests = d.all("experiments", "variable='frequency'", order="week_start")
    return page(request, "cadence.html", rows=rows, tests=tests)


@app.post("/cadence/{account_id}/frequency-test")
def cadence_frequency_test(request: Request, account_id: str):
    from ..publishing import plan_frequency_test
    _admin(request)
    rows = plan_frequency_test(db(), account_id, actor=request.state.user or "dashboard")
    return back(request, "/cadence", f"Planned a 3-week frequency test for {account_id} starting {rows[0]['week_start']}")


@app.post("/cadence/recompute")
def cadence_recompute(request: Request):
    from ..cadence import recommend_all
    _admin(request)
    n = len(recommend_all(db()))
    return back(request, "/cadence", f"Recomputed {n} accounts")


# --- Phase 2: viral content scout + creative director --------------------------------------------

def _ops_db() -> DB:
    if not settings.ops_database_url:
        raise HTTPException(400, "ops portal database is not configured (PLN_OPS_DATABASE_URL)")
    return DB(settings.ops_database_url, init=False)


@app.get("/scout", response_class=HTMLResponse)
def scout_page(request: Request, brand: str = "", source: str = "", view: str = "potential"):
    from .. import scout
    from ..media import resolve_media
    d = db()
    field = "pln_potential" if view == "pln" else "potential"
    moments = scout.top_moments(d, source, brand, 20, field)
    for m in moments:
        tk = (m.get("detail") or {}).get("thumb_key")
        m["thumb"] = resolve_media(f"s3://{settings.ops_assets_bucket}/{tk}", "image/jpeg") if tk and settings.ops_assets_bucket else ""
    srcs = scout.sources(d, brand)[:60]
    counts = {r["brand_id"]: r["n"] for r in d.query("SELECT brand_id, COUNT(*) AS n FROM scout_moments GROUP BY brand_id")}
    return page(request, "scout.html", moments=moments, sources=srcs, brand=brand, source=source, view=view,
                counts=counts, weights=scout.taxonomy()["weights"], model=scout.taxonomy()["model_version"])


@app.post("/scout/promote")
async def scout_promote(request: Request):
    """Moments → assets + creative packages. Background: imports and drafting outlast the proxy window."""
    import threading
    from .. import scout
    _admin(request)
    form = await request.form()
    ids = [i for i in form.getlist("clip_id") if i][:40]
    if not ids:
        return back(request, "/scout", err="Pick at least one moment")
    actor = request.state.user or "dashboard"
    ops = _ops_db()
    threading.Thread(target=lambda: scout.promote(DB(settings.database_url, init=False), ops, ids, settings, actor),
                     daemon=True).start()
    return back(request, "/scout", f"Making {len(ids)} moment(s) into drafts — they'll appear in the Approval queue shortly.")


@app.post("/scout/{clip_id:path}/dismiss")
def scout_dismiss(request: Request, clip_id: str, reason: str = Form("")):
    from .. import scout
    _admin(request)
    scout.dismiss(db(), clip_id, request.state.user or "dashboard", reason)
    return back(request, "/scout", "Dismissed")


@app.post("/scout/scan")
def scout_scan(request: Request):
    import threading
    from .. import scout
    _admin(request)
    ops = _ops_db()
    threading.Thread(target=lambda: scout.scan(DB(settings.database_url, init=False), ops, actor=request.state.user or "dashboard"),
                     daemon=True).start()
    return back(request, "/scout", "Scanning the library — refresh in a minute.")


@app.post("/variant/{vid:path}/cuts/render")
def variant_cuts_render(request: Request, vid: str, use_ops: str = Form("on")):
    """Video Production: all three cuts. With use_ops, ask the ops pipeline for subject-tracked 9:16 versions
    first (they render automatically when ready); otherwise render now with the agent's own crop."""
    import threading
    from ..media import render_cuts
    from ..ops_vertical import request_cuts
    actor = request.state.user or "dashboard"
    if use_ops == "on":
        try:
            r = request_cuts(db(), vid, actor)
        except Exception as e:  # noqa: BLE001
            return back(request, f"/variant/{vid}", err=f"ops vertical request failed: {str(e)[:200]}")
        if r.get("requested"):
            return back(request, f"/variant/{vid}", f"Asked ops for subject-tracked 9:16 cuts {', '.join(r['requested'])} — "
                                                    "they render here automatically when ready (a few minutes).")
    threading.Thread(target=lambda: render_cuts(DB(settings.database_url, init=False), vid, settings, actor),
                     daemon=True).start()
    return back(request, f"/variant/{vid}", "Rendering all cuts — refresh in a couple of minutes.")


@app.post("/variant/{vid:path}/cut")
def variant_cut(request: Request, vid: str, label: str = Form(...)):
    from ..creative_director import use_cut
    try:
        use_cut(db(), vid, label, request.state.user or "dashboard")
    except ValueError as e:
        return back(request, f"/variant/{vid}", err=str(e))
    return back(request, f"/variant/{vid}", f"Switched to cut {label} — render it and review again.")


@app.get("/migration", response_class=HTMLResponse)
def migration(request: Request):
    d = db()
    mig.sync_decisions(d)
    return page(request, "migration.html", sheets=mig.decision_sheet(d),
                proposals=d.all("profile_change_proposals", order="account_id, field"),
                plan=[t for t in mig.ninety_day_plan() if t["phase"] != "report"],
                ann=mig.announcement_variants("instagram"), report=mig.weekly_report(d))


@app.post("/migration/approve")
def migration_approve(request: Request, platform: str = Form(...), actor: str = Form("")):
    try:
        mig.approve_decision(db(), platform, who(request, actor))
    except WorkflowError as e:
        return back(request, "/migration", err=str(e))
    return back(request, "/migration", f"{platform} decision approved")


@app.post("/migration/propose")
def migration_propose(request: Request, actor: str = Form("")):
    n = len(mig.propose_profile_changes(db(), request.state.user or actor or "dashboard"))
    return back(request, "/migration", f"{n} proposals created")


@app.post("/migration/proposal/{pid}")
def migration_review(request: Request, pid: int, decision: str = Form(...), actor: str = Form("")):
    try:
        mig.review_proposal(db(), pid, decision, who(request, actor))
        return back(request, "/migration", f"Proposal #{pid} → {decision}")
    except (PermissionError, WorkflowError) as e:
        return back(request, "/migration", err=str(e))


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    return page(request, "audit.html", rows=db().all("audit_log", order="id DESC")[:300])


@app.post("/internal/tick")
def internal_tick(ops: int = 0):
    """One worker pass (publisher + status poll; `?ops=1` also refreshes from the ops portal).
    Called by EventBridge Scheduler: App Runner throttles CPU between requests, so a background
    thread is not a reliable clock there."""
    from ..worker import tick
    r = tick(db(), settings, bool(ops))
    return {"ok": True, "planned": r["planned"], "publisher": r["publisher"],
            "ops_clips": (r.get("ops_clips") or {}).get("imported")}


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run, "publishing_enabled": settings.publishing_enabled,
            "kill_switch": kill_switch_on(db()), "time": datetime.now(timezone.utc).isoformat()}
