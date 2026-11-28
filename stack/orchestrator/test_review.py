#!/usr/bin/env python3
"""python3 -I stack/orchestrator/test_review.py: the analyst's pure parts (no Chatwoot, no LLM, no Twenty)."""
import json, os, pathlib, sys, tempfile, unittest

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("CATALOG_PATH", str(HERE.parent / "group-watcher" / "services.json"))
os.environ.setdefault("CAMPAIGNS_PATH", str(HERE.parent / "voice" / "campaigns.json"))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "p.db"))
sys.path.insert(0, str(HERE))
import review as rv  # noqa: E402


def judged(**kw):
    r = {"analysis": "", "agent": {"relevance": 4, "accuracy": 9, "discovery": 0, "objection_handling": 0,
                                    "next_step": 3, "tone": 5, "brevity": "x"},
         "techniques": [{"name": "empathy", "turn": 2, "effect": "positive"}, {"name": "hypnosis", "turn": 3,
                                                                                "effect": "negative"}],
         "objections": [{"type": "price", "turn": 3, "handled": True}],
         "client": {"sentiment_start": "NEUTRAL", "sentiment_end": "POSITIVE", "engagement": 7, "predicted_csat": 0,
                    "csat_drivers": ["быстро"], "readiness": "HIGH", "role": "CHAIRMAN"},
         "case": {"outcome": "SOMETHING", "lost_reason": "none", "closed": True, "closure_quality": 4,
                  "service_fit": 5, "summary": "s", "next_best_action": "n"},
         "mistakes": ["m"] * 9, "lesson": "l"}
    r.update(kw)
    return rv.normalize(r)


class Review(unittest.TestCase):
    def test_normalize(self):
        r = judged()
        self.assertEqual(r["agent"]["accuracy"], 5, "clamped to 5")
        self.assertEqual(r["agent"]["brevity"], 0, "garbage is not applicable")
        self.assertEqual(r["score"], round((4 + 5 + 3 + 5) / 4, 2), "the mean of applicable criteria only")
        self.assertEqual(r["client"]["engagement"], 5)
        self.assertEqual(r["client"]["predicted_csat"], 1)
        self.assertEqual(r["case"]["outcome"], "OTHER")
        self.assertEqual([t["name"] for t in r["techniques"]], ["empathy"])
        self.assertEqual(len(r["mistakes"]), 5)

    def test_dialog_lines(self):
        rv._me[:] = [2]
        msgs = [{"id": 1, "message_type": 1, "sender": {"type": "user", "id": 2}, "content": "Предложение"},
                {"id": 2, "message_type": 2, "content": "Conversation was reopened"},
                {"id": 3, "message_type": 0, "sender": {"type": "contact", "id": 7}, "content": "Сколько стоит?"},
                {"id": 4, "message_type": 1, "sender": {"type": "agent_bot", "id": 1}, "content": "Цена такая"},
                {"id": 5, "message_type": 1, "private": True, "sender": {"type": "user", "id": 2}, "content": "📞 звонок"},
                {"id": 6, "message_type": 1, "private": True, "sender": {"type": "agent_bot"}, "content": "Заявка"},
                {"id": 7, "message_type": 1, "sender": {"type": "user", "id": 9}, "content": "Менеджер на связи"}]
        lines = rv.dialog_lines(msgs)
        self.assertEqual([w for w, _ in lines], ["Помощник", "Жилец", "Помощник", "Заметка помощника менеджеру",
                                                 "Менеджер"])
        self.assertTrue(rv.reviewable(lines))
        self.assertFalse(rv.reviewable(lines[:1]))
        self.assertTrue(rv.numbered(lines).startswith("[1] Помощник: Предложение\n[2] Жилец:"))

    def test_schema_enums_match_maps(self):
        s = rv.schema()["properties"]
        self.assertEqual(set(s["case"]["properties"]["outcome"]["enum"]), set(rv.OUTCOMES))
        self.assertIn("none", s["case"]["properties"]["lost_reason"]["enum"])

    def test_report(self):
        rv.save("dm", "9", 7, "a", judged())
        rv.save("dm", "9", 7, "b", judged(case={"outcome": "LEAD", "closed": True, "summary": "", "lost_reason": "none",
                                                "next_best_action": ""}))
        rv.save("call", "3", 7, None, judged(case={"outcome": "NO_INTEREST", "closed": False, "lost_reason": "price",
                                                   "summary": "", "next_best_action": ""}))
        rep = rv.report(1)
        self.assertEqual(rep["dialogs"], 2, "the last review of each dialog counts")
        self.assertEqual(rep["outcomes"], {"заявка": 1, "не интересно": 1})
        self.assertEqual(rep["lost_reasons"], {"цена": 1})
        self.assertEqual(rep["techniques"]["сочувствие, признание проблемы"]["сработал"], 2)
        self.assertEqual(rep["closed_share"], 0.5)
        json.dumps(rep, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=1)
