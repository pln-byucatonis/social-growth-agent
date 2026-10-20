"""Human approval dashboard (FastAPI + server-rendered Jinja; HTMX optional enhancement).

Local-first: binds 127.0.0.1 by default. Set PLN_DASHBOARD_PASSWORD to require HTTP Basic auth.
Every action goes through the same workflow functions as the CLI, so gates and audit are identical.
"""
from __future__ import annotations

import base64
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import migration as mig
from ..accounts import publish_eligibility
from ..adapters import get_adapter
from ..calendar_planner import plan_week
from ..config import load_settings
from ..db import DB
from ..experiments import recommendations
from ..media import render_variant
from ..metrics import NORTH_STAR
from ..workflow import (WorkflowError, approve, cancel, edit_variant, kill_switch_on, reject, run_publisher, schedule,
                        set_kill_switch)

HERE = Path(__file__).parent
settings = load_settings()
app = FastAPI(title="PLN Social Growth Agent", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(HERE / "templates"))
settings.render_dir.mkdir(parents=True, exist_ok=True)
app.mount("/renders", StaticFiles(directory=str(settings.render_dir)), name="renders")
_db: DB | None = None


def db() -> DB:
    global _db
    if _db is None:
        _db = DB(settings.database_url)
    return _db


@app.middleware("http")
async def basic_auth(request: Request, call_next):
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


def ctx(request: Request, **kw) -> dict:
    return {"request": request, "dry_run": settings.dry_run, "publishing_enabled": settings.publishing_enabled,
            "kill": kill_switch_on(db()), **kw}


def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(url + ("?" + q if q else ""), status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    d = db()
    counts = {r["state"]: r["n"] for r in d.query("SELECT state, COUNT(*) AS n FROM variants GROUP BY state")}
    jobs = d.all("publish_jobs", order="updated_at DESC")[:15]
    return templates.TemplateResponse(request, "home.html", ctx(request, counts=counts, jobs=jobs, recs=recommendations(d)[:25],
                                                       msg=request.query_params.get("msg"), err=request.query_params.get("err")))


@app.post("/kill")
def kill(state: str = Form(...), actor: str = Form(...), reason: str = Form("")):
    set_kill_switch(db(), state == "on", actor or "dashboard", reason)
    return back("/", f"Emergency stop {state.upper()}")


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, platform: str = "", state: str = ""):
    d = db()
    where, params = ["1=1"], []
    if platform:
        where.append("platform=?"), params.append(platform)
    if state:
        where.append("state=?"), params.append(state)
    vs = d.all("variants", " AND ".join(where), params, order="platform, state, score DESC")
    accs = {a["account_id"]: a for a in d.all("accounts")}
    perm = {k: publish_eligibility(a, settings, kill_switch_on(d)) for k, a in accs.items()}
    return templates.TemplateResponse(request, "queue.html", ctx(request, variants=vs, accs=accs, perm=perm, platform=platform, state=state,
                                                        platforms=sorted({v["platform"] for v in d.all("variants")}),
                                                        msg=request.query_params.get("msg"), err=request.query_params.get("err")))


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
    if v.get("render_path"):
        rp = Path(v["render_path"])
        try:
            render_url = "/renders/" + str(rp.relative_to(settings.render_dir))
        except ValueError:
            render_url = ""
    preview = get_adapter(acc["platform"], settings=settings).simulate(v, acc, "preview")
    return templates.TemplateResponse(request, "variant.html", ctx(request, v=v, acc=acc, asset=asset, live_ok=ok, reasons=reasons,
                                                          approvals=approvals, audit=audit, jobs=jobs, planned=planned,
                                                          render_url=render_url, would_send=preview.raw["would_send"],
                                                          north=NORTH_STAR.get(v["platform"], {}),
                                                          msg=request.query_params.get("msg"), err=request.query_params.get("err")))


