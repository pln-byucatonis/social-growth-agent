"""The run sheet proposes only for the worked accounts that are connected (config/focus.json)."""
import unittest

from pln_agent import runsheet

from tests.helpers import SLOT, make


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.day = SLOT.date()

    def test_only_connected_worked_accounts_are_proposed(self):
        self.db.update("accounts", "account_id", "putt_tour.instagram.putttour", {"verification_status": "oauth_connected"})
        sheet = runsheet.day_sheet(self.db, self.s, self.day)
        props = {r["account_id"] for r in sheet["rows"] if r["kind"] == "proposed"}
        self.assertTrue(props <= set(sheet["focus"]["connected"]), props)
        self.assertIn("putt_tour.instagram.putttour", sheet["focus"]["connected"])
        self.assertIn("carjitsu.tiktok.carjitsuchampionship", sheet["focus"]["waiting"])
        everything = runsheet.day_sheet(self.db, self.s, self.day, focus=False)
        self.assertGreaterEqual(len(everything["rows"]), len(sheet["rows"]))
        self.assertEqual(everything["focus"], {})


if __name__ == "__main__":
    unittest.main()


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)

    def test_focus_limits_drafting_to_worked_and_bridge(self):
        import os
        from unittest import mock
        from pln_agent.accounts import planning_eligible
        with mock.patch.dict(os.environ, {"PLN_FOCUS_DISABLED": "0"}):
            accs = {a["account_id"]: a for a in self.db.all("accounts")}
            self.assertTrue(planning_eligible(accs["carjitsu.instagram.carjitsu_championship"]))
            self.assertTrue(planning_eligible(accs["world_putting_league.instagram.worldputtingleague"]))  # bridge post
            self.assertFalse(planning_eligible(accs["carjitsu.x.carjitsuusa"]))  # parked

    def test_archive_restore_and_remove(self):
        from pln_agent.workflow import archive_drafts, remove_accounts, restore_archived
        keep = {v["variant_id"] for v in self.db.all("variants")[:1]}
        n = archive_drafts(self.db, "bill", "backlog", keep)
        self.assertGreater(n, 0)
        open_ = {v["variant_id"] for v in self.db.all("variants", "state IN ('in_review','needs_fix','approved')")}
        self.assertTrue(open_ <= keep)
        self.assertEqual(restore_archived(self.db, "bill"), n)
        removed = remove_accounts(self.db, ["slapfight.x.slapfightusa", "nope"], "removed", "bill")
        self.assertEqual(removed, ["slapfight.x.slapfightusa"])
        self.assertEqual(self.db.get("accounts", "account_id", "slapfight.x.slapfightusa")["hard_block_reason"], "removed")


class DailyLimitTests(unittest.TestCase):
    def test_a_full_day_moves_the_post_and_says_so(self):
        from datetime import timedelta
        from pln_agent.workflow import schedule
        db, s = make(packs=True)
        acc = "carjitsu.instagram.carjitsu_championship"
        mine = [v for v in db.all("variants", "account_id=?", [acc])]
        for v in mine[:3]:
            db.update("variants", "variant_id", v["variant_id"], {"state": "approved"})
        ids = [v["variant_id"] for v in mine[:3]]
        if len(ids) < 3:
            for i in range(3 - len(ids)):
                twin = {**mine[0], "variant_id": f"{mine[0]['variant_id']}-t{i}", "asset_id": f"a-t{i}", "caption_hash": f"h{i}",
                        "state": "approved"}
                db.insert("variants", twin)
                ids.append(twin["variant_id"])
        j1 = schedule(db, ids[0], SLOT, "bill")
        j2 = schedule(db, ids[1], SLOT + timedelta(hours=2), "bill")
        j3 = schedule(db, ids[2], SLOT + timedelta(hours=4), "bill")
        self.assertFalse(j1["moved"] or j2["moved"])
        self.assertIn("moved to the same time", j3["moved"])
        self.assertEqual(j3["next_attempt_at"][:10], (SLOT + timedelta(days=1)).date().isoformat())
