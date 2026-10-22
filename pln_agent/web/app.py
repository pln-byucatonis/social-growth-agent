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
from ..accounts import focus as acc_focus
from ..accounts import publish_eligibility
from ..adapters import get_adapter
from ..calendar_planner import plan_week
from ..config import load_settings
from ..db import DB, now_iso
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
from .ui import register as _register_ui  # noqa: E402
fmt = _register_ui(templates, settings, lambda: db())
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
async def read_cache(request: Request, call_next):
    """Pages read through one per-request cache: on the Data API every statement is a ~40 ms round trip."""
    if request.method not in ("GET", "HEAD") or request.url.path in ("/healthz", "/internal/tick"):
        return await call_next(request)
    with db().request_cache():
        return await call_next(request)


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


BACKGROUND = True  # tests set False to run slow actions inline on the injected database


def run_later(name: str, fn, actor: str = "") -> None:
    """Run fn(db) after the response: the ops proxy gives a request 25 seconds. Progress lands in
    settings_kv bg:<name> so the page can show it."""
    import json as _json
    import threading

    def work(d):
        d.kv_set(f"bg:{name}", _json.dumps({"state": "running", "started": now_iso()}), actor or "dashboard")
        try:
            msg = fn(d)
            d.kv_set(f"bg:{name}", _json.dumps({"state": "done", "finished": now_iso(), "msg": msg or ""}), actor or "dashboard")
        except Exception as e:  # noqa: BLE001 — surfaced on the page
            d.kv_set(f"bg:{name}", _json.dumps({"state": "failed", "finished": now_iso(), "msg": str(e)[:300]}), actor or "dashboard")
    if not BACKGROUND:
        work(db())
        return
    threading.Thread(target=lambda: work(DB(settings.database_url, init=False)), daemon=True, name=f"bg-{name}").start()


def bg_status(name: str) -> dict | None:
    import json as _json
    raw = db().kv_get(f"bg:{name}")
    return _json.loads(raw) if raw else None


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
    from .. import growth_officer as cgo
    d = db()
    counts = {r["state"]: r["n"] for r in d.query("SELECT state, COUNT(*) AS n FROM variants GROUP BY state")}
    jobs = d.all("publish_jobs", order="updated_at DESC")[:12]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    upcoming = d.query("SELECT s.variant_id, s.account_id, s.slot_at, v.hook FROM schedules s LEFT JOIN variants v ON v.variant_id=s.variant_id "
                       "WHERE s.state='scheduled' AND s.slot_at >= ? ORDER BY s.slot_at", [now.isoformat()])[:8]
    next24 = sum(1 for u in upcoming if u["slot_at"] < (now + timedelta(hours=24)).isoformat())
    esc = d.one("SELECT COUNT(*) AS n FROM comments WHERE status='escalated'")["n"]
    waiting = d.one("SELECT COUNT(*) AS n FROM comments WHERE status IN ('drafted','needs_answer')")["n"]
    failed = sum(1 for j in d.all("publish_jobs", "state IN ('failed','blocked')"))
    last = d.one("SELECT MAX(published_at) AS m FROM posts p JOIN accounts a ON a.account_id=p.account_id WHERE a.brand_id IN ('pln','carjitsu','putt_tour')")
    brief = cgo.latest_brief(d) or {}
    actions = [a for a in brief.get("actions", []) if a["priority"] in ("P0", "P1")][:8]
    return page(request, "home.html", counts=counts, jobs=jobs, recs=recommendations(d)[:12], upcoming=upcoming, next24=next24,
                esc=esc, waiting_replies=waiting, failed=failed, last_post=(last or {}).get("m"), actions=actions, brief=brief)


