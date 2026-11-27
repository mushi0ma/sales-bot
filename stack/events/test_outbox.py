#!/usr/bin/env python3
"""python3 -I stack/events/test_outbox.py: queue, once, delivery order, retry and drop, against a local server."""
import json, os, pathlib, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from outbox import Outbox  # noqa: E402

GOT, MODE = [], {"code": 202}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        code = MODE["code"]
        if code == 202:
            GOT.append(body)
        self.send_response(code)
        self.end_headers()


class OutboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def box(self):
        d = tempfile.mkdtemp()
        return Outbox(os.path.join(d, "o.db"), "test", url=f"http://127.0.0.1:{self.srv.server_port}", token="t")

    def setUp(self):
        GOT.clear()
        MODE["code"] = 202

    def test_order_and_shape(self):
        b = self.box()
        b.emit("need.detected", 5, {"service": "x"}, name="A", house="H")
        b.emit("offer.sent", 5, {"service": "x"})
        self.assertEqual(b.flush_once(), 2)
        self.assertEqual([e["type"] for e in GOT], ["need.detected", "offer.sent"])
        self.assertEqual(GOT[0]["key"], "cw:5")
        self.assertEqual(GOT[0]["resident"], {"cw_contact_id": 5, "name": "A", "phone": None})
        self.assertEqual(b.pending(), 0)

    def test_once(self):
        b = self.box()
        self.assertIsNotNone(b.emit("dm.replied", 5, once="reply:9"))
        self.assertIsNone(b.emit("dm.replied", 5, once="reply:9"))
        self.assertEqual(b.pending(), 1)

    def test_down_keeps_then_delivers(self):
        b = self.box()
        b.emit("optout", 5)
        MODE["code"] = 503
        self.assertEqual(b.flush_once(), 0)
        self.assertEqual(b.pending(), 1)
        MODE["code"] = 202
        self.assertEqual(b.flush_once(), 1)

    def test_refused_is_dropped(self):
        b = self.box()
        b.emit("bad", 5)
        b.emit("optout", 5)
        MODE["code"] = 422
        b.flush_once()
        self.assertEqual(b.pending(), 0)

    def test_disabled_without_token_or_contact(self):
        b = self.box()
        self.assertIsNone(b.emit("optout", None))
        b.token = ""
        self.assertIsNone(b.emit("optout", 5))


if __name__ == "__main__":
    unittest.main(verbosity=1)
