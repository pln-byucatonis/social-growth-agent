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