@app.post("/variant/{vid:path}/edit")
def do_edit(vid: str, actor: str = Form(...), hook: str = Form(...), caption: str = Form(...), cta: str = Form(...)):
    try:
        v = edit_variant(db(), vid, {"hook": hook, "caption": caption, "cta": cta}, actor)
        return back(f"/variant/{vid}", f"Saved. State: {v['state']} (any prior approval was invalidated)")
    except WorkflowError as e:
        return back(f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/approve")
def do_approve(vid: str, reviewer: str = Form(...), note: str = Form("")):
    try:
        r = approve(db(), vid, reviewer, note)
        return back(f"/variant/{vid}", f"Recorded approval by {reviewer}: state={r['state']} ({len(r['approvers'])}/{r['needed']})")
    except WorkflowError as e:
        return back(f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/reject")
def do_reject(vid: str, reviewer: str = Form(...), note: str = Form(...)):
    try:
        reject(db(), vid, reviewer, note)
        return back(f"/variant/{vid}", "Rejected")
    except WorkflowError as e:
        return back(f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/schedule")
def do_schedule(vid: str, actor: str = Form(...), slot_at: str = Form(...)):
    try:
        at = datetime.fromisoformat(slot_at)
        at = at if at.tzinfo else at.replace(tzinfo=ZoneInfo(settings.timezone))
        j = schedule(db(), vid, at, actor)
        return back(f"/variant/{vid}", f"Scheduled job {j['job_id']}")
    except (WorkflowError, ValueError) as e:
        return back(f"/variant/{vid}", err=str(e))


@app.post("/variant/{vid:path}/cancel")
def do_cancel(vid: str, actor: str = Form(...), reason: str = Form("cancelled from dashboard")):
    cancel(db(), vid, actor, reason)
    return back(f"/variant/{vid}", "Cancelled")


@app.post("/variant/{vid:path}/render")
def do_render(vid: str):
    r = render_variant(db(), vid, settings, actor="dashboard")
    return back(f"/variant/{vid}", f"Render: {r['status']}")


@app.post("/publish/run")
def do_publish():
    res = run_publisher(db(), settings)
    return back("/", f"Publisher ran: {res or 'nothing due'}")


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, start: str = ""):
    s = date.fromisoformat(start) if start else date.today() + timedelta(days=1)
    plan = plan_week(db(), settings, s, states=("in_review", "approved", "scheduled"), persist=False)
    days: dict[str, list] = {}
    for p in plan["placed"]:
        days.setdefault(p["slot"].strftime("%a %b %d"), []).append(p)
    return templates.TemplateResponse(request, "calendar.html", ctx(request, plan=plan, days=days, start=s.isoformat()))


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    d = db()
    rows = []
    for a in d.all("accounts", order="canonical_brand_id, platform"):
        ok, reasons = publish_eligibility(a, settings, kill_switch_on(d))
        rows.append({**a, "live_ok": ok, "reasons": reasons})
    caps = {p: get_adapter(p, settings=settings).capabilities() for p in sorted({a["platform"] for a in rows})}
    return templates.TemplateResponse(request, "accounts.html", ctx(request, rows=rows, caps=caps))


@app.get("/migration", response_class=HTMLResponse)
def migration(request: Request):
    d = db()
    mig.sync_decisions(d)
    return templates.TemplateResponse(request, "migration.html", ctx(
        request, sheets=mig.decision_sheet(d), proposals=d.all("profile_change_proposals", order="account_id, field"),
        plan=[t for t in mig.ninety_day_plan() if t["phase"] != "report"], ann=mig.announcement_variants("instagram"),
        report=mig.weekly_report(d), msg=request.query_params.get("msg"), err=request.query_params.get("err")))


@app.post("/migration/approve")
def migration_approve(platform: str = Form(...), actor: str = Form(...)):
    mig.approve_decision(db(), platform, actor)
    return back("/migration", f"{platform} decision approved")


@app.post("/migration/propose")
def migration_propose(actor: str = Form("dashboard")):
    n = len(mig.propose_profile_changes(db(), actor))
    return back("/migration", f"{n} proposals created")


@app.post("/migration/proposal/{pid}")
def migration_review(pid: int, decision: str = Form(...), actor: str = Form(...)):
    try:
        mig.review_proposal(db(), pid, decision, actor)
        return back("/migration", f"Proposal #{pid} → {decision}")
    except PermissionError as e:
        return back("/migration", err=str(e))


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    return templates.TemplateResponse(request, "audit.html", ctx(request, rows=db().all("audit_log", order="id DESC")[:300]))


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run, "publishing_enabled": settings.publishing_enabled,
            "kill_switch": kill_switch_on(db()), "time": datetime.now(timezone.utc).isoformat()}