@app.post("/kill")
def kill(request: Request, state: str = Form(...), actor: str = Form(""), reason: str = Form("")):
    try:
        set_kill_switch(db(), state == "on", who(request, actor), reason)
    except WorkflowError as e:
        return back(request, "/", err=str(e))
    return back(request, "/", f"Emergency stop {state.upper()}")


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, platform: str = "", state: str = "", brand: str = ""):
    d = db()
    where, params = ["1=1"], []
    if platform:
        where.append("platform=?"), params.append(platform)
    if brand:
        where.append("brand_id=?"), params.append(brand)
    base_cond, base_params = " AND ".join(where), list(params)
    state_counts = {r["state"]: r["n"] for r in d.query(f"SELECT state, COUNT(*) AS n FROM variants WHERE {base_cond} GROUP BY state", base_params)}
    if state:
        where.append("state=?"), params.append(state)
    cond = " AND ".join(where)
    total = d.one(f"SELECT COUNT(*) AS n FROM variants WHERE {cond}", params)["n"]
    vs = [d.decode("variants", r) for r in d.query(
        "SELECT variant_id, asset_id, account_id, platform, state, hook, score, risk_flags, validation, caption_hash "
        "FROM variants "
        f"WHERE {cond} ORDER BY score DESC, platform LIMIT {QUEUE_LIMIT}", params)]
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
    brands = [r["brand_id"] for r in d.query("SELECT DISTINCT brand_id FROM variants ORDER BY brand_id")]
    return page(request, "queue.html", variants=vs, accs=accs, perm=perm, platform=platform, state=state, thumbs=thumbs,
                platforms=platforms, total=total, limit=QUEUE_LIMIT, brand=brand, brands=brands, state_counts=state_counts)


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
    render_url = _render_url(request, v)
    from ..overlays import style_for
    ov = style_for(v.get("brand_id", ""), (v.get("treatment") or {}).get("overlay"))
    ov["hook_text"] = next((o["text"] for o in v.get("onscreen_text") or [] if not (o.get("position") or "").endswith("small")), "")
    preview = get_adapter(acc["platform"], settings=settings).simulate(v, acc, "preview")
    from ..media import asset_preview
    source = asset_preview(asset, (v.get("edl") or {}).get("segments"))
    from .. import carousel
    slides = carousel.preview_urls(v) if carousel.is_slides(v) else []
    siblings = {k: d.get("variants", "variant_id", f"{vid}~{k}") for k in carousel.SLIDE_TYPES} \
        if (not carousel.is_slides(v) and v["platform"] in carousel.PLATFORMS) else {}
    return page(request, "variant.html", v=v, acc=acc, asset=asset, live_ok=ok, reasons=reasons, approvals=approvals,
                source=source, slides=slides, siblings=siblings,
                audit=audit, jobs=jobs, planned=planned, render_url=render_url, ov=ov,
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
        return back(request, f"/variant/{vid}", f"Scheduled job {j['job_id']}" + (f" — {j['moved']}" if j.get("moved") else ""))
    except (WorkflowError, ValueError) as e:
        return back(request, f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/cancel")
def do_cancel(request: Request, vid: str, actor: str = Form(""), reason: str = Form("cancelled from dashboard")):
    cancel(db(), vid, request.state.user or actor or "dashboard", reason)
    return back(request, f"/variant/{vid}", "Cancelled")


@app.post("/variant/{vid:path}/overlay")
def variant_overlay(request: Request, vid: str, hook_text: str = Form(""), hook: str = Form("pill"), captions: str = Form("kinetic"),
                    stamp: str = Form(""), countdown: str = Form(""), actor: str = Form("")):
    """Change the words burned into the video and their look, then re-render. The video changes, so an approval
    is revoked (approvals bind to what was reviewed)."""
    d = db()
    v = d.get("variants", "variant_id", vid)
    if not v:
        raise HTTPException(404)
    if v["state"] in ("scheduled", "publishing", "published", "simulated"):
        return back(request, f"/variant/{vid}", err=f"can't change the video of a {v['state']} post — unschedule it first")
    if hook not in ("knockout", "pill", "outline") or captions not in ("kinetic", "classic", "none"):
        return back(request, f"/variant/{vid}", err="unknown overlay style")
    name = who(request, actor)
    ot = [dict(o) for o in (v.get("onscreen_text") or [])]
    if hook_text.strip():
        if ot and not (ot[0].get("position") or "").endswith("small"):
            ot[0]["text"] = hook_text.strip()[:80]
        else:
            ot.insert(0, {"start": 0.0, "end": 3.0, "text": hook_text.strip()[:80], "position": "top"})
    t = dict(v.get("treatment") or {})
    t["overlay"] = {"hook": hook, "captions": captions, "stamp": stamp.strip()[:16], "countdown": countdown == "on",
                    "hook_custom": bool(hook_text.strip())}  # a typed hook is used as written
    changes = {"onscreen_text": ot, "treatment": t, "render_status": "queued", "updated_at": now_iso()}
    if v["state"] == "approved":
        changes["state"] = "in_review"
        d.insert("approvals", {"variant_id": vid, "decision": "revoke", "reviewer": name, "note": "on-screen text/style changed",
                               "caption_hash": v["caption_hash"], "at": now_iso()})
    d.update("variants", "variant_id", vid, changes)
    d.audit(name, "variant_overlay_changed", "variants", vid, {"hook_text": hook_text.strip()[:80], **t["overlay"]})
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    else:  # local / tests: render now
        from ..media import render_queued
        if not BACKGROUND:
            render_queued(d, settings, actor=name)
        else:
            import threading
            threading.Thread(target=lambda: render_queued(DB(settings.database_url, init=False), settings, actor=name),
                             daemon=True).start()
    return back(request, f"/variant/{vid}", "Saved — re-rendering with the new on-screen text and style (about two minutes).")


@app.post("/variant/{vid:path}/render")
def do_render(request: Request, vid: str):
    """Renders take longer than the portal proxy's 25s window, so run them in the background."""
    import threading
    d = db()
    v = d.get("variants", "variant_id", vid)
    if not v:
        raise HTTPException(404)
    if v.get("render_status") in ("rendering", "queued"):
        return back(request, f"/variant/{vid}", "Already rendering — refresh in a minute.")
    actor = request.state.user or "dashboard"
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):  # App Runner throttles background CPU: the runner renders it
        d.update("variants", "variant_id", vid, {"render_status": "queued", "updated_at": now_iso()})
        _kick_team_tasks()
        return back(request, f"/variant/{vid}", "Queued for rendering — about two minutes. Refresh to see it.")
    d.update("variants", "variant_id", vid, {"render_status": "rendering"})

    def work():
        own = DB(settings.database_url, init=False)  # separate connection for the worker thread
        try:
            render_variant(own, vid, settings, actor=actor)
        except Exception as e:  # never leave a variant stuck in "rendering"
            own.update("variants", "variant_id", vid, {"render_status": "render_failed"})
            own.audit(actor, "variant_render_failed", "variants", vid, {"error": str(e)[:300]})

    threading.Thread(target=work, daemon=True, name=f"render-{vid[:20]}").start()
    return back(request, f"/variant/{vid}", "Rendering started — this takes about a minute. Refresh to see it.")


@app.post("/variant/{vid:path}/writers/{i}")
def do_writers_pick(request: Request, vid: str, i: int):
    """Switch the post to another of the writers' room drafts (re-renders with its hook)."""
    from .. import writers_room
    try:
        r = writers_room.switch(db(), vid, i, request.state.user or "dashboard")
    except (ValueError, WorkflowError) as e:
        return back(request, f"/variant/{vid}", err=str(e))
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    return back(request, f"/variant/{vid}", f"Using “{r['hook']}” — re-rendering.")


@app.post("/variant/{vid:path}/writers")
def do_writers_run(request: Request, vid: str):
    """Send this post (back) to the writers' room; the task runner writes it."""
    db().update("variants", "variant_id", vid, {"writers_status": "pending"})
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    return back(request, f"/variant/{vid}", "Sent to the writers' room — five drafts in a minute or two.")


@app.post("/variant/{vid:path}/make/{kind}")
def do_make_slides(request: Request, vid: str, kind: str):
    """A carousel or Story version of this Reel: a new post, reviewed and approved on its own."""
    from .. import carousel
    try:
        r = carousel.make(db(), vid, kind, request.state.user or "dashboard")
    except carousel.CarouselError as e:
        return back(request, f"/variant/{vid}", err=str(e))
    if r["created"] and os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    elif r["created"]:
        db().update("variants", "variant_id", r["variant_id"], {"render_status": "rendering"})
        render_variant(db(), r["variant_id"], settings, actor=request.state.user or "dashboard")
    note = "Created — rendering its slides (about a minute)." if r["created"] else "Already made — here it is."
    return back(request, f"/variant/{r['variant_id']}", note)


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


def _render_url(request: Request, v: dict) -> str:
    """A playable URL for the post's finished render (presigned when it lives in S3)."""
    path = v.get("render_path") or ""
    if path.startswith("s3://"):
        from ..media import resolve_media
        return resolve_media(path)
    if path:
        try:
            return base(request) + "/renders/" + str(Path(path).relative_to(settings.render_dir))
        except ValueError:
            return ""
    return ""


@app.get("/runsheet", response_class=HTMLResponse)
def runsheet_page(request: Request, day: str = "", all: int = 0):
    from .. import runsheet
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    d = (today + timedelta(days=1)) if day == "tomorrow" else (date.fromisoformat(day) if day else today)
    sheet = runsheet.day_sheet(db(), settings, d, focus=not all)
    for r in sheet["rows"]:  # play-in-place preview, with sound, before approving
        v = db().get("variants", "variant_id", r["variant_id"]) or {}
        r["render_url"] = _render_url(request, v) if v.get("render_status") == "rendered" else ""
    from ..team_tasks import pins_for_day
    challenge_vids = {p["variant_id"] for p in pins_for_day(db(), d.isoformat(), settings.timezone)}
    props = [r for r in sheet["rows"] if r["kind"] == "proposed"]
    pinned = [r for r in props if r.get("pinned")]
    # what needs a decision: posts a team challenge pinned for the day; on days without any, the planner's proposals
    approve = pinned if challenge_vids else props
    return page(request, "runsheet.html", s=sheet, day=d, today=today, prev=(d - timedelta(days=1)).isoformat(),
                next=(d + timedelta(days=1)).isoformat(), bg=bg_status("runsheet"), approve=approve,
                # a day a team challenge planned shows only those posts; the planner's extras are one click away
                suggestions=[r for r in props if r not in approve] if (all or not challenge_vids) else [],
                hidden=0 if (all or not challenge_vids) else len([r for r in sheet["rows"] if r["variant_id"] not in challenge_vids]),
                scheduled=[r for r in sheet["rows"] if r["kind"] == "scheduled" and (all or not challenge_vids or r["variant_id"] in challenge_vids)],
                now=datetime.now(ZoneInfo(settings.timezone)))


@app.post("/runsheet/lock")
def runsheet_lock(request: Request, pick: list[str] = Form([]), day: str = Form(""), actor: str = Form("")):
    from .. import runsheet
    try:
        reviewer = who(request, actor)
    except WorkflowError as e:
        return back(request, f"/runsheet?day={day}", err=str(e))
    picks = [tuple(p.split("|", 1)) for p in pick if "|" in p]
    if not picks:
        return back(request, f"/runsheet?day={day}", err="Tick at least one post to lock in.")

    def work(d):
        r = runsheet.lock(d, picks, reviewer)
        msg = f"Scheduled {len(r['scheduled'])} post(s)"
        if r["needs_second_approver"]:
            msg += f"; {len(r['needs_second_approver'])} need a second approver (rights policy)"
        if r["moved"]:
            msg += "; moved a day (daily limit): " + "; ".join(m["variant_id"] for m in r["moved"][:4])
        if r["failed"]:
            msg += "; not scheduled: " + "; ".join(f"{f['variant_id']}: {f['error']}" for f in r["failed"][:4])
        return msg
    run_later("runsheet", work, reviewer)
    return back(request, f"/runsheet?day={day}", "Locking in the selected posts…")


@app.post("/runsheet/unschedule")
def runsheet_unschedule(request: Request, vid: str = Form(...), day: str = Form(""), actor: str = Form("")):
    from .. import runsheet
    try:
        runsheet.unschedule(db(), vid, who(request, actor))
    except WorkflowError as e:
        return back(request, f"/runsheet?day={day}", err=str(e))
    return back(request, f"/runsheet?day={day}", "Unscheduled — it's back to approved.")


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


@app.get("/predct", response_class=HTMLResponse)
def predct_page(request: Request, show: str = "", view: str = "new"):
    """PREDCT curation queue: watch the footage, pick the moments, write what happens. Nothing posts without a pick."""
    from .. import predct
    from ..media import resolve_media
    d = db()
    where = {"new": "status IN ('new','picked')", "picked": "status='picked'", "dismissed": "status='dismissed'"}.get(view, "status IN ('new','picked')")
    params: list = []
    if show:
        where += " AND show=?"
        params.append(show)
    rows = d.all("predct_candidates", where, params, order="recorded_at DESC", limit=60)
    for r in rows:
        r["preview_url"] = resolve_media(r["preview_path"], "video/mp4") if r.get("preview_path") else ""
        r["thumb_url"] = resolve_media(r["thumb_path"], "image/jpeg") if r.get("thumb_path") else ""
    pol = predct.policy()
    accts = [d.get("accounts", "account_id", a) or {"account_id": a, "verification_status": "missing"} for a in pol["accounts"]]
    counts = {r["status"]: r["n"] for r in d.query("SELECT status, COUNT(*) AS n FROM predct_candidates GROUP BY status")}
    return page(request, "predct.html", rows=rows, shows=pol["shows"], show=show, view=view, accts=accts, counts=counts,
                never_say=pol.get("never_say") or [], voice=pol.get("voice", ""))


@app.post("/predct/scan")
def predct_scan(request: Request):
    from .. import predct
    r = predct.scan(db(), actor=request.state.user or "dashboard")
    if r["new"] and os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    return back(request, "/predct", f"Found {len(r['new'])} new — previews in a few minutes." if r["new"] else "Nothing new.")


@app.post("/predct/add")
async def predct_add(request: Request):
    """Upload a show file (stored in the render bucket's PREDCT inbox) or register an s3:// link."""
    import tempfile
    import uuid
    from .. import predct
    form = await request.form()
    show, link, title = form.get("show") or "other", (form.get("link") or "").strip(), (form.get("title") or "").strip()
    f = form.get("file")
    try:
        if f is not None and getattr(f, "filename", ""):
            bucket = os.environ.get("PLN_RENDER_BUCKET")
            if not bucket:
                raise predct.CurationError("uploads need PLN_RENDER_BUCKET (paste an s3:// link instead)")
            name = re.sub(r"[^A-Za-z0-9._-]+", "-", f.filename)[-80:]
            key = f"social-agent/predct/inbox/{show}/{uuid.uuid4().hex[:8]}-{name}"
            with tempfile.NamedTemporaryFile() as tmp:
                size = 0
                while chunk := await f.read(1 << 20):
                    size += len(chunk)
                    if size > 1_500_000_000:
                        raise predct.CurationError("over 1.5 GB: trim it first")
                    tmp.write(chunk)
                tmp.flush()
                import boto3
                boto3.client("s3").upload_file(tmp.name, bucket, key, ExtraArgs={"ContentType": f.content_type or "video/mp4"})
            link = f"s3://{bucket}/{key}"
        cid = predct.add_file(db(), link, show, title or (getattr(f, "filename", "") or ""), request.state.user or "dashboard")
    except predct.CurationError as e:
        return back(request, "/predct", err=str(e))
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    return back(request, f"/predct#{cid}", "Added — the preview is being made.")


@app.post("/predct/{cid}/pick")
def predct_pick(request: Request, cid: str, start: float = Form(...), end: float = Form(...), note: str = Form(...),
                payoff_at: str = Form("")):
    from .. import predct
    try:
        r = predct.pick(db(), cid, start, end, note, request.state.user or "dashboard",
                        float(payoff_at) if payoff_at.strip() else None)
    except (predct.CurationError, ValueError) as e:
        return back(request, f"/predct#{cid}", err=str(e))
    if os.environ.get("PLN_TASK_RUNNER_PROJECT"):
        _kick_team_tasks()
    return back(request, f"/predct#{cid}", f"Picked ({r['pick_id']}) — cutting it and drafting the posts; they'll be in Approvals.")


@app.post("/predct/{cid}/dismiss")
def predct_dismiss(request: Request, cid: str):
    from .. import predct
    predct.dismiss(db(), cid, request.state.user or "dashboard")
    return back(request, "/predct", "Dismissed")


def _tiktok_page(request: Request, **kw):
    d = db()
    tiktok = [a for a in d.all("accounts", "platform='tiktok'", order="account_id")
              if a["account_id"] not in set(acc_focus().get("removed") or [])]
    recent = d.all("audit_log", "action='tiktok_export_imported'", order="id DESC", limit=10)
    kw.setdefault("account_id", next((a["account_id"] for a in tiktok if a["account_id"] in set(acc_focus().get("worked") or [])), ""))
    return page(request, "tiktok_export.html", tiktok=tiktok, recent=recent, **kw)


@app.get("/accounts/tiktok-export", response_class=HTMLResponse)
def tiktok_export_page(request: Request):
    return _tiktok_page(request)


@app.post("/accounts/tiktok-export", response_class=HTMLResponse)
async def tiktok_export_read(request: Request):
    import base64
    from .. import tiktok_export
    form = await request.form()
    f = form.get("file")
    data = await f.read() if f is not None and hasattr(f, "read") else b""
    if len(data) > 5_000_000:
        return back(request, "/accounts/tiktok-export", err="That file is over 5 MB — export one tab at a time")
    try:
        parsed = tiktok_export.parse(f.filename or "export.csv", data)
    except (tiktok_export.ExportError, ValueError, KeyError, IndexError) as e:
        return back(request, "/accounts/tiktok-export", err=f"Couldn't read it: {e}")
    names = sorted({m for r in parsed["rows"] for m in r["metrics"]})
    return _tiktok_page(request, parsed=parsed, filename=f.filename, data_b64=base64.b64encode(data).decode(),
                        account_id=form.get("account_id") or "", metric_names=names)


@app.post("/accounts/tiktok-export/save")
def tiktok_export_save(request: Request, account_id: str = Form(...), filename: str = Form(""), data: str = Form(...)):
    import base64
    from .. import tiktok_export
    try:
        parsed = tiktok_export.parse(filename, base64.b64decode(data))
        r = tiktok_export.save(db(), account_id, parsed, request.state.user or "dashboard")
    except (tiktok_export.ExportError, ValueError) as e:
        return back(request, "/accounts/tiktok-export", err=str(e))
    what = f"{r['posts']} videos, " if r["posts"] else ""
    return back(request, "/accounts/tiktok-export", f"Saved {what}{r['metrics']} numbers for {account_id}.")


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
    return page(request, "growth.html", b=brief, shares=cgo.current_shares(d), bg=bg_status("growth_brief"))


@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    from .. import team
    d = db()
    r = team.roster(d)
    for a in r:
        a["status"] = team.status_line(d, a["key"])
    from .. import team_tasks
    all_tasks = team_tasks.tasks(d)
    return page(request, "team.html", agents=r, challenges=team.threads(d, "team", 10), llm=team.llm_status(),
                team_directives=d.all("agent_directives", "status='active' AND agent_key='team'", order="created_at"),
                open_tasks=[t for t in all_tasks if t["status"] in team_tasks.OPEN or t["status"] == "failed"],
                done_tasks=[t for t in all_tasks if t["status"] == "done"][-20:], names={a["key"]: a["name"] for a in team.AGENTS})


@app.get("/team/agent/{key}", response_class=HTMLResponse)
def team_agent(request: Request, key: str, thread: str = ""):
    from .. import team
    d = db()
    if key not in team.BY_KEY:
        raise HTTPException(404, "unknown agent")
    a = next(x for x in team.roster(d) if x["key"] == key)
    a["status"] = team.status_line(d, key)
    chats = team.threads(d, key)
    cur = thread or (chats[0]["thread_id"] if chats else "")
    th = d.get("team_threads", "thread_id", cur) if cur and cur != "new" else None
    from .. import team_tasks
    return page(request, "team_agent.html", a=a, chats=chats, thread=cur, msgs=team.messages(d, cur) if th else [], llm=team.llm_status(),
                tasks=[t for t in team_tasks.tasks(d, owner=key) if t["status"] != "done"][-8:], names={x["key"]: x["name"] for x in team.AGENTS},
                bg={"state": "running", "label": f"{a['name']} is thinking"} if th and th["status"] == "thinking" else bg_status(f"chat-{cur}"))


@app.post("/team/agent/{key}/chat")
def team_chat(request: Request, key: str, text: str = Form(...), thread: str = Form(""), actor: str = Form("")):
    from .. import team
    try:
        name = who(request, actor)
        t = team.ask(db(), key, text, name, thread)
    except (KeyError, WorkflowError) as e:
        return back(request, f"/team/agent/{key}", err=str(e))
    run_later(f"chat-{t['thread_id']}", lambda d: team.answer(d, key, t["thread_id"], name), name)
    return back(request, f"/team/agent/{key}?thread={t['thread_id']}")


@app.post("/team/agent/{key}/param")
def team_param(request: Request, key: str, name: str = Form(...), value: str = Form(""), reset: str = Form(""),
               actor: str = Form("")):
    from .. import team, team_params
    _admin(request)
    try:
        if reset:
            team_params.clear(db(), key, name, who(request, actor))
            return back(request, f"/team/agent/{key}", f"{name} reset to default")
        v = team.set_param(db(), key, name, value, who(request, actor))
    except (KeyError, ValueError, WorkflowError) as e:
        return back(request, f"/team/agent/{key}", err=str(e))
    return back(request, f"/team/agent/{key}", f"{name} = {v} — used from the agent's next run")


@app.post("/team/directive")
def team_directive(request: Request, agent: str = Form(...), text: str = Form(...), actor: str = Form(""),
                   back_to: str = Form("/team")):
    from .. import team
    try:
        team.add_directive(db(), agent, text, who(request, actor))
    except (ValueError, WorkflowError) as e:
        return back(request, back_to, err=str(e))
    return back(request, back_to, "Directive added")


@app.post("/team/directive/{did}/retire")
def team_directive_retire(request: Request, did: str, actor: str = Form(""), back_to: str = Form("/team")):
    from .. import team
    try:
        team.retire_directive(db(), did, who(request, actor))
    except WorkflowError as e:
        return back(request, back_to, err=str(e))
    return back(request, back_to, "Directive retired")


@app.post("/team/challenge")
def team_challenge(request: Request, text: str = Form(...), agents: list[str] = Form([]), actor: str = Form("")):
    from .. import team
    try:
        t = team.start_challenge(db(), text, who(request, actor), agents or None,
                                 db_factory=lambda: DB(settings.database_url, init=False))
    except WorkflowError as e:
        return back(request, "/team", err=str(e))
    return back(request, f"/team/challenge/{t['thread_id']}", "Challenge sent — the team is working on it")


@app.get("/team/challenge/{tid}", response_class=HTMLResponse)
def team_challenge_view(request: Request, tid: str):
    from .. import team
    d = db()
    t = d.get("team_threads", "thread_id", tid)
    if not t:
        raise HTTPException(404, "no such challenge")
    from .. import team_tasks
    names = {a["key"]: a["name"] for a in team.AGENTS}
    tk = team_tasks.tasks(d, tid)
    return page(request, "team_challenge.html", t=t, msgs=team.messages(d, tid), names=names, tasks=tk,
                tasks_by_index={x["action_index"]: x for x in tk}, has_open=any(x["status"] in team_tasks.OPEN for x in tk))


@app.post("/team/challenge/{tid}/adopt")
def team_adopt(request: Request, tid: str, index: int = Form(...), apply_param: str = Form(""), actor: str = Form("")):
    from .. import team
    try:
        if apply_param:
            _admin(request)
        r = team.adopt(db(), tid, index, who(request, actor), bool(apply_param))
    except (ValueError, KeyError, WorkflowError) as e:
        return back(request, f"/team/challenge/{tid}", err=str(e))
    _kick_team_tasks()
    return back(request, f"/team/challenge/{tid}", f"Adopted: a directive for {r['directive']['agent_key']} and a tracked task "
                f"({r['task']['kind']}) — progress shows below" + (f"; parameter set to {r['param']}" if "param" in r else ""))


def _kick_team_tasks() -> None:
    from .. import team_tasks
    if BACKGROUND:
        team_tasks.start(settings)
    else:
        team_tasks.advance(db(), settings, _ops_db() if settings.ops_database_url else None, hooks=TEAM_TASK_HOOKS)


TEAM_TASK_HOOKS = None  # tests inject fakes for promote / MediaConvert / render


@app.post("/team/tasks/run")
def team_tasks_run(request: Request, back_to: str = Form("/team")):
    _kick_team_tasks()
    return back(request, (back_to if back_to.startswith("/team") else "/team") + "#tasks",
                "Checking every open task now — refresh in a minute for renders.")


@app.post("/team/task/{task_id}/mark")
def team_task_mark(request: Request, task_id: str, status: str = Form(...), note: str = Form(""), actor: str = Form("")):
    from .. import team_tasks
    t = db().get("team_tasks", "task_id", task_id)
    if not t:
        raise HTTPException(404, "no such task")
    try:
        team_tasks.mark(db(), task_id, status, who(request, actor), note)
    except (ValueError, WorkflowError) as e:
        return back(request, f"/team/challenge/{t['thread_id']}#tasks", err=str(e))
    if status == "todo":
        _kick_team_tasks()
    return back(request, f"/team/challenge/{t['thread_id']}#{task_id}", {"done": "Marked done", "todo": "Retrying", "blocked": "Marked blocked"}[status])


@app.get("/experiments", response_class=HTMLResponse)
def experiments_page(request: Request):
    from .. import growth_lab as lab
    d = db()
    exps = [e for e in d.all("experiments", order="week_start DESC") if (e.get("result") or {}).get("mode") or e["variable"] == "frequency"]
    counts = {}
    for r in d.query("SELECT experiment_id, arm, COUNT(*) AS n FROM experiment_arms GROUP BY experiment_id, arm"):
        counts.setdefault(r["experiment_id"], {})[r["arm"]] = r["n"]
    accs = [a for a in d.all("accounts", order="account_id") if not a.get("hard_block_reason")]
    from .. import peer_cadence
    from ..runsheet import focus_accounts
    keep = (focus_accounts(d) or {}).get("connected") or [a["account_id"] for a in accs]
    cadence = []
    if d.one("SELECT 1 AS x FROM competitor_snapshots"):
        for aid in [a for a in keep if ".instagram." in a]:
            try:
                cadence.append(peer_cadence.for_account(d, aid, tz=settings.timezone))
            except Exception as e:  # noqa: BLE001 — one account's data must not break the page
                cadence.append({"account_id": aid, "error": str(e)[:160]})
    return page(request, "experiments.html", exps=exps, counts=counts, proposals=lab.propose(d), accs=accs, bg=bg_status("experiments"),
                outcomes=lab.RATE_OUTCOMES, cadence=cadence)


@app.post("/experiments/cadence/{account_id}")
def experiments_cadence(request: Request, account_id: str):
    """Timing test against the growing benchmarks' posting window + the three-week frequency test."""
    from .. import peer_cadence
    _admin(request)
    try:
        r = peer_cadence.plan_tests(db(), account_id, request.state.user or "dashboard", tz=settings.timezone)
    except (KeyError, ValueError, WorkflowError) as e:
        return back(request, "/experiments", err=str(e))
    bits = ([f"timing test {r['timing']}"] if r["timing"] else []) + ([f"{len(r['frequency'])}-week frequency test"] if r["frequency"] else [])
    return back(request, "/experiments#cadence", ("Started " + " and ".join(bits) + ". " if bits else "") + " ".join(r["notes"]))


@app.post("/experiments/new")
def experiments_new(request: Request, account_id: str = Form(...), variable: str = Form(...), mode: str = Form("assigned"),
                    arm_a: str = Form(...), arm_b: str = Form(...), arm_c: str = Form(""), hypothesis: str = Form(...),
                    outcome: str = Form("engagements_per_1k_views"), actor: str = Form("")):
    from .. import growth_lab as lab
    given = [x.strip() for x in (arm_a, arm_b, arm_c) if x.strip()]
    if variable == "cut":  # arms are the cut labels themselves
        arms = {x.upper(): f"cut {x.upper()}" for x in given}
    else:
        arms = dict(zip("ABC", given))
    try:
        e = lab.create(db(), account_id, variable, arms, hypothesis, who(request, actor), mode=mode, outcome=outcome)
    except (KeyError, ValueError, WorkflowError) as ex:
        return back(request, "/experiments", err=str(ex))
    n = db().one("SELECT COUNT(*) AS n FROM experiment_arms WHERE experiment_id=?", [e["experiment_id"]])["n"]
    return back(request, "/experiments", f"Started {e['experiment_id']}" + (f" — {n} draft(s) assigned to arms" if mode == "assigned" else ""))


@app.post("/experiments/read")
def experiments_read(request: Request):
    from .. import growth_lab as lab
    actor = request.state.user or "dashboard"

    def work(d):
        r = lab.refresh(d, actor=actor)
        return f"Read {r['read']} experiment(s); {r['concluded']} concluded; {r['assigned']} new draft(s) assigned"
    run_later("experiments", work, actor)
    return back(request, "/experiments", "Reading experiments in the background — this page updates when it's done.")


@app.post("/experiments/{eid}/stop")
def experiments_stop(request: Request, eid: str, reason: str = Form(""), actor: str = Form("")):
    from .. import growth_lab as lab
    try:
        lab.stop(db(), eid, who(request, actor), reason or "stopped from dashboard")
    except WorkflowError as ex:
        return back(request, "/experiments", err=str(ex))
    return back(request, "/experiments", f"Stopped {eid}")


@app.get("/competitors", response_class=HTMLResponse)
def competitors_page(request: Request, brand: str = ""):
    from .. import competitive as ci
    d = db()
    hist = {}
    for r in d.all("competitor_snapshots", order="captured_at"):
        hist.setdefault(r["handle"], []).append(r)
    return page(request, "competitors.html", bg=bg_status("competitors"), rows=ci.latest(d), hist=hist, gaps=ci.gaps(d), standouts=ci.standouts(d, brand),
                cfg=ci.config(), brand=brand)


@app.post("/competitors/refresh")
def competitors_refresh(request: Request):
    from .. import competitive as ci
    _admin(request)
    actor = request.state.user or "dashboard"

    def work(d):
        r = ci.snapshot(d, actor=actor)
        errs = "; ".join(f"@{k}: {v}" for k, v in list(r["errors"].items())[:4])
        return f"Captured {len(r['captured'])} benchmark account(s)" + (f". Problems: {errs}" if errs else "")
    run_later("competitors", work, actor)
    return back(request, "/competitors", "Capturing benchmarks in the background — this page updates when it's done.")


@app.get("/community", response_class=HTMLResponse)
def community_page(request: Request, status: str = ""):
    import json as _json
    from .. import community as cm
    d = db()
    sts = tuple(x for x in status.split(",") if x) or ("escalated", "needs_answer", "drafted", "approved")
    rows = cm.queue(d, sts)
    accs = {a["account_id"]: a for a in d.all("accounts")}
    raw = d.kv_get("community:faqs")
    return page(request, "community.html", bg=bg_status("community"), rows=rows, accs=accs, s=cm.summary(d), faqs=_json.loads(raw) if raw else cm.faqs(d),
                status=",".join(sts), cfg=cm.config())


@app.post("/community/refresh")
def community_refresh(request: Request):
    from .. import community as cm
    _admin(request)
    actor = request.state.user or "dashboard"

    def work(d):
        r = cm.refresh(d, settings, actor=actor)
        errs = "; ".join(f"{k}: {v}" for k, v in list(r["errors"].items())[:3])
        return f"Fetched {r['new']} new comment(s)" + (f". Problems: {errs}" if errs else "")
    run_later("community", work, actor)
    return back(request, "/community", "Fetching comments in the background — this page updates when it's done.")


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
    actor = request.state.user or "dashboard"
    run_later("growth_brief", lambda d: (cgo.generate_brief(d, actor), "Brief regenerated from the latest data")[1], actor)
    return back(request, "/growth", "Regenerating the brief in the background — this page updates when it's done.")


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


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request, show: str = ""):
    from .. import trend_scout
    from ..accounts import load_brands
    d = db()
    cards = trend_scout.active(d) if show != "all" else d.all("trend_cards", order="updated_at DESC")
    brands = {b["brand_id"]: b["name"] for b in load_brands()}
    recent = d.all("trend_signals", order="seen_at DESC")[:30]
    import json as _json
    from .. import trend_brief
    brief = trend_brief.latest(d)
    return page(request, "trends.html", cards=cards, brands=brands, recent=recent, show=show, brief=brief,
                sources=_json.loads(d.kv_get("trend_scout:sources") or "{}"), bg=bg_status("trends"))


