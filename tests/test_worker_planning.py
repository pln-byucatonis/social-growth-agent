"""The planner gives every clip a turn: a clip it looked at and placed nowhere doesn't come back every minute."""
import unittest
from unittest import mock

from pln_agent import worker
from pln_agent.db import now_iso
from tests.helpers import make


class PlanningTurnsTests(unittest.TestCase):
    def test_clip_placed_nowhere_is_not_retried_every_tick(self):
        db, s = make(packs=False)
        calls = []
        with mock.patch.object(worker, "generate_pack", lambda d, aid, st, actor="": calls.append(aid)):
            worker.plan_new_clear_assets(db, s, limit=1)  # nothing gets made (the fake writes no drafts)…
            first = calls[-1]
            # …but the real planner records a pack every time it looks at a clip
            db.insert("editorial_packs", {"pack_id": "p1", "asset_id": first, "created_at": now_iso(), "summary": {}})
            worker.plan_new_clear_assets(db, s, limit=1)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[1], first, "the same clip came back instead of the next one")


if __name__ == "__main__":
    unittest.main()
