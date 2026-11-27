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

    def test_tester_policy(self):
        d = lambda **k: w.decide(NEED, k.pop("at", utc(12)), **{"mode": "auto", "opted_out": False,
                                                                 "last_offer_at": None, "sent_today": 0, **k})
        hour_ago = utc(12).timestamp() - 3600
        self.assertEqual(d(last_offer_at=hour_ago), ("skipped", "cooldown"), "a resident: 7 days")
        self.assertEqual(d(last_offer_at=hour_ago, tester=True), ("send", ""), "a tester: 30 minutes")
        self.assertEqual(d(last_offer_at=utc(12).timestamp() - 600, tester=True), ("skipped", "cooldown"))
        self.assertEqual(d(at=utc(22)), ("held", "quiet_hours"))
        self.assertEqual(d(at=utc(22), tester=True), ("send", ""), "testers until 23:00")
        self.assertEqual(d(at=utc(23, 30), tester=True), ("held", "quiet_hours"))
        self.assertEqual(d(at=utc(8), tester=True), ("send", ""))
        self.assertEqual(d(opted_out=True, tester=True), ("skipped", "optout"), "«стоп» still wins")

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

    def test_choose_path(self):
        self.assertEqual(w.choose_path(7, "B", 0), "A", "no cloud inbox: always A")
        self.assertEqual(w.choose_path(7, "A", 5), "A")
        self.assertEqual(w.choose_path(8, "B", 5), "B")
        self.assertEqual([w.choose_path(i, "SPLIT", 5) for i in (7, 8, 7)], ["B", "A", "B"], "stable per contact")

    def test_offer_template(self):
        n = {"service": "dispatcher", "author": "Айгерим Б.", "lang": "kk", "quote": "лифт"}
        m = w.offer_template(n)
        tp = m["template_params"]
        self.assertEqual((tp["name"], tp["language"]), (w.TEMPLATES["kk"], "kk"))
        self.assertEqual(tp["processed_params"]["body"]["1"], "Айгерим")
        self.assertIn(w.CATALOG["dispatcher"]["url"], m["content"])
        self.assertNotIn("{", m["content"])
        n.update(author="+77001234567", lang="ru")
        self.assertEqual(w.offer_template(n)["template_params"]["processed_params"]["body"]["1"], "сосед")


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

    def test_auto_testers(self):
        calls = []
        old = (w.AUTO_TESTERS, w.cw, w._spawn)
        w.AUTO_TESTERS, w._spawn = True, lambda fn, *a: fn(*a)
        w.cw = lambda method, path, body=None: (calls.append((method, path, body)), (200, {}))[1]
        try:
            w.intake(self.payload(id=110, sender={"id": 8, "name": "B", "type": "contact"}))
            w.intake(self.payload(id=111, sender={"id": 8, "name": "B", "type": "contact"}))
            w.intake(self.payload(id=112, sender={"id": 9, "name": "C", "type": "contact",
                                                  "custom_attributes": {w.TESTER_ATTR: True}}))
        finally:
            w.AUTO_TESTERS, w.cw, w._spawn = old
        self.assertEqual(calls, [("PATCH", "/contacts/8", {"custom_attributes": {w.TESTER_ATTR: True}})])


    def test_card_events(self):
        """The hooks for the orchestrator: a reply to our offer (once), the seller's handoff note, an opt-out."""
        got = []

        class Fake:
            def emit(self, type, contact_id, data=None, **kw):
                got.append((type, contact_id, (data or {}).get("service"), kw.get("once")))

        old = (w.EVENTS, w._spawn, w.cw)
        w.EVENTS, w._spawn = Fake(), (lambda fn, *a: None)
        w.cw = lambda method, path, body=None: (200, {})
        try:
            w.x("INSERT INTO needs (message_id, conv_id, inbox_id, contact_id, service, confidence, message_at, "
                "found_at, status, offer_conv_id) VALUES (900, 50, 2, 7, 'online_voting', 0.9, 0, 0, 'sent', 77)")
            dm = dict(group_type="individual")
            p = self.payload(id=120, **dm)
            p["conversation"]["id"] = 77
            self.assertEqual(w.intake(p), "ignored:individual:replied")
            p = self.payload(id=121, **dm)
            p["conversation"]["id"] = 78  # no offer of ours there
            self.assertEqual(w.intake(p), "ignored:individual")
            p = self.payload(id=122, private=True, message_type="outgoing", content="Председатель, Астана, 100 кв.",
                             sender={"id": 1, "type": "agent_bot"}, **dm)
            p["conversation"]["id"] = 77
            p["conversation"]["meta"] = {"sender": {"id": 7, "name": "A"}}
            self.assertEqual(w.intake(p), "lead")
            w.opt_out(7, 77, True)
        finally:
            w.EVENTS, w._spawn, w.cw = old
            w.x("DELETE FROM optout WHERE contact_id=7")
            w.x("DELETE FROM needs WHERE message_id=900")
        self.assertEqual(got, [("dm.replied", 7, "online_voting", "reply:77"),
                               ("lead.handoff", 7, "online_voting", "handoff:122"),
                               ("optout", 7, None, None)])

    def test_call_command(self):
        """/call: only a tester, not after «стоп», a known campaign, in hours, with a free line; then the endpoint."""
        calls, notes = [], []
        tester = {"id": 7, "phone_number": "+77000000000", "custom_attributes": {w.TESTER_ATTR: True}}
        state = {"contact": tester, "voice": {"active": 0, "max_calls": 2, "today": 0, "daily": 40}}

        def cw(method, path, body=None):
            calls.append((method, path, body))
            return (200, {"payload": state["contact"]}) if method == "GET" else (201, {"call_id": "x"})

        old = (w.cw, w.note, w._http, w.campaigns, w.quiet_for)
        w.cw, w.note = cw, (lambda conv, text: notes.append(text))
        w._http = lambda method, url, body=None, headers=None, timeout=30: (200, state["voice"])
        w.campaigns = lambda: {"votes": {"title": "сбор голосов"}}
        w.quiet_for = lambda now, tester: False
        dm = lambda text, **c: {"id": 500, "content": text, "conversation": {"id": 77, "group_type": "individual",
                                                                            "meta": {"sender": {"id": 7}}, **c}}
        try:
            self.assertEqual(w.call_command(dm("/call votes")), "placed")
            self.assertEqual(calls[-1], ("POST", "/conversations/77/voice_agent_call",
                                         {"campaign": "votes", "name": "", "key": "note-500"}))
            self.assertEqual(w.call_command(dm("/call")), "refused")
            self.assertIn("/call votes", notes[-1], "without a campaign the note lists them")
            self.assertEqual(w.call_command(dm("/call votes", group_type="group")), "refused:group")
            state["contact"] = {**tester, "custom_attributes": {}}
            self.assertEqual(w.call_command(dm("/call votes")), "refused")
            self.assertIn("тестировщикам", notes[-1])
            state["contact"] = {**tester, "custom_attributes": {w.TESTER_ATTR: True, w.OPTOUT_ATTR: True}}
            self.assertEqual(w.call_command(dm("/call votes")), "refused")
            self.assertIn("стоп", notes[-1])
            state["contact"], state["voice"] = tester, {"active": 2, "max_calls": 2, "today": 0, "daily": 40}
            self.assertEqual(w.call_command(dm("/call votes")), "refused")
            self.assertIn("заняты", notes[-1])
            w.quiet_for = lambda now, tester: True
            state["voice"] = {"active": 0, "max_calls": 2, "today": 0, "daily": 40}
            self.assertEqual(w.call_command(dm("/call votes")), "refused")
            self.assertIn("не звоним", notes[-1])
        finally:
            w.cw, w.note, w._http, w.campaigns, w.quiet_for = old
        self.assertEqual(sum(1 for c in calls if c[0] == "POST"), 1, "only the first one rang")