@app.post("/trends/refresh")
def trends_refresh(request: Request):
    from .. import trend_scout
    _admin(request)
    run_later("trends", lambda d: "Trend scan: " + ", ".join(f"{k} {v}" for k, v in trend_scout.refresh(d)["sources"].items()),
              request.state.user or "dashboard")
    return back(request, "/trends", "Scanning sources — refresh in a minute.")


@app.post("/trends/brief")
def trends_brief(request: Request):
    """Build this week's Monday brief now: fresh YouTube feeds and market prices, then the list."""
    from .. import team, trend_brief
    _admin(request)

    def work(d):
        trend_brief.collect(d)
        b = trend_brief.build(d, llm=team._llm, actor=request.state.user or "dashboard")
        return f"Monday brief: {len(b['make'])} to make, {len(b['clip'])} to clip"
    run_later("trends", work, request.state.user or "dashboard")
    return back(request, "/trends", "Building this week's brief — refresh in a minute.")


@app.post("/trends/manual")
def trends_manual(request: Request, lines: str = Form(...), actor: str = Form("")):
    from .. import trend_scout
    name = who(request, actor)
    n = trend_scout.add_manual(db(), lines, name)
    trend_scout.build_cards(db(), actor=name)
    return back(request, "/trends", f"Added {n} line(s) and rebuilt the trend cards.")


