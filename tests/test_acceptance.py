"""v0.4 acceptance tests (see CLAUDE_CODE_PROMPT.md → Acceptance tests)."""
import csv
import unittest
from dataclasses import replace
from datetime import timedelta

from pln_agent import accounts as acc_mod
from pln_agent import migration as mig
from pln_agent.adapters import ADAPTERS, HttpResponse
from pln_agent.config import CONFIG_DIR
from pln_agent.routing import routes_for_asset
from pln_agent.strategists import similarity
from pln_agent.workflow import WorkflowError, approve, run_publisher, schedule, set_kill_switch

from tests.helpers import CJ, PUTT, SLAP, SLOT, WPL, go_live, make

LIVE = dict(dry_run=False, publishing_enabled=True)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)

    def test_28_entries_load_verbatim_without_drops_or_dedupe(self):
        with open(CONFIG_DIR / "accounts.csv") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 28)
        user = self.db.all("accounts", "source='user_csv'")
        self.assertEqual(len(user), 28)
        self.assertEqual(len({a["account_id"] for a in user}), 28)
        self.assertEqual(sorted(r["username"] for r in rows), sorted(a["username_original"] for a in user))
        # same handle on several platforms/brands is kept (e.g. @proleaguenetwork on 4 platforms, @str33tofficial on 2)
        self.assertEqual(sum(a["username_original"] == "@proleaguenetwork" for a in user), 4)
        self.assertTrue(all(a["verification_status"] == "unverified" for a in user))

    def test_reload_is_idempotent_and_preserves_human_verification(self):
        acc_mod.set_verification(self.db, "carjitsu.tiktok.carjitsuchampionship", "ownership_confirmed", "t")
        acc_mod.load_accounts(self.db)
        self.assertEqual(len(self.db.all("accounts")), 29)
        self.assertEqual(self.db.get("accounts", "account_id", "carjitsu.tiktok.carjitsuchampionship")["verification_status"],
                         "ownership_confirmed")

    def test_linkedin_company_page_is_the_supplied_org(self):
        li = self.db.get("accounts", "account_id", "pln.linkedin.pending")
        self.assertEqual(li["stable_platform_id"], "91515598")
        self.assertEqual(li["role"], "canonical")
        self.assertEqual(li["hard_block_reason"], "")
        self.assertEqual(li["credential_ref"], "")  # not connected until a Page admin signs in

    def test_brand_ids_and_account_ids_are_separate(self):
        wpl = self.db.get("accounts", "account_id", "world_putting_league.instagram.worldputtingleague")
        self.assertEqual((wpl["brand_id"], wpl["canonical_brand_id"]), ("world_putting_league", "putt_tour"))

    def test_credential_ref_rejects_raw_tokens(self):
        with self.assertRaises(ValueError):
            acc_mod.connect_credentials(self.db, "carjitsu.x.carjitsuusa", "EAABsbCS1iHgBA_rawtoken", "1", "t")

    def test_unverified_accounts_cannot_publish_even_with_global_gates_open(self):
        live = replace(self.s, **LIVE)
        for a in self.db.all("accounts"):
            ok, reasons = acc_mod.publish_eligibility(a, live, kill_switch=False)
            self.assertFalse(ok, a["account_id"])
            self.assertTrue(any("not oauth_connected" in r for r in reasons))

    def test_secondary_tiktok_and_footgolf_hard_blocked_until_cleared(self):
        for aid in ("pln.tiktok.pro_league_networ", "world_footgolf_tour.tiktok.ezz_abde"):
            with self.assertRaises(PermissionError):
                acc_mod.clear_hard_block(self.db, aid, "t", "because")  # ownership not confirmed
            acc_mod.set_verification(self.db, aid, "ownership_confirmed", "t")
            acc_mod.connect_credentials(self.db, aid, "env:X", "1", "t")
            with self.assertRaises(PermissionError):
                acc_mod.authorize_publishing(self.db, aid, "t")  # hard block still present


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)

    def _routes(self, asset_id):
        a = self.db.get("assets", "asset_id", asset_id)
        routes, skipped = routes_for_asset(self.db, a)
        return {r.account["account_id"]: r.role for r in routes}, skipped

    def test_carjitsu_routes_canonical_first_then_pln(self):
        r, skipped = self._routes(CJ)
        for aid in ("carjitsu.instagram.carjitsu_championship", "carjitsu.tiktok.carjitsuchampionship",
                    "carjitsu.youtube.carjitsuchampionship6540", "carjitsu.x.carjitsuusa"):
            self.assertEqual(r[aid], "canonical")
        self.assertEqual(r["pln.instagram.proleaguenetwork"], "pln_amplification")
        self.assertEqual(r["pln.linkedin.pending"], "pln_b2b")
        self.assertNotIn("carjitsu.facebook", " ".join(r))  # no CarJitsu Facebook was supplied → not presumed

    def test_pln_secondary_tiktok_never_routed(self):
        r, skipped = self._routes(CJ)
        self.assertNotIn("pln.tiktok.pro_league_networ", r)
        self.assertTrue(any("pro_league_networ" in s for s in skipped))
        self.assertIn("pln.tiktok.proleaguenetwork", r)

    def test_putt_tour_vs_wpl_instagram_and_wpl_only_platforms(self):
        r, _ = self._routes(PUTT)
        self.assertEqual(r["putt_tour.instagram.putttour"], "canonical")
        self.assertEqual(r["world_putting_league.instagram.worldputtingleague"], "legacy_bridge")
        for aid in ("world_putting_league.tiktok.worldputtingleague", "world_putting_league.youtube.worldputtingleague",
                    "world_putting_league.x.worldputtleague"):
            self.assertEqual(r[aid], "legacy_inplace")

    def test_wpl_asset_maps_to_putt_tour_preserving_legacy_identity(self):
        a = self.db.get("assets", "asset_id", WPL)
        self.assertEqual(a["brand_id"], "putt_tour")
        self.assertEqual(a["source_brand_name"], "World Putting League")

    def test_footgolf_creator_account_unrouted(self):
        accs = {a["account_id"] for a in self.db.all("accounts")}
        self.assertIn("world_footgolf_tour.tiktok.ezz_abde", accs)
        self.assertFalse(acc_mod.planning_eligible(self.db.get("accounts", "account_id", "world_footgolf_tour.tiktok.ezz_abde")))

    def test_distinct_p0_route_sets(self):
        pln_like = self._routes(CJ)[0]
        putt = self._routes(PUTT)[0]
        self.assertNotEqual(set(pln_like), set(putt))
        self.assertTrue(any(k.startswith("carjitsu.") for k in pln_like))
        self.assertTrue(any(k.startswith("putt_tour.") for k in putt))


class MigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)

    def test_no_adapter_can_rename_or_delete(self):
        for cls in ADAPTERS.values():
            for forbidden in ("rename", "update_profile", "change_username", "delete", "delete_post", "merge"):
                self.assertFalse(hasattr(cls, forbidden), f"{cls.__name__}.{forbidden}")
            caps = cls().capabilities()
            self.assertFalse(caps["profile_edits"]) and self.assertFalse(caps["delete"])

    def test_accounts_not_merged_and_proposals_require_signed_off_decision(self):
        props = mig.propose_profile_changes(self.db, "t")
        self.assertTrue(props)
        self.assertTrue(all(p["status"] == "proposed" for p in self.db.all("profile_change_proposals")))
        self.assertEqual(len(self.db.all("accounts", "brand_id='world_putting_league'")), 4)
        pid = self.db.all("profile_change_proposals")[0]["id"]
        with self.assertRaises(PermissionError):
            mig.review_proposal(self.db, pid, "approved_for_manual_change", "t")
        mig.approve_decision(self.db, "instagram", "lead")
        ig = [p for p in self.db.all("profile_change_proposals") if "instagram" in p["account_id"]][0]
        mig.review_proposal(self.db, ig["id"], "approved_for_manual_change", "lead")
        self.assertEqual(self.db.get("profile_change_proposals", "id", ig["id"])["status"], "approved_for_manual_change")
        # username of the legacy account never changed by the system
        self.assertEqual(self.db.get("accounts", "account_id", ig["account_id"])["username_original"], "@worldputtingleague")

    def test_decision_sheet_without_evidence_is_provisional(self):
        for sh in mig.decision_sheet(self.db):
            self.assertIn("audit required", sh["confidence"])
            self.assertFalse(sh["follower_transfer_assumed"])
            self.assertFalse(sh["auto_rename"])

    def test_decision_sheet_with_evidence(self):
        from pathlib import Path
        from pln_agent.metrics import import_csv
        import_csv(self.db, Path(__file__).parent / "fixtures" / "synthetic_metrics.csv")
        ig = [s for s in mig.decision_sheet(self.db) if s["platform"] == "instagram"][0]
        self.assertEqual(ig["recommended_option"], "inplace_rebrand")  # 12000 vs 1500 (synthetic)
        self.assertEqual(ig["confidence"], "evidence-based")

    def test_ninety_day_plan_has_approval_checkpoints(self):
        plan = mig.ninety_day_plan()
        self.assertEqual(max(t["day"] for t in plan), 90)
        self.assertGreaterEqual(sum(1 for t in plan if t["gate"] and "CHECKPOINT" in t["gate"]), 4)


class CreativeTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()

    def test_one_clip_distinct_creative_per_platform(self):
        vs = self.db.all("variants", "asset_id=?", [CJ])
        platforms = {v["platform"] for v in vs}
        self.assertTrue({"linkedin", "facebook", "instagram", "tiktok"} <= platforms)
        clean = [v for v in vs if v["state"] == "in_review"]
        self.assertGreaterEqual(len(clean), 8)
        for i, a in enumerate(clean):
            for b in clean[i + 1:]:
                self.assertLess(similarity(a["caption"], b["caption"]), 0.45, (a["variant_id"], b["variant_id"]))
        core = [v for v in vs if v["platform"] in ("linkedin", "facebook", "instagram", "tiktok") and v["route_role"] != "pln_amplification"
                or v["account_id"] == "pln.facebook.pro_league_network"]
        self.assertEqual(len({v["hook"] for v in core}), len(core))
        self.assertEqual(len({v["edl"]["output"]["h"] / v["edl"]["output"]["w"] for v in core}), 3)  # 9:16, 4:5, 1:1

    def test_edl_grounded_in_annotated_timecodes_within_clip(self):
        a = self.db.get("assets", "asset_id", CJ)
        for v in self.db.all("variants", "asset_id=? AND platform != 'twitch'", [CJ]):
            for s in v["edl"]["segments"]:
                self.assertLessEqual(s["source_out"], a["duration_s"])
                self.assertTrue(s["source_timecode"])

    def test_validation_catches_invented_facts_and_clickbait(self):
        from pln_agent.workflow import edit_variant
        vid = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"
        v = edit_variant(self.db, vid, {"caption": "You won't believe this INSANE 47-second finish!"}, "t")
        self.assertEqual(v["state"], "needs_fix")
        errs = " ".join(v["validation"]["errors"])
        self.assertIn("banned", errs)
        self.assertIn("47", errs)

    def test_unverified_people_names_rejected(self):
        from pln_agent.workflow import revalidate
        self.db.update("variants", "variant_id", f"{SLAP}:slapfight.x.slapfightusa", {"caption": "Unverified Competitor lands it"})
        errs = revalidate(self.db, f"{SLAP}:slapfight.x.slapfightusa")["errors"]
        self.assertTrue(any("unverified person" in e for e in errs))

    def test_no_video_creative_without_annotated_footage(self):
        self.db.update("assets", "asset_id", CJ, {"moments": []})
        from pln_agent.editorial import generate_pack
        from tests.helpers import TODAY
        self.db.execute("DELETE FROM variants WHERE asset_id=?", [CJ])
        p = generate_pack(self.db, CJ, self.s, today=TODAY)
        self.assertFalse([v for v in p["variants"] if v["platform"] in ("tiktok", "instagram")])
        self.assertTrue(any("won't guess footage" in n for n in p["notes"]))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()
        self.cj_tt = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"

    def test_rights_gate_blocks_expired_and_unlicensed(self):
        for vid in (f"{WPL}:putt_tour.instagram.putttour", f"{SLAP}:slapfight.instagram.slapfight"):
            self.assertEqual(self.db.get("variants", "variant_id", vid)["state"], "needs_fix")
            with self.assertRaises(WorkflowError):
                approve(self.db, vid, "editor")

    def test_rights_rechecked_at_publish_time(self):
        approve(self.db, self.cj_tt, "editor")
        schedule(self.db, self.cj_tt, SLOT, "t")
        self.db.update("assets", "asset_id", CJ, {"rights_expires_at": "2026-10-21T00:00:00+00:00"})
        r = run_publisher(self.db, self.s, now=SLOT)
        self.assertEqual(r[0]["state"], "blocked")
        self.assertEqual(self.db.get("variants", "variant_id", self.cj_tt)["state"], "needs_fix")

    def test_approval_required_and_bound_to_exact_text(self):
        from pln_agent.workflow import edit_variant
        with self.assertRaises(WorkflowError):
            schedule(self.db, self.cj_tt, SLOT, "t")
        approve(self.db, self.cj_tt, "editor")
        edit_variant(self.db, self.cj_tt, {"cta": "Follow for the rematch"}, "editor")  # cta change keeps caption hash…
        v = self.db.get("variants", "variant_id", self.cj_tt)
        self.assertEqual(v["state"], "in_review")  # …but any edit drops back to review
        edit_variant(self.db, self.cj_tt, {"caption": v["caption"] + " Rematch soon."}, "editor")
        r = approve(self.db, self.cj_tt, "editor2")
        self.assertEqual(r["approvers"], ["editor2"])  # old approval no longer counts

    def test_one_approver_for_paid_partnership(self):
        self.assertEqual(approve(self.db, f"{PUTT}:putt_tour.instagram.putttour", "a")["state"], "approved")

    def test_two_person_signoff_when_policy_lists_the_flag(self):
        from unittest import mock
        from pln_agent import rights
        mock.patch.object(rights, "SENSITIVE", {"paid_partnership"}).start()
        self.addCleanup(mock.patch.stopall)
        vid = f"{PUTT}:putt_tour.instagram.putttour"
        self.assertEqual(approve(self.db, vid, "a")["state"], "in_review")
        self.assertEqual(approve(self.db, vid, "a")["state"], "in_review")
        self.assertEqual(approve(self.db, vid, "b")["state"], "approved")

    def test_evergreen_policy_off_by_default_and_never_for_flagged(self):
        from pln_agent.workflow import apply_evergreen_policy
        self.assertEqual(apply_evergreen_policy(self.db), [])
        with self.assertRaises(WorkflowError):
            approve(self.db, f"{PUTT}:putt_tour.instagram.putttour", "policy:evergreen")

    def test_no_duplicate_scheduling(self):
        approve(self.db, self.cj_tt, "editor")
        schedule(self.db, self.cj_tt, SLOT, "t")
        self.db.update("variants", "variant_id", self.cj_tt, {"state": "approved"})  # stale double-submit
        with self.assertRaises(WorkflowError) as e:
            schedule(self.db, self.cj_tt, SLOT + timedelta(hours=2), "t")
        self.assertIn("duplicate", str(e.exception))
        self.assertEqual(len(self.db.all("publish_jobs")), 1)

    def test_emergency_stop(self):
        approve(self.db, self.cj_tt, "editor")
        schedule(self.db, self.cj_tt, SLOT, "t")
        set_kill_switch(self.db, True, "ops", "drill")
        self.assertEqual(run_publisher(self.db, self.s, now=SLOT), [{"skipped": "emergency stop is ON"}])
        self.assertEqual(self.db.all("publish_jobs")[0]["state"], "halted")
        live = replace(self.s, **LIVE)
        go_live(self.db, "carjitsu.tiktok.carjitsuchampionship")
        ok, reasons = acc_mod.publish_eligibility(self.db.get("accounts", "account_id", "carjitsu.tiktok.carjitsuchampionship"), live, True)
        self.assertFalse(ok)
        self.assertIn("emergency stop is ON", reasons)

    def test_dry_run_simulates_without_any_http(self):
        calls = []
        approve(self.db, self.cj_tt, "editor")
        schedule(self.db, self.cj_tt, SLOT, "t")
        r = run_publisher(self.db, self.s, now=SLOT, transport=lambda req: calls.append(req))
        self.assertEqual(r[0]["state"], "simulated")
        self.assertEqual(calls, [])
        j = self.db.all("publish_jobs")[0]
        self.assertTrue(j["permalink"].startswith("dryrun://"))
        self.assertEqual(self.db.get("variants", "variant_id", self.cj_tt)["state"], "simulated")  # never 'published'
        sent = j["receipt"]["would_send"]
        self.assertTrue(all("REDACTED" in str(x["headers"]) for x in sent if x["headers"]))

    def test_linkedin_pending_can_be_planned_not_posted(self):
        vid = f"{CJ}:pln.linkedin.pending"
        approve(self.db, vid, "editor")
        schedule(self.db, vid, SLOT, "t")
        live = replace(self.s, **LIVE)
        calls = []
        r = run_publisher(self.db, live, now=SLOT, transport=lambda req: calls.append(req))
        self.assertEqual(r[0]["state"], "simulated")
        self.assertTrue(any("not oauth_connected" in x for x in r[0]["why_not_live"]))
        self.assertEqual(calls, [])
        with self.assertRaises(PermissionError):
            acc_mod.authorize_publishing(self.db, "pln.linkedin.pending", "t")