class GroupAnswers(unittest.TestCase):
    def setUp(self):
        w.x("DELETE FROM faq"); w.x("DELETE FROM group_answers"); w.x("DELETE FROM messages")
        self.calls, self.posts = [], []
        vecs = {"Сколько стоит онлайн-голосование?": [1, 0, 0], "А голосование онлайн почём?": [0.95, 0.3, 0],
                "Сколько стоит голосование в e-KNOT?": [0.97, 0.2, 0], "Где взять приложение e-KNOT?": [0, 1, 0],
                "На какой счёт скидываться на домофон?": [0, 0, 1], "Куда переводить деньги за домофон?": [0, 0.3, 0.95],
                "Кто знает, во сколько собрание?": [0.6, 0, 0.8]}

        def llm(path, body):
            self.calls.append(path)
            if path == "/embeddings":
                return 200, {"data": [{"embedding": vecs.get(body["input"][0], [0.3, 0.3, 0.3])}]}
            return 200, {"choices": [{"message": {"content": json.dumps(
                {"answerable": True, "stable": True, "lang": "ru", "answer": "Lite 12 000 тенге."})}}]}

        self.f = w.faqmod.Faq(q=w.q, x=w.x, llm=llm, cw=lambda m, p, b=None: (self.posts.append(b), (200, {}))[1],
                              catalog_path=w.CATALOG_PATH, catalog_roots=["голосов"], log=w.log)

    def row(self, i, text, conv=50, contact=None, author=None, store=False, ago=0):
        r = {"id": i, "conv_id": conv, "contact_id": contact or 100 + i, "author": author, "text": text,
             "created_at": time.time() - ago}
        if store:
            w.x("INSERT INTO messages (id, conv_id, inbox_id, contact_id, author, text, created_at) VALUES (?,?,?,?,?,?,?)",
                (r["id"], conv, 1, r["contact_id"], author, text, r["created_at"]))
        return r

    def test_thread_follow_up(self):
        """A reply to our answer continues the thread in the group; «спасибо» gets no answer; a cap per hour."""
        seen = []

        def llm(path, body):
            seen.append(body["messages"][1]["content"])
            last = body["messages"][1]["content"].rsplit("Жилец: ", 1)[-1]
            reply = not last.lower().startswith("спасибо")
            return 200, {"choices": [{"message": {"content": json.dumps(
                {"reply": reply, "lang": "ru", "answer": "Председатель подаёт заявку, дальше всё в приложении."})}}]}

        self.f.llm = llm
        chain = [("Жилец", "Хотим переизбрать председателя, как собрать собрание?"),
                 ("Помощник e-KNOT", "По закону нужно 10% собственников."), ("Жилец", "Как это работает?")]
        self.assertTrue(self.f.thread(self.row(119, "Как это работает?"), chain))
        self.assertEqual(self.posts[-1]["content_attributes"], {"in_reply_to": 119})
        self.assertIn("Помощник e-KNOT: По закону", seen[-1], "the model sees the whole thread")
        self.assertTrue(w.answered_in_group(119), "the need path skips a message answered in the thread")
        self.assertFalse(self.f.thread(self.row(121, "Спасибо!"), chain[:2] + [("Жилец", "Спасибо!")]))
        old = w.faqmod.THREAD_PER_HOUR
        w.faqmod.THREAD_PER_HOUR = 1
        try:
            self.assertFalse(self.f.thread(self.row(122, "А сколько стоит?"), chain), "the hourly cap")
        finally:
            w.faqmod.THREAD_PER_HOUR = old

    def test_reply_chain(self):
        me = w.my_user_id
        msgs = {"payload": [
            {"id": 116, "message_type": 0, "sender": {"type": "contact", "id": 7}, "content": "Как собрать собрание?"},
            {"id": 117, "message_type": 1, "sender": {"type": "user", "id": 2}, "content": "Нужно 10%.",
             "content_attributes": {"in_reply_to": 116}},
            {"id": 118, "message_type": 1, "private": True, "sender": {"type": "user", "id": 2}, "content": "💡 карточка"},
            {"id": 120, "message_type": 0, "sender": {"type": "contact", "id": 8}, "content": "У нас так же",
             "content_attributes": {"in_reply_to": 116}}]}
        old = w.cw
        w.cw, w.my_user_id = (lambda m, p, b=None: (200, msgs)), (lambda: 2)
        try:
            chain = w.reply_chain(50, 117)
            self.assertEqual([(m["id"], m["ours"]) for m in chain], [(116, False), (117, True)])
            self.assertFalse(w.reply_chain(50, 120)[-1]["ours"], "a reply to a neighbour is not our thread")
        finally:
            w.cw, w.my_user_id = old, me

    def test_candidates(self):
        c = lambda t: w.faqmod.is_candidate(t, ["голосов"])  # noqa: E731
        self.assertTrue(c("Сколько стоит онлайн-голосование?"))
        self.assertTrue(c("e-KNOT қосымшасы тегін бе"))
        self.assertFalse(c("Кто оставил машину у подъезда?"), "a question, not about e-KNOT")
        self.assertFalse(c("Голосование в субботу"), "about the topic, not a question")

    def test_cache_by_meaning(self):
        self.assertTrue(self.f.handle(self.row(1, "Сколько стоит онлайн-голосование?")))
        self.assertIn("/chat/completions", self.calls)
        self.calls.clear()
        self.assertTrue(self.f.handle(self.row(2, "А голосование онлайн почём?", conv=51)))
        self.assertEqual(self.calls, ["/embeddings"], "same meaning: from the cache, no model call")
        self.assertEqual(self.posts[-1]["content_attributes"], {"in_reply_to": 2})
        self.assertEqual(w.q("SELECT hits FROM faq")[0]["hits"], 1)

    def test_every_asker_answered(self):
        self.assertTrue(self.f.handle(self.row(1, "Сколько стоит онлайн-голосование?")))
        self.assertTrue(self.f.handle(self.row(2, "Сколько стоит голосование в e-KNOT?")),
                        "another person asking the same in a busy chat gets the answer too")
        self.assertEqual(self.posts[-1]["content_attributes"], {"in_reply_to": 2})
        self.assertFalse(self.f.handle(self.row(3, "А голосование онлайн почём?", contact=101)))
        self.assertEqual(w.q("SELECT source FROM group_answers WHERE message_id=3")[0]["source"],
                         "none:repeat_same_person")

    def test_learns_chat_answer(self):
        q1 = self.row(1, "На какой счёт скидываться на домофон?", store=True)
        self.assertFalse(self.f.handle(q1), "not about e-KNOT and nothing learned yet")
        ans = self.row(2, "Kaspi Gold 4400 4301 2222 1111, Айгуль Т.", author="Айгуль Т.", store=True)
        self.assertTrue(self.f.learn(ans, q1))
        self.posts.clear()
        self.assertTrue(self.f.handle(self.row(3, "Куда переводить деньги за домофон?", contact=300)))
        self.assertIn("Здесь уже отвечали (Айгуль)", self.posts[-1]["content"])
        self.assertIn("Kaspi Gold", self.posts[-1]["content"])
        self.assertFalse(self.f.handle(self.row(4, "Куда переводить деньги за домофон?", conv=51)),
                         "learned answers stay in their own group")

    def test_backfill_earlier_askers(self):
        q1 = self.row(1, "На какой счёт скидываться на домофон?", store=True, ago=600)
        self.f.handle(self.row(2, "Куда переводить деньги за домофон?", store=True, ago=300))
        self.f.handle(self.row(3, "Кто знает, во сколько собрание?", store=True, ago=200))
        self.posts.clear()
        self.f.learn(self.row(4, "Kaspi Gold 4400 4301 2222 1111", author="Айгуль", store=True), q1)
        self.assertEqual([p["content_attributes"]["in_reply_to"] for p in self.posts], [2],
                         "the other asker of the same question gets the answer; the different question does not")

    def test_not_learned(self):
        q1 = self.row(1, "На какой счёт скидываться на домофон?", store=True)
        self.assertFalse(self.f.learn(self.row(2, "+1", store=True), q1))
        self.assertFalse(self.f.learn(self.row(3, "Тоже", store=True), q1))
        self.assertFalse(self.f.learn(self.row(4, "не знаю", store=True), q1))
        self.assertFalse(self.f.learn(self.row(5, "Kaspi 4400", contact=101, store=True), q1), "the asker themself")
        self.assertFalse(self.f.learn(self.row(6, "А вы у кого спрашивали?", store=True), q1), "a question back")
        self.f.learn(self.row(7, "Kaspi 4400", author="Айгуль", store=True), q1)
        w.x("UPDATE faq SET author_contact_id=777 WHERE conv_id=50")
        self.f.forget_contact(777)
        self.assertEqual(w.q("SELECT COUNT(*) AS n FROM faq WHERE conv_id=50")[0]["n"], 0, "opt-out forgets it")

    def test_quoted_id(self):
        self.assertEqual(w.quoted_id({"content_attributes": {"in_reply_to": 42}}), 42)
        self.assertEqual(w.quoted_id({"content_attributes": json.dumps({"in_reply_to": "43"})}), 43)
        self.assertIsNone(w.quoted_id({"content_attributes": {}}))

    def test_stale_material_misses(self):
        self.f.handle(self.row(1, "Сколько стоит онлайн-голосование?"))
        w.x("UPDATE faq SET kb_version='old'")
        self.calls.clear()
        self.f.handle(self.row(2, "А голосование онлайн почём?", conv=51))
        self.assertIn("/chat/completions", self.calls, "a changed site retires the cached answer")


if __name__ == "__main__":
    unittest.main(verbosity=1)