@app.post("/trends/{card_id:path}/apply")
def trends_apply(request: Request, card_id: str, brand_id: str = Form(...), hook: str = Form(""), actor: str = Form("")):
    from .. import trend_scout
    name = who(request, actor)
    try:
        r = trend_scout.apply(db(), card_id, brand_id, name, _ops_db() if settings.ops_database_url else None, settings, hook)
    except (KeyError, ValueError, LookupError, RuntimeError) as e:
        return back(request, "/trends", err=str(e))
    _kick_team_tasks()  # the render is queued for the task runner
    return back(request, f"/variant/{r['variant_id']}", f"Trend applied: “{r['hook']}” on {r['clip'].get('caption', '')[:60]} — "
                                                       "rendering now; it waits for your approval.")


@app.post("/trends/{card_id:path}/dismiss")
def trends_dismiss(request: Request, card_id: str, actor: str = Form("")):
    from .. import trend_scout
    trend_scout.dismiss(db(), card_id, who(request, actor))
    return back(request, "/trends", "Dismissed")


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
                ann=mig.announcement_variants("instagram"), report=mig.weekly_report(d),
                tracking=__import__("pln_agent.migration_tracking", fromlist=["track"]).track(d, persist=False))


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
def audit(request: Request, actor: str = "", q: str = ""):
    where, params = ["1=1"], []
    if actor:
        where.append("actor=?"), params.append(actor)
    if q:
        where.append("(action LIKE ? OR entity_id LIKE ?)"), params.extend([f"%{q}%", f"%{q}%"])
    d = db()
    actors = [r["actor"] for r in d.query("SELECT actor, COUNT(*) AS n FROM audit_log GROUP BY actor ORDER BY n DESC")][:30]
    return page(request, "audit.html", rows=d.all("audit_log", " AND ".join(where), params, order="id DESC")[:300],
                actors=actors, actor=actor, q=q)


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