class LivePathFailureTests(unittest.TestCase):
    """Live publisher logic against a mock transport — no network, fake token."""

    def setUp(self):
        self.db, s = make()
        self.s = replace(s, **LIVE)
        self.acc = "carjitsu.x.carjitsuusa"
        self.vid = f"{CJ}:{self.acc}"
        go_live(self.db, self.acc)
        approve(self.db, self.vid, "editor")
        schedule(self.db, self.vid, SLOT, "t")

    def _run(self, responses, rounds):
        calls, out, now = [], [], SLOT

        def transport(req):
            calls.append(req)
            r = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(r, Exception):
                raise r
            return r
        for _ in range(rounds):
            out.append(run_publisher(self.db, self.s, now=now, transport=transport))
            nxt = self.db.all("publish_jobs")[0]["next_attempt_at"]
            from datetime import datetime
            now = max(now, datetime.fromisoformat(nxt)) + timedelta(seconds=1)
        return calls, out

    def test_retry_with_exponential_backoff_then_success_and_no_duplicate(self):
        calls, out = self._run([HttpResponse(503, {}), HttpResponse(429, {}), ConnectionError("reset"),
                                HttpResponse(201, {"data": {"id": "999"}})], 6)
        j = self.db.all("publish_jobs")[0]
        self.assertEqual(j["state"], "published")
        self.assertEqual(j["attempts"], 4)
        self.assertEqual(len(calls), 4)  # later rounds made no further calls
        att = self.db.all("publish_attempts", order="id")
        self.assertEqual([a["error_kind"] for a in att], ["retryable", "rate_limit", "retryable", None])
        self.assertEqual(self.db.get("variants", "variant_id", self.vid)["state"], "published")
        self.assertIn("999", j["permalink"])

    def test_auth_failure_is_terminal(self):
        calls, out = self._run([HttpResponse(401, {"title": "Unauthorized"})], 3)
        self.assertEqual(self.db.all("publish_jobs")[0]["state"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertIn("Manual rollback", self.db.all("publish_jobs")[0]["receipt"]["guidance"])

    def test_max_attempts_then_failed(self):
        self.s = replace(self.s, max_publish_attempts=3)
        calls, _ = self._run([HttpResponse(500, {})], 6)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.db.all("publish_jobs")[0]["state"], "failed")

    def test_crash_mid_publish_is_not_blindly_retried(self):
        from datetime import datetime, timezone
        j = self.db.all("publish_jobs")[0]
        self.db.update("publish_jobs", "job_id", j["job_id"], {"state": "publishing", "locked_at": "2026-10-21T15:00:00+00:00"})
        calls = []
        run_publisher(self.db, self.s, now=datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc), transport=lambda r: calls.append(r))
        self.assertEqual(self.db.all("publish_jobs")[0]["state"], "needs_manual_check")
        self.assertEqual(calls, [])

    def test_daily_rate_cap_defers(self):
        import json
        cap = json.loads((CONFIG_DIR / "allocation.json").read_text())["max_posts_per_account_per_day"]["x"]
        from pln_agent.db import now_iso
        for i in range(cap):
            self.db.insert("publish_jobs", {"job_id": f"prior{i}", "variant_id": "x", "account_id": self.acc,
                                            "idempotency_key": f"k{i}", "state": "published", "mode": "live",
                                            "created_at": now_iso(), "updated_at": SLOT.isoformat()})
        r = run_publisher(self.db, self.s, now=SLOT, transport=lambda req: HttpResponse(201, {"data": {"id": "1"}}))
        self.assertEqual(r[0]["state"], "deferred")

    def test_tiktok_unaudited_forces_private_and_checks_creator_info(self):
        from pln_agent.adapters import get_adapter
        from pln_agent.adapters.base import AdapterError
        import os
        os.environ["PLN_MEDIA_PUBLIC_BASE_URL"] = "https://cdn.example.invalid"
        try:
            acc = "carjitsu.tiktok.carjitsuchampionship"
            go_live(self.db, acc)
            v = self.db.get("variants", "variant_id", f"{CJ}:{acc}")
            v["render_path"] = "/x/y/video.mp4"
            v["public_media_url"] = "https://cdn.example.invalid/v/abc/carjitsu-tiktok.mp4"
            a = get_adapter("tiktok", transport=lambda req: HttpResponse(200, {"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE"]}}))
            init = a.plan_requests(v, self.db.get("accounts", "account_id", acc), "u")[1]
            self.assertEqual(init.json_body["post_info"]["privacy_level"], "SELF_ONLY")
            with self.assertRaises(AdapterError) as e:
                a.publish(v, self.db.get("accounts", "account_id", acc), "k")
            self.assertEqual(e.exception.kind, "validation")
        finally:
            os.environ.pop("PLN_MEDIA_PUBLIC_BASE_URL", None)

    def test_pull_based_apis_blocked_without_public_media_url(self):
        from pln_agent.adapters import get_adapter
        from pln_agent.adapters.base import AdapterError
        acc = "pln.instagram.proleaguenetwork"
        go_live(self.db, acc)
        v = self.db.get("variants", "variant_id", f"{CJ}:{acc}")
        v["render_path"] = "/x/y/video.mp4"
        with self.assertRaises(AdapterError) as e:
            get_adapter("instagram", transport=lambda r: HttpResponse(200, {"id": "1"})).publish(
                v, self.db.get("accounts", "account_id", acc), "k")
        self.assertEqual(e.exception.kind, "blocked")


if __name__ == "__main__":
    unittest.main()
