#!/usr/bin/env python3
"""Offline tests of tg-userbot's chat rules and message mapping (no Telegram, no Chatwoot): python3 -I test_userbot.py"""
import os, sys, tempfile, time, unittest

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import userbot as u  # noqa: E402


class Rules(unittest.TestCase):
    def setUp(self):
        for f in ("userbot.db", "userbot.db-wal", "userbot.db-shm"):
            try:
                os.remove(os.path.join(u.DATA, f))
            except FileNotFoundError:
                pass
        u.init_db()

    def group_msg(self, chat, sender, msg_id=1):
        with u.db() as c:
            c.execute("INSERT INTO group_messages (chat_id, msg_id, sender_id, sent_at, text) VALUES (?,?,?,?,?)",
                      (chat, msg_id, sender, u.now_iso(), "x"))

    def test_identifier(self):
        self.assertEqual(u.tg_identifier("tg:123"), 123)
        self.assertEqual(u.tg_identifier("tg:-1001234"), -1001234)
        for bad in ("", "tg:", "tg:abc", "wa:123", "tg:12-3"):
            self.assertIsNone(u.tg_identifier(bad), bad)

    def test_private_allowed_by_group(self):
        self.assertFalse(u.private_allowed(42), "default deny")
        self.group_msg(-100, 42)
        self.assertFalse(u.private_allowed(42), "a group that is not allowed does not count")
        u.allow(-100, "group", "Дом 5")
        self.assertTrue(u.private_allowed(42), "wrote in an allowed group")
        self.assertFalse(u.private_allowed(43))
        u.allow(43, "private", "")
        self.assertTrue(u.private_allowed(43), "allowed by the owner")

    def test_bridge_since(self):
        first = u.bridge_since(-200)
        time.sleep(1.1)
        self.assertEqual(u.bridge_since(-200), first, "a group's start does not move")
        u.allow(-200, "group", "Дом 7")
        self.assertGreater(u.bridge_since(-200), first, "allowing again starts the bridge again")

    def test_contact_source(self):
        gid = {"conversation": {"id": 13, "contact_inbox": "#<GlobalID gid://chatwoot/ContactInbox/16>",
                                "messages": [{"conversation": {"contact_inbox": {"source_id": "tg:42"}}}]}}
        self.assertEqual(u.contact_source(gid), "tg:42", "the GlobalID string is skipped")
        self.assertEqual(u.contact_source({"conversation": {"contact_inbox": {"source_id": "abc"}}}), "abc")
        self.assertIsNone(u.contact_source({"conversation": {"messages": []}}))

    def test_msg_map(self):
        u.map_msg(-100, 7, 501)
        u.map_msg(-100, 8, 502)
        self.assertEqual(u.cw_msg_of(-100, 7), 501)
        self.assertEqual(u.tg_msg_of(-100, 502), 8)
        self.assertIsNone(u.cw_msg_of(-101, 7), "per group")
        self.assertIsNone(u.tg_msg_of(-100, 999))


if __name__ == "__main__":
    unittest.main()
