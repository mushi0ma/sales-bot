#!/usr/bin/env python3
"""python3 -I stack/orchestrator/test_funnel.py: the funnel rules, no network."""
import pathlib, sys, unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import funnel as f  # noqa: E402


class Funnel(unittest.TestCase):
    def test_forward_only(self):
        self.assertEqual(f.advance(None, "NEW"), "NEW")
        self.assertEqual(f.advance("NEW", "OFFERED"), "OFFERED")
        self.assertEqual(f.advance("REPLIED", "OFFERED"), "REPLIED")  # a late offer.sent does not pull it back
        self.assertEqual(f.advance("LEAD", "NEW"), "LEAD")
        self.assertEqual(f.advance("OFFERED", None), "OFFERED")

    def test_lost(self):
        self.assertEqual(f.advance("REPLIED", "LOST"), "LOST")
        self.assertEqual(f.advance("LOST", "REPLIED"), "LOST")  # an automatic event does not reopen
        self.assertEqual(f.advance("LOST", "LEAD"), "LEAD")  # an explicit request does

    def test_call_stage_and_channel(self):
        self.assertIsNone(f.stage_for({"type": "call.ended", "data": {"direction": "out", "campaign": "votes"}}))
        self.assertEqual(f.stage_for({"type": "call.ended", "data": {"lead": {"role": "председатель"}}}), "LEAD")
        self.assertEqual(f.channel_for({"type": "call.ended", "data": {"direction": "out"}}), "CALL_OUT")
        self.assertEqual(f.channel_for({"type": "call.ended", "data": {}}), "CALL_IN")
        self.assertEqual(f.channel_for({"type": "dm.replied", "data": {"channel": "telegram"}}), "TELEGRAM")

    def test_pick_opportunity(self):
        opps = [{"id": "a", "stage": "OFFERED", "service": "ONLINE_VOTING", "createdAt": "2026-11-01"},
                {"id": "b", "stage": "LOST", "service": "DISPATCHER", "createdAt": "2026-11-05"},
                {"id": "c", "stage": "REPLIED", "service": "PAYMENTS", "createdAt": "2026-11-03"}]
        self.assertEqual(f.pick_opportunity(opps, "ONLINE_VOTING")["id"], "a")
        self.assertIsNone(f.pick_opportunity(opps, "ACCOUNTING"))  # a new service: a new card
        self.assertEqual(f.pick_opportunity(opps, None)["id"], "c")  # latest open, LOST skipped
        self.assertIsNone(f.pick_opportunity([], None))


if __name__ == "__main__":
    unittest.main(verbosity=1)
