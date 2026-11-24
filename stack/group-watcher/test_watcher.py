#!/usr/bin/env python3
"""Offline tests of group-watcher's pure logic (no Chatwoot, no LLM): python3 -I test_watcher.py"""
import hashlib, hmac, json, os, sys, tempfile, time, unittest
from datetime import datetime, timezone

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher as w  # noqa: E402


def utc(h, m=0):  # local hour h in UTC+5
    return datetime(2026, 11, 24, (h - 5) % 24, m, tzinfo=timezone.utc)


NEED = {"confidence": 0.9}


class Logic(unittest.TestCase):
    def test_signature(self):
        raw, ts = b'{"a":1}', str(int(time.time()))
        sig = "sha256=" + hmac.new(b"s3", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        self.assertTrue(w.verify_signature("s3", ts, sig, raw))
        self.assertFalse(w.verify_signature("s3", ts, sig, raw + b" "))
        self.assertFalse(w.verify_signature("", ts, sig, raw), "no secret configured must refuse")
        self.assertFalse(w.verify_signature("s3", str(int(time.time()) - 3600), sig, raw), "stale timestamp")

    def test_stop_words(self):
        for t in ["стоп", " Стоп! ", "тоқта", "STOP", "отписаться."]:
            self.assertTrue(w.is_stop(t), t)
        for t in ["стоп-кран сломан", "не стоп", ""]:
            self.assertFalse(w.is_stop(t), t)

    def test_prefilter(self):
        self.assertIn("debt_collection", w.prefilter("Половина дома не платит взносы"))
        self.assertIn("dispatcher", w.prefilter("Лифт үш күннен бері істемейді"))
        self.assertEqual(w.prefilter("Доброе утро, соседи!"), [])
        self.assertEqual(w.prefilter("лифт"), [], "too short to judge")

    def test_quiet_hours(self):
        self.assertTrue(w.in_quiet_hours(utc(22)))
        self.assertTrue(w.in_quiet_hours(utc(3)))
        self.assertFalse(w.in_quiet_hours(utc(9)))
        self.assertFalse(w.in_quiet_hours(utc(20, 59)))

    def test_decide_order(self):
        d = lambda **k: w.decide(NEED, utc(12), **{"mode": "auto", "opted_out": False, "last_offer_at": None,
                                                    "sent_today": 0, **k})
        self.assertEqual(d(), ("send", ""))
        self.assertEqual(d(opted_out=True, mode="approve"), ("skipped", "optout"))
        self.assertEqual(d(last_offer_at=utc(12).timestamp() - 86400), ("skipped", "cooldown"))
        self.assertEqual(d(last_offer_at=utc(12).timestamp() - 8 * 86400), ("send", ""))
        self.assertEqual(d(sent_today=w.DAILY_SEND_CAP), ("skipped", "daily_cap"))
        self.assertEqual(d(mode="approve"), ("awaiting", ""))
        self.assertEqual(d(mode="dry"), ("skipped", "dry_run"))
        self.assertEqual(w.decide(NEED, utc(23), mode="auto", opted_out=False, last_offer_at=None, sent_today=0),
                         ("held", "quiet_hours"))
        self.assertEqual(w.decide({"confidence": 0.5}, utc(12), mode="auto", opted_out=False, last_offer_at=None,
                                  sent_today=0), ("skipped", "low_confidence"))

    def test_prompt_has_no_names(self):
        rows = [{"id": 1, "contact_id": 77, "text": "лифт не работает"}, {"id": 2, "contact_id": 78, "text": "у нас тоже"}]
        system, user = w.build_prompt([], rows)
        self.assertIn("1 NEW A: лифт не работает", user)
        self.assertIn("2 NEW B: у нас тоже", user)

    def test_parse_needs(self):
        rows = [{"id": 5, "text": "Половина дома не платит взносы"}, {"id": 6, "text": "привет"}]
        out = w.parse_needs(json.dumps({"needs": [
            {"message_id": 5, "service": "debt_collection", "confidence": 1.4, "quote": "выдумка", "lang": "ru"},
            {"message_id": 5, "service": "dispatcher", "confidence": 0.9, "quote": "", "lang": "ru"},
            {"message_id": 9, "service": "dispatcher", "confidence": 0.9, "quote": "", "lang": "ru"},
            {"message_id": 6, "service": "no_such", "confidence": 0.9, "quote": "", "lang": "ru"}]}), rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["confidence"], 1.0)
        self.assertEqual(out[0]["quote"], "Половина дома не платит взносы", "a quote not in the text is replaced")
        self.assertEqual(w.parse_needs("not json", rows), [])

    def test_offer_text(self):
        t = w.offer_text({"service": "debt_collection", "author": "Айгуль Б.", "group_name": "ЖК Тест",
                          "quote": "не платят"})
        self.assertTrue(t.startswith("Здравствуйте, Айгуль!"))
        self.assertIn("https://eknot.me/services/debt_collection", t)
        self.assertIn("«стоп»", t)
        self.assertTrue(w.offer_text({"service": "payments", "author": "+77001234567", "quote": "x"})
                        .startswith("Здравствуйте! "), "a phone is not a name")


class Intake(unittest.TestCase):
    def payload(self, **k):
        p = {"event": "message_created", "id": 100, "content": "Лифт не работает третий день", "message_type": "incoming",
             "private": False, "created_at": "2026-11-24T10:00:00Z", "sender": {"id": 7, "name": "A", "type": "contact"},
             "conversation": {"id": 3, "inbox_id": 2, "group_type": "group", "labels": [w.WATCH_LABEL],
                              "meta": {"sender": {"name": "ЖК Тест"}}}}
        for key, v in k.items():
            if key in ("labels", "group_type"):
                p["conversation"][key] = v
            else:
                p[key] = v
        return p

    def test_stores_only_allowed_groups(self):
        self.assertEqual(w.intake(self.payload()), "stored")
        self.assertEqual(w.q("SELECT conv_id, contact_id, group_name FROM messages WHERE id=100")[0]["group_name"],
                         "ЖК Тест")
        self.assertEqual(w.intake(self.payload(id=101, labels=[])), "ignored:group_not_allowed")
        self.assertEqual(w.intake(self.payload(id=102, group_type="individual")), "ignored:individual")
        self.assertEqual(w.intake(self.payload(id=103, message_type="outgoing")), "ignored:not_incoming")
        self.assertEqual(w.intake({"event": "conversation_created"}), "ignored:event")


if __name__ == "__main__":
    unittest.main(verbosity=1)
