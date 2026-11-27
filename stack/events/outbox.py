"""Events from a service to the orchestrator, with an outbox: emit() writes the event to a local SQLite table at
once, a thread delivers it to POST {ORCH_URL}/events and deletes it on 202. If the orchestrator or Hatchet is down,
events wait and go out in order when it is back; the service itself never blocks or fails on it. Stdlib only.

    from outbox import Outbox
    events = Outbox("/data/outbox.db", source="group-watcher")   # ORCH_URL, ORCH_TOKEN from the environment
    events.emit("need.detected", contact_id=12, name="Айгуль", phone=None, house="ЖК Тестовый",
                data={"service": "online_voting", "quote": "..."}, once="need:41")

`once` makes an event idempotent at the source: a second emit with the same value is dropped (kept 30 days).
The event contract is in orchestrator/cards.py.
"""
import json, logging, os, sqlite3, threading, time, urllib.error, urllib.request, uuid

log = logging.getLogger("outbox")
KEEP_ONCE_DAYS = 30
DROP_CODES = (400, 404, 413, 422)  # the orchestrator will never take it: dropped with a warning, not retried


class Outbox:
    def __init__(self, path, source, url=None, token=None, interval=3.0):
        self.source = source
        self.url = (url or os.environ.get("ORCH_URL", "http://orchestrator:8080")).rstrip("/") + "/events"
        self.token = token if token is not None else os.environ.get("ORCH_TOKEN", "")
        self.interval = interval
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL,
              created REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS once (key TEXT PRIMARY KEY, at REAL NOT NULL);""")
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None

    @property
    def enabled(self):
        return bool(self.token)

    def emit(self, type, contact_id, data=None, name=None, phone=None, house=None, once=None, at=None):
        """Queue one event. Returns its id, or None when skipped (disabled, no contact, or `once` seen)."""
        if not self.enabled or not contact_id:
            return None
        ev = {"v": 1, "id": str(uuid.uuid4()), "type": type, "at": at or time.time(), "source": self.source,
              "key": f"cw:{int(contact_id)}",
              "resident": {"cw_contact_id": int(contact_id), "name": name or None, "phone": phone or None},
              "house": house or None, "data": data or {}}
        with self.lock:
            if once:
                if self.db.execute("SELECT 1 FROM once WHERE key=?", (once,)).fetchone():
                    return None
                self.db.execute("INSERT INTO once VALUES (?,?)", (once, time.time()))
            self.db.execute("INSERT INTO outbox (body, created) VALUES (?,?)",
                            (json.dumps(ev, ensure_ascii=False), time.time()))
        self.wake.set()
        return ev["id"]

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="outbox", daemon=True)
            self.thread.start()
        return self

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def flush_once(self):
        """Deliver what is queued, oldest first; stop at the first transient failure. Returns events sent."""
        sent = 0
        while True:
            with self.lock:
                row = self.db.execute("SELECT seq, body, created FROM outbox ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return sent
            seq, body, _ = row
            code, err = self._post(body)
            with self.lock:
                if code == 202:
                    self.db.execute("DELETE FROM outbox WHERE seq=?", (seq,))
                    sent += 1
                    continue
                self.db.execute("UPDATE outbox SET tries=tries+1, last_error=? WHERE seq=?", (err[:300], seq))
                if code in DROP_CODES:
                    self.db.execute("DELETE FROM outbox WHERE seq=?", (seq,))
                    log.warning("outbox: dropped event %s: HTTP %s %s", seq, code, err[:120])
                    continue
            return sent

    def _post(self, body):
        req = urllib.request.Request(self.url, data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300].decode(errors="replace")
        except Exception as e:  # down, DNS, timeout: try again later
            return 0, str(e)

    def _run(self):
        backoff = self.interval
        last_gc = 0
        while True:
            self.wake.wait(backoff)
            self.wake.clear()
            try:
                self.flush_once()
                backoff = self.interval if not self.pending() else min(backoff * 2, 120)
                if time.time() - last_gc > 3600:
                    with self.lock:
                        self.db.execute("DELETE FROM once WHERE at < ?", (time.time() - KEEP_ONCE_DAYS * 86400,))
                    last_gc = time.time()
            except Exception as e:
                log.warning("outbox: %s", str(e)[:200])
                backoff = min(backoff * 2, 120)
