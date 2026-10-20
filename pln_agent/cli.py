"""`python -m pln_agent <command>` — operator CLI. Everything defaults to dry-run."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import accounts as acc_mod
from . import migration as mig
from .adapters import ADAPTERS, get_adapter
from .calendar_planner import plan_week, render_calendar_text
from .config import ROOT, load_settings
from .db import DB, render_ddl
from .editorial import generate_pack
from .experiments import plan_experiments, recommendations
from .ingest import scan_inbox, watch
from .media import render_variant
from .metrics import baseline, fmt, import_csv
from .reports import platform_report_text, portfolio_report
from .workflow import (WorkflowError, approve, edit_variant, poll_statuses, reject, run_publisher, schedule,
                       set_kill_switch)


def _db(args) -> DB:
    s = load_settings()
    return DB(args.db or s.database_url)


def _p(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(args):
    db = _db(args)
    r = acc_mod.load_accounts(db, actor=args.actor)
    mig.sync_decisions(db)
    print(f"Loaded {r.loaded}/{r.supplied_rows} user-supplied accounts (+{r.placeholders} pending LinkedIn placeholder). "
          f"Warnings: {r.warnings or 'none'}. Nothing is verified.")


def cmd_accounts(args):
    db = _db(args)
    if args.action == "list":
        for a in acc_mod.list_accounts(db):
            print(f"{a['account_id']:<52} {a['username_original'] or '(pending)':<28} {a['role']:<22} "
                  f"{a['verification_status']:<18} {'BLOCK: ' + a['hard_block_reason'][:50] if a['hard_block_reason'] else ''}")
    elif args.action == "verify":
        acc_mod.set_verification(db, args.account_id, "ownership_confirmed", args.actor, owner=args.owner or "", note=args.note or "")
        print("ownership confirmed (audited). Publishing still requires OAuth + authorization.")
    elif args.action == "clear-block":
        acc_mod.clear_hard_block(db, args.account_id, args.actor, args.note or "")
        print("hard block cleared (audited)")
    elif args.action == "connect":
        acc_mod.connect_credentials(db, args.account_id, args.credential_ref, args.platform_id, args.actor)
        print("credential reference recorded (no secret stored)")
    elif args.action == "authorize":
        acc_mod.authorize_publishing(db, args.account_id, args.actor, enabled=not args.revoke)
        print("publishing " + ("revoked" if args.revoke else "authorized") + " for account (global gates still apply)")


def cmd_ingest(args):
    db, s = _db(args), load_settings()
    inbox = Path(args.inbox) if args.inbox else s.media_inbox
    if args.watch:
        watch(db, inbox, interval_s=args.interval)
        return
    for a in scan_inbox(db, inbox, scenes=args.scenes):
        print(a.get("asset_id", a.get("sidecar")), "media_present=" + str(a.get("media_present")), a.get("error", ""))


def cmd_plan(args):
    db, s = _db(args), load_settings()
    ids = [a["asset_id"] for a in db.all("assets")] if args.all else [args.asset]
    for aid in ids:
        p = generate_pack(db, aid, s, include_extended=not args.core_only)
        print(f"{aid}: {len(p['variants'])} variants; rights_ok={p['rights']['ok']}")
        for n in p["notes"]:
            print("   note:", n)


def cmd_calendar(args):
    db, s = _db(args), load_settings()
    start = date.fromisoformat(args.start) if args.start else date.today() + timedelta(days=1)
    plan = plan_week(db, s, start)
    print(render_calendar_text(plan))


def cmd_queue(args):
    db = _db(args)
    where, params = "state IN ('in_review','needs_fix','approved','scheduled')", []
    if args.platform:
        where += " AND platform=?"
        params.append(args.platform)
    for v in db.all("variants", where, params, order="platform, state, score DESC"):
        flags = (v["risk_flags"] or {})
        badges = ",".join(flags.get("review_flags", []) + (["RIGHTS"] if flags.get("blockers") else []))
        print(f"{v['platform']:<9} {v['state']:<9} {v['score'] or 0:.3f} {v['variant_id']:<70} {badges}")


def cmd_show(args):
    v = _db(args).get("variants", "variant_id", args.variant_id)
    _p(v)


def cmd_edit(args):
    ch = {k: getattr(args, k) for k in ("caption", "hook", "title", "cta") if getattr(args, k)}
    _p(edit_variant(_db(args), args.variant_id, ch, args.actor)["state"])


def cmd_approve(args):
    try:
        _p(approve(_db(args), args.variant_id, args.reviewer, args.note or ""))
    except WorkflowError as e:
        print("REFUSED:", e)
        sys.exit(2)


def cmd_reject(args):
    reject(_db(args), args.variant_id, args.reviewer, args.note or "")
    print("rejected")


def cmd_schedule(args):
    db, s = _db(args), load_settings()
    if args.at:
        at = datetime.fromisoformat(args.at)
        at = at if at.tzinfo else at.replace(tzinfo=ZoneInfo(s.timezone))
    else:
        row = db.one("SELECT slot_at FROM schedules WHERE variant_id=? AND state='planned'", [args.variant_id])
        if not row:
            print("no planned slot; pass --at")
            sys.exit(2)
        at = datetime.fromisoformat(row["slot_at"])
    try:
        _p(schedule(db, args.variant_id, at, args.actor))
    except WorkflowError as e:
        print("REFUSED:", e)
        sys.exit(2)


def cmd_publish(args):
    db, s = _db(args), load_settings()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    _p(run_publisher(db, s, now=now))
    _p(poll_statuses(db, s))


def cmd_kill(args):
    set_kill_switch(_db(args), args.state == "on", args.actor, args.note or "")
    print("emergency stop", args.state.upper())


def cmd_render(args):
    db, s = _db(args), load_settings()
    ids = [v["variant_id"] for v in db.all("variants", "state NOT IN ('rejected','cancelled')")] if args.all else [args.variant_id]
    for vid in ids:
        r = render_variant(db, vid, s)
        print(f"{r['status']:<14} {vid}  {r['path']}")


def cmd_metrics(args):
    db = _db(args)
    if args.action == "import":
        _p(import_csv(db, Path(args.csv), args.actor))
    else:
        for a in db.all("accounts", order="canonical_brand_id"):
            b = baseline(db, a["account_id"])
            print(f"{a['account_id']:<52} followers={fmt(b['followers']):<9} posts/wk={fmt(b['posts_per_week']):<8} "
                  f"reach/post={fmt(b['median_reach_per_post']):<9} watch_s={fmt(b['median_watch_time_s']):<8} "
                  f"shares={fmt(b['median_shares_per_post']):<8} saves={fmt(b['median_saves_per_post']):<8} "
                  f"profile→follow={fmt(b['profile_to_follow_conversion'])}")


def cmd_migration(args):
    db = _db(args)
    mig.sync_decisions(db)
    if args.action == "sheet":
        for sh in mig.decision_sheet(db):
            print(f"\n[{sh['platform'].upper()}] legacy {sh['legacy_handle']} | Putt Tour {sh['putt_tour_handle']}")
            print(f"  configured: {sh['configured_option']} ({sh['status']})  recommended: {sh['recommended_option']} [{sh['confidence']}]")
            print(f"  why: {sh['rationale']}")
            print(f"  missing evidence: {sh['missing_evidence'] or 'none'}")
            print("  follower transfer assumed: NO · auto-rename: NO · auto-delete: NO")
    elif args.action == "plan":
        for t in mig.ninety_day_plan():
            if t["phase"] != "report" or args.verbose:
                print(f"D{t['day']:>3} {t['date']} {t['phase']:<12} {t['task']}" + (f"  [{t['gate']}]" if t["gate"] else ""))
    elif args.action == "propose":
        _p(mig.propose_profile_changes(db, args.actor))
    elif args.action == "proposals":
        for p in db.all("profile_change_proposals", order="account_id, field"):
            print(f"#{p['id']:<3} {p['status']:<26} {p['account_id']:<52} {p['field']:<12} → {p['proposed_value']}")
    elif args.action == "approve":
        mig.approve_decision(db, args.platform, args.actor, args.note or "")
        print(f"{args.platform} decision approved (profile proposals may now be approved for MANUAL execution)")
    elif args.action == "review":
        mig.review_proposal(db, int(args.proposal_id), args.decision, args.actor)
        print("proposal updated")
    elif args.action == "announcements":
        for p in ("instagram", "tiktok", "youtube", "x"):
            for a in mig.announcement_variants(p):
                print(f"{p:<10} {a['label']:<26} {a['format']:<40} {a['copy'][:70]}…")
    elif args.action == "report":
        _p(mig.weekly_report(db))


def cmd_experiments(args):
    db = _db(args)
    start = date.fromisoformat(args.week) if args.week else date.today()
    _p(plan_experiments(db, start))


def cmd_recommend(args):
    for r in recommendations(_db(args)):
        print(f"[{r['priority']}] {r['type']:<20} {r['text']}  (evidence: {r['evidence']})")


def cmd_report(args):
    db, s = _db(args), load_settings()
    if args.json:
        _p(portfolio_report(db, s))
    else:
        print(platform_report_text(db, s))


def cmd_capabilities(args):
    for p in ADAPTERS:
        c = get_adapter(p).capabilities()
        print(f"\n== {p}  video_publish={c['video_publish']}  profile_edits={c['profile_edits']}  delete={c['delete']}")
        for r in c["requirements"]:
            print("   needs:", r)
        for b in c["app_review_blockers"]:
            print("   BLOCKER:", b)


def cmd_schema(args):
    print(render_ddl(args.dialect))


def cmd_demo(args):
    from .demo import run_demo
    run_demo(Path(args.out) if args.out else ROOT / "data" / "demo_output")


def cmd_ops(args):
    from .ops_sources import import_ops_accounts, import_ops_clips
    s = load_settings()
    if not s.ops_database_url:
        print("Set PLN_OPS_DATABASE_URL (e.g. rdsdata://plnops?cluster=<arn>&secret=<arn>)")
        sys.exit(2)
    db, ops = _db(args), DB(s.ops_database_url, init=False)
    if args.action in ("accounts", "all"):
        _p(import_ops_accounts(db, ops, args.actor))
    if args.action in ("clips", "all"):
        r = import_ops_clips(db, ops, s.ops_assets_bucket, limit=args.limit, min_score=args.min_score,
                             properties=args.property or None, actor=args.actor)
        print(f"imported {r['imported']} clips; skipped {len(r['skipped'])}")
        for x in r["skipped"][:20]:
            print("   ", x)


def cmd_rights(args):
    from .ops_sources import set_asset_rights
    db = _db(args)
    ids = [a["asset_id"] for a in db.all("assets", "brand_id=?", [args.brand])] if args.brand else [args.asset_id]
    fields = {"license": args.license, "owner": args.owner or "", "territory": [t.strip().upper() for t in args.territory.split(",")],
              "rights_expires_at": args.expires, "rights_verified": True, "music_cleared": args.music_cleared,
              "note": args.note or ""}
    if args.asset_id and args.people_verified:
        fields["people_verified"] = True  # never in bulk: identities are confirmed clip by clip
    for aid in ids:
        set_asset_rights(db, aid, args.actor, **fields)
    print(f"rights recorded for {len(ids)} asset(s) by {args.actor}")


def cmd_worker(args):
    from . import worker
    s = load_settings()
    worker.run(_db(args), s, interval_s=args.interval, ops_every_s=args.ops_every)


def cmd_serve(args):
    import os
    import uvicorn
    port = int(os.environ.get("PORT", args.port))
    uvicorn.run("pln_agent.web.app:app", host=args.host, port=port, reload=False,
                proxy_headers=True, forwarded_allow_ips="*")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pln", description="PLN Social Growth Agent (dry-run by default)")
    ap.add_argument("--db", help="database URL (default PLN_DATABASE_URL or sqlite:///data/pln_agent.db)")
    ap.add_argument("--actor", default="operator", help="name recorded in the audit log")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("init").set_defaults(fn=cmd_init)
    a = sp.add_parser("accounts")
    a.add_argument("action", choices=["list", "verify", "clear-block", "connect", "authorize"])
    a.add_argument("account_id", nargs="?")
    a.add_argument("--owner"), a.add_argument("--note"), a.add_argument("--credential-ref", dest="credential_ref")
    a.add_argument("--platform-id", dest="platform_id", default=""), a.add_argument("--revoke", action="store_true")
    a.set_defaults(fn=cmd_accounts)
    i = sp.add_parser("ingest")
    i.add_argument("--inbox"), i.add_argument("--scenes", action="store_true"), i.add_argument("--watch", action="store_true")
    i.add_argument("--interval", type=float, default=10.0)
    i.set_defaults(fn=cmd_ingest)
    p = sp.add_parser("plan")
    p.add_argument("--asset"), p.add_argument("--all", action="store_true"), p.add_argument("--core-only", action="store_true")
    p.set_defaults(fn=cmd_plan)
    c = sp.add_parser("calendar")
    c.add_argument("--start")
    c.set_defaults(fn=cmd_calendar)
    q = sp.add_parser("queue")
    q.add_argument("--platform")
    q.set_defaults(fn=cmd_queue)
    s = sp.add_parser("show")
    s.add_argument("variant_id")
    s.set_defaults(fn=cmd_show)
    e = sp.add_parser("edit")
    e.add_argument("variant_id"), e.add_argument("--caption"), e.add_argument("--hook"), e.add_argument("--title"), e.add_argument("--cta")
    e.set_defaults(fn=cmd_edit)
    ap_ = sp.add_parser("approve")
    ap_.add_argument("variant_id"), ap_.add_argument("--reviewer", required=True), ap_.add_argument("--note")
    ap_.set_defaults(fn=cmd_approve)
    rj = sp.add_parser("reject")
    rj.add_argument("variant_id"), rj.add_argument("--reviewer", required=True), rj.add_argument("--note")
    rj.set_defaults(fn=cmd_reject)
    sc = sp.add_parser("schedule")
    sc.add_argument("variant_id"), sc.add_argument("--at")
    sc.set_defaults(fn=cmd_schedule)
    pb = sp.add_parser("publish")
    pb.add_argument("--now")
    pb.set_defaults(fn=cmd_publish)
    k = sp.add_parser("kill-switch")
    k.add_argument("state", choices=["on", "off"]), k.add_argument("--note")
    k.set_defaults(fn=cmd_kill)
    r = sp.add_parser("render")
    r.add_argument("variant_id", nargs="?"), r.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_render)
    m = sp.add_parser("metrics")
    m.add_argument("action", choices=["import", "baselines"]), m.add_argument("csv", nargs="?")
    m.set_defaults(fn=cmd_metrics)
    mg = sp.add_parser("migration")
    mg.add_argument("action", choices=["sheet", "plan", "propose", "proposals", "approve", "review", "announcements", "report"])
    mg.add_argument("platform", nargs="?"), mg.add_argument("--proposal-id", dest="proposal_id")
    mg.add_argument("--decision"), mg.add_argument("--note"), mg.add_argument("--verbose", action="store_true")
    mg.set_defaults(fn=cmd_migration)
    ex = sp.add_parser("experiments")
    ex.add_argument("--week")
    ex.set_defaults(fn=cmd_experiments)
    sp.add_parser("recommend").set_defaults(fn=cmd_recommend)
    rp = sp.add_parser("report")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_report)
    sp.add_parser("capabilities").set_defaults(fn=cmd_capabilities)
    sch = sp.add_parser("schema")
    sch.add_argument("--dialect", choices=["sqlite", "postgres"], default="postgres")
    sch.set_defaults(fn=cmd_schema)
    d = sp.add_parser("demo")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_demo)
    o = sp.add_parser("ops", help="read-only import from the ops portal (accounts registry + clip library)")
    o.add_argument("action", choices=["accounts", "clips", "all"])
    o.add_argument("--limit", type=int, default=100), o.add_argument("--min-score", dest="min_score", type=int, default=60)
    o.add_argument("--property", action="append", help="clips.property value, e.g. CarJitsu (repeatable)")
    o.set_defaults(fn=cmd_ops)
    rg = sp.add_parser("rights", help="record a human rights confirmation for one asset or a whole brand")
    rg.add_argument("asset_id", nargs="?"), rg.add_argument("--brand")
    rg.add_argument("--license", required=True), rg.add_argument("--owner"), rg.add_argument("--territory", required=True)
    rg.add_argument("--expires"), rg.add_argument("--music-cleared", dest="music_cleared", action="store_true")
    rg.add_argument("--people-verified", dest="people_verified", action="store_true"), rg.add_argument("--note")
    rg.set_defaults(fn=cmd_rights)
    w = sp.add_parser("worker", help="publisher loop + periodic ops refresh")
    w.add_argument("--interval", type=int, default=60), w.add_argument("--ops-every", dest="ops_every", type=int, default=6 * 3600)
    w.set_defaults(fn=cmd_worker)
    sv = sp.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1"), sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
