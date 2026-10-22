"""Per-request read cache: identical reads hit the database once, small tables are read once, writes empty it."""
import unittest

from tests.helpers import CJ, make


class RequestCacheTests(unittest.TestCase):
    def setUp(self):
        self.db, _ = make(packs=True)
        self.calls = []
        orig = self.db._raw_query

        def counted(sql, params):
            self.calls.append(sql)
            return orig(sql, params)
        self.db._raw_query = counted

    def test_repeated_reads_hit_once_and_writes_clear(self):
        vid = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"
        with self.db.request_cache():
            for _ in range(5):
                self.db.get("variants", "variant_id", vid)
                self.db.kv_get("anything")
                self.db.get("accounts", "account_id", "carjitsu.instagram.carjitsu_championship")
            self.assertEqual(len(self.calls), 3)  # one variant read, one settings_kv table, one accounts table
            self.db.update("variants", "variant_id", vid, {"hook": "NEW"})
            self.assertEqual(self.db.get("variants", "variant_id", vid)["hook"], "NEW")
        self.calls.clear()
        self.db.get("variants", "variant_id", vid)
        self.db.get("variants", "variant_id", vid)
        self.assertEqual(len(self.calls), 2)  # no cache outside a request

    def test_prefetch_answers_gets(self):
        ids = [v["variant_id"] for v in self.db.all("variants")][:6]
        with self.db.request_cache():
            self.calls.clear()
            self.db.prefetch("variants", "variant_id", ids + ["missing"])
            got = [self.db.get("variants", "variant_id", i) for i in ids]
            self.assertIsNone(self.db.get("variants", "variant_id", "missing"))
            self.assertEqual(len(self.calls), 1)
            self.assertEqual([g["variant_id"] for g in got], ids)

    def test_cached_rows_are_copies(self):
        with self.db.request_cache():
            a = self.db.get("accounts", "account_id", "carjitsu.instagram.carjitsu_championship")
            a["platform"] = "changed"
            b = self.db.get("accounts", "account_id", "carjitsu.instagram.carjitsu_championship")
            self.assertEqual(b["platform"], "instagram")


if __name__ == "__main__":
    unittest.main()
