"""End-to-end local simulation on SAMPLE data. No tokens, no network, no paid models.

Writes a fresh demo database + human-readable evidence files to data/demo_output/.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import accounts as acc_mod
from . import migration as mig
from .adapters import HttpResponse
from .calendar_planner import plan_week, render_calendar_text
from .config import ROOT, load_settings
from .db import DB
from .editorial import generate_pack
from .experiments import plan_experiments, recommendations
from .ingest import scan_inbox
from .media import render_variant
from .reports import platform_report_text
from .strategists import similarity
from .workflow import WorkflowError, approve, run_publisher, schedule, set_kill_switch

DEMO_TODAY = date(2026, 10, 20)
CALENDAR_START = date(2026, 10, 21)
SHOWCASE_ASSET = "carjitsu-sample-001"


class Out:
    def __init__(self, path: Path):
        self.path, self.lines = path, []

    def h(self, title: str):
        self.p("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)

    def p(self, *a):
        s = " ".join(str(x) for x in a)
        print(s)
        self.lines.append(s)

    def save(self):
        self.path.write_text("\n".join(self.lines) + "\n")


def _setup(db_url: str, settings):
    db = DB(db_url)
    inv = acc_mod.load_accounts(db, actor="demo")
    mig.sync_decisions(db)
    assets = scan_inbox(db, settings.media_inbox)
    return db, inv, assets


def run_demo(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    settings = replace(settings, dry_run=True, publishing_enabled=False, render_dir=out_dir / "renders",
                       database_url=f"sqlite:///{out_dir / 'demo.db'}")
    dbfile = out_dir / "demo.db"
    for f in (dbfile, Path(str(dbfile) + "-wal"), Path(str(dbfile) + "-shm")):
        f.unlink(missing_ok=True)
    if (out_dir / "renders").exists():
        shutil.rmtree(out_dir / "renders")
    o = Out(out_dir / "demo_console.txt")
    db, inv, assets = _setup(settings.database_url, settings)

    o.h("1) ACCOUNT INVENTORY — user-supplied handles, loaded verbatim, NOTHING verified")
    o.p(f"CSV rows: {inv.supplied_rows} | loaded: {inv.loaded} | dropped: {inv.supplied_rows - inv.loaded} | "
        f"system placeholder: pln.linkedin.pending (no handle fabricated)")
    for a in acc_mod.list_accounts(db):
        o.p(f"  {a['account_id']:<52} {a['username_original'] or '(not supplied)':<27} {a['role']:<21} "
            f"{a['verification_status']:<11} {('⛔ ' + a['hard_block_reason'][:55]) if a['hard_block_reason'] else ''}")

    o.h("2) INGESTION — licensed-asset inbox + sidecars + ffprobe (SAMPLE synthetic clips)")
    for a in assets:
        o.p(f"  {a['asset_id']:<22} brand={a['brand_id']:<10} source_identity={a['source_brand_name']!r:<26} "
            f"media={'yes' if a['media_present'] else 'MISSING'} dur={a['duration_s']} {a['width']}x{a['height']}")

    o.h("3) EDITORIAL PACKS — routing: canonical brand first, then selective PLN amplification / WPL bridge")
    packs = {}
    for a in db.all("assets", order="asset_id"):
        p = generate_pack(db, a["asset_id"], settings, today=DEMO_TODAY)
        packs[a["asset_id"]] = p
        o.p(f"\n  {a['asset_id']}: {len(p['variants'])} variants · rights_ok={p['rights']['ok']} "
            f"blockers={p['rights']['blockers']} review_flags={p['rights']['review_flags']}")
        for v in p["variants"]:
            o.p(f"    {v['state']:<9} {v['account_id']:<50} [{v['route_role']}] score={v['score']}")
        for n in p["notes"]:
            o.p(f"    note: {n}")

    o.h(f"4) ONE ASSET → FOUR GENUINELY DIFFERENT CORE-PLATFORM SCRIPTS ({SHOWCASE_ASSET})")
    core = ["carjitsu.tiktok.carjitsuchampionship", "carjitsu.instagram.carjitsu_championship",
            "pln.facebook.pro_league_network", "pln.linkedin.pending"]
    showcase = [db.get("variants", "variant_id", f"{SHOWCASE_ASSET}:{acc}") for acc in core]
    for v in showcase:
        o.p(f"\n--- {v['platform'].upper()} → {v['account_id']} [{v['route_role']}] series='{v['series']}' format={v['format']}")
        o.p(f"HOOK: {v['hook']}")
        o.p("CAPTION:\n" + "\n".join("  " + ln for ln in v["caption"].splitlines()))
        o.p("EDL: " + " | ".join(f"cut {s['source_in']:.2f}s–{s['source_out']:.2f}s {s['purpose']} (annotated {s['source_timecode']})"
                         for s in v["edl"]["segments"]))
        o.p(f"OUTPUT: {v['edl']['output']} · ON-SCREEN: {[t['text'] for t in v['onscreen_text']]}")
        o.p(f"HASHTAGS: {v['hashtags']} — {v['hashtag_rationale']}")
        o.p(f"CTA: {v['cta']} · ANGLE: {v['angle']}")
        o.p(f"TEST: {v['test_hypothesis']}")
        o.p(f"ALT HOOKS: {[h['text'] for h in v['hook_candidates'][1:]]}")
        o.p(f"SCORE {v['score']}: " + "; ".join(f"{k}={x['score']} ({x['why'][:60]})" for k, x in v["score_breakdown"]["parts"].items()))
    o.p("\nCaption similarity matrix (trigram Jaccard; gate rejects > 0.45):")
    for v in showcase:
        o.p("  " + v["platform"][:9].ljust(10) + " ".join(f"{similarity(v['caption'], w['caption']):.2f}" for w in showcase))

    o.h("5) NATIVE MEDIA PACKAGING (FFmpeg) — real renders from SAMPLE clips; EDL-only when media missing")
    render_ids = [v["variant_id"] for v in showcase] + [
        "putt-sample-002:putt_tour.instagram.putttour", "putt-sample-002:world_putting_league.instagram.worldputtingleague",
        "slapfight-sample-004:slapfight.tiktok.slapfightchampionshipusa"]
    for vid in render_ids:
        r = render_variant(db, vid, settings)
        o.p(f"  {r['status']:<14} {vid:<70} {Path(r['path']).relative_to(out_dir) if r['path'] else ''}")

    o.h("6) SEVEN-DAY CALENDAR (coordinated across accounts; empty slots = supply gap, not filler)")
    plan = plan_week(db, settings, CALENDAR_START)
    o.p(render_calendar_text(plan))

    o.h("7) APPROVAL GATES, RIGHTS GATE, DUPLICATE PREVENTION, EMERGENCY STOP, SAFE PUBLISH GATE")
    tries = [("wpl-archive-003:putt_tour.instagram.putttour", "demo.editor", "expired-rights asset"),
             ("slapfight-sample-004:slapfight.tiktok.slapfightchampionshipusa", "demo.editor", "unlicensed / unverified people"),
             (f"{SHOWCASE_ASSET}:carjitsu.tiktok.carjitsuchampionship", "demo.editor", "clean asset"),
             ("putt-sample-002:putt_tour.instagram.putttour", "demo.editor", "paid partnership (1st reviewer)"),
             ("putt-sample-002:putt_tour.instagram.putttour", "demo.editor", "same reviewer again (doesn't count twice)"),
             ("putt-sample-002:putt_tour.instagram.putttour", "demo.brand_safety", "paid partnership (2nd reviewer)"),
             (f"{SHOWCASE_ASSET}:pln.linkedin.pending", "demo.editor", "PLN LinkedIn (pending, unverified page)"),
             ("putt-sample-002:world_putting_league.instagram.worldputtingleague", "demo.editor", "WPL→Putt Tour bridge teaser (1st)"),
             ("putt-sample-002:world_putting_league.instagram.worldputtingleague", "demo.brand_safety", "WPL bridge (2nd reviewer)")]
    for vid, who, label in tries:
        try:
            r = approve(db, vid, who)
            o.p(f"  APPROVE {label:<44} → state={r['state']} approvers={r['approvers']} needed={r['needed']}")
        except WorkflowError as e:
            o.p(f"  APPROVE {label:<44} → REFUSED: {e}")
    slot = datetime(2026, 10, 21, 16, 15, tzinfo=timezone.utc)
    o.p("\n  Approval queue by platform:")
    for row in db.query("SELECT platform, state, COUNT(*) AS n FROM variants GROUP BY platform, state ORDER BY platform, state"):
        o.p(f"    {row['platform']:<10} {row['state']:<10} {row['n']}")
    for vid in [f"{SHOWCASE_ASSET}:carjitsu.tiktok.carjitsuchampionship", "putt-sample-002:putt_tour.instagram.putttour", f"{SHOWCASE_ASSET}:pln.linkedin.pending",
                f"{SHOWCASE_ASSET}:carjitsu.instagram.carjitsu_championship"]:
        try:
            j = schedule(db, vid, slot, "demo.scheduler")
            o.p(f"  SCHEDULE {vid:<66} → job {j['job_id']} key={j['idempotency_key'][:12]}…")
        except WorkflowError as e:
            o.p(f"  SCHEDULE {vid:<66} → REFUSED: {e}")
    # stale UI double-submit: variant flipped back to approved while a job already exists
    dupe = f"{SHOWCASE_ASSET}:carjitsu.tiktok.carjitsuchampionship"
    db.update("variants", "variant_id", dupe, {"state": "approved"})
    try:
        schedule(db, dupe, slot + timedelta(hours=3), "demo.scheduler")
    except WorkflowError as e:
        o.p(f"  SCHEDULE (double-submit of same post)                            → REFUSED: {e}")
    db.update("variants", "variant_id", dupe, {"state": "scheduled"})
    set_kill_switch(db, True, "demo.ops", "demo: incident drill")
    o.p(f"  EMERGENCY STOP ON → publisher: {run_publisher(db, settings, now=slot + timedelta(minutes=1))}")
    set_kill_switch(db, False, "demo.ops", "drill over")
    for j in db.all("publish_jobs", "state='halted'"):
        db.update("publish_jobs", "job_id", j["job_id"], {"state": "queued"})
    res = run_publisher(db, settings, now=slot + timedelta(minutes=2))
    for r in res:
        o.p(f"  PUBLISH (dry-run) {r['job_id']} → {r['state']}; not live because: {r.get('why_not_live')}")
    receipts = []
    for j in db.all("publish_jobs"):
        receipts.append({"job_id": j["job_id"], "variant_id": j["variant_id"], "state": j["state"], "mode": j["mode"],
                         "permalink": j["permalink"], "would_send": (j["receipt"] or {}).get("would_send")})
    (out_dir / "simulated_receipts.json").write_text(json.dumps(receipts, indent=2))
    o.p("  → simulated receipts incl. exact (token-redacted) API requests: simulated_receipts.json")

    o.h("8) LIVE-PATH REHEARSAL WITH A MOCK TRANSPORT (no network; proves retry/backoff/idempotency/error handling)")
    o.p(_live_rehearsal(settings))

    o.h("9) WORLD PUTTING LEAGUE → PUTT TOUR MIGRATION")
    for sh in mig.decision_sheet(db):
        o.p(f"  [{sh['platform']:<9}] legacy {sh['legacy_handle']:<22} putt tour {sh['putt_tour_handle']:<32} "
            f"configured={sh['configured_option']:<24} recommended={sh['recommended_option']} ({sh['confidence']})")
        o.p(f"      {sh['rationale']}")
    props = mig.propose_profile_changes(db, "demo")
    o.p(f"\n  {len(props)} profile-change PROPOSALS created (status=proposed; never auto-applied). e.g.:")
    for p in props[:5]:
        o.p(f"    {p['account_id']:<52} {p['field']:<12} → {p['proposed_value']}")
    try:
        first = db.one("SELECT id FROM profile_change_proposals LIMIT 1")["id"]
        mig.review_proposal(db, first, "approved_for_manual_change", "demo.editor")
    except PermissionError as e:
        o.p(f"  Approving a proposal before the platform decision is signed off → REFUSED: {e}")
    o.p("\n  90-day plan (approval checkpoints marked):")
    for t in mig.ninety_day_plan():
        if t["phase"] != "report":
            o.p(f"    D{t['day']:>3} {t['date']} {t['phase']:<12} {t['task']}" + (f"  [{t['gate']}]" if t["gate"] else ""))
    o.p("\n  Announcement variants (per platform, native edits):")
    for a in mig.announcement_variants("instagram"):
        o.p(f"    {a['label']:<26} “{a['copy']}”  edit: {a['edit']}")
    o.p("\n  UTM example: ?" + mig.utm_params("instagram", "world_putting_league.instagram.worldputtingleague", "announcement_founder"))
    o.p("  Weekly migration report (proxies; unknown until analytics imported):")
    for r in mig.weekly_report(db)["platforms"]:
        o.p(f"    {r['platform']:<9} legacy_followers={r['legacy_followers']} canonical_followers={r['canonical_followers']} "
            f"link_clicks={r['migration_link_clicks_from_legacy']}")

    o.h("10) WEEKLY EXPERIMENTS + EXPLAINABLE RECOMMENDATIONS")
    for e in plan_experiments(db, CALENDAR_START):
        if e.get("status") == "not_run":
            o.p(f"  {e['platform']:<9} not run: {e['reason']}")
        else:
            o.p(f"  {e['platform']:<9} {e['variable']:<10} control='{e['control'][:45]}' vs treatment='{e['treatment'][:45]}'")
    for r in recommendations(db, now=datetime(2026, 10, 22, tzinfo=timezone.utc)):
        o.p(f"  [{r['priority']}] {r['type']:<20} {r['text'][:150]}")

    o.h("11) PLATFORM REPORT")
    o.p(platform_report_text(db, settings))
    o.save()
    (out_dir / "calendar.json").write_text(json.dumps(plan, indent=2, default=str))
    (out_dir / "creative_package.json").write_text(json.dumps(showcase, indent=2, default=str))
    (out_dir / "migration_decision_sheet.json").write_text(json.dumps(mig.decision_sheet(db), indent=2, default=str))
    print(f"\nEvidence written to {out_dir}")
    return {"db": str(dbfile), "out": str(out_dir)}


def _live_rehearsal(settings) -> str:
    lines = []
    s = replace(settings, dry_run=False, publishing_enabled=True, database_url="sqlite:///:memory:", backoff_base_seconds=60)
    db, _, _ = _setup(":memory:", s)
    generate_pack(db, SHOWCASE_ASSET, s, today=DEMO_TODAY)
    acc = "carjitsu.x.carjitsuusa"
    os.environ["DEMO_FAKE_X_TOKEN"] = "not-a-real-token"
    acc_mod.set_verification(db, acc, "ownership_confirmed", "rehearsal")
    acc_mod.connect_credentials(db, acc, "env:DEMO_FAKE_X_TOKEN", "000000", "rehearsal")
    acc_mod.authorize_publishing(db, acc, "rehearsal")
    vid = f"{SHOWCASE_ASSET}:{acc}"
    approve(db, vid, "rehearsal.editor")
    t0 = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
    schedule(db, vid, t0, "rehearsal")
    calls = []
    script = [HttpResponse(503, {"title": "Service Unavailable"}), HttpResponse(429, {"title": "Too Many Requests"}),
              HttpResponse(201, {"data": {"id": "1850000000000000001", "text": "…"}})]

    def transport(req):
        calls.append(req.redacted())
        return script[min(len(calls) - 1, len(script) - 1)]
    now = t0
    for _ in range(3):
        r = run_publisher(db, s, now=now, transport=transport)
        lines.append(f"  {now.isoformat()} → {r}")
        nxt = db.one("SELECT next_attempt_at FROM publish_jobs")["next_attempt_at"]
        now = datetime.fromisoformat(nxt) + timedelta(seconds=1)
    j = db.one("SELECT state, attempts, permalink FROM publish_jobs")
    lines.append(f"  final job: {dict(j)} · HTTP calls made: {len(calls)} (Authorization header redacted in logs)")
    again = run_publisher(db, s, now=now + timedelta(hours=1), transport=transport)
    lines.append(f"  re-run publisher after success → {again or 'nothing due'} (no duplicate post; calls still {len(calls)})")
    # terminal auth failure
    db2, _, _ = _setup(":memory:", s)
    generate_pack(db2, SHOWCASE_ASSET, s, today=DEMO_TODAY)
    acc_mod.set_verification(db2, acc, "ownership_confirmed", "rehearsal")
    acc_mod.connect_credentials(db2, acc, "env:DEMO_FAKE_X_TOKEN", "000000", "rehearsal")
    acc_mod.authorize_publishing(db2, acc, "rehearsal")
    approve(db2, vid, "rehearsal.editor")
    schedule(db2, vid, t0, "rehearsal")
    r = run_publisher(db2, s, now=t0, transport=lambda req: HttpResponse(401, {"title": "Unauthorized"}))
    lines.append(f"  401 Unauthorized → {r[0]['state']} (kind={r[0].get('kind')}); no retry storm; rollback guidance stored")
    os.environ.pop("DEMO_FAKE_X_TOKEN", None)
    return "\n".join(lines)
