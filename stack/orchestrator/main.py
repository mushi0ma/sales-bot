"""orchestrator: the event bus between the roles (Hatchet) and the roles that run on it.

    producers (group-watcher, voice, ...) --HTTP POST /events--> ingress --Hatchet event--> tasks, each a role:
      card   Twenty: the resident's card, the opportunity's stage by the funnel (funnel.py), the timeline
    (next roles subscribe to the same events: advisor hints for the operator, monitoring, analyst tools)

Ingress answers 202 only once Hatchet has the event, so a producer's outbox (events/outbox.py) keeps an event until
then. Hatchet retries a failed task (3 times, backoff) and shows every run in its dashboard. A task is idempotent by
the event id (processed.db), and the tasks of one resident run one at a time (concurrency key = the event's key).
"""
import hmac, json, logging, os, sqlite3, threading, time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy, Context, Hatchet
from pydantic import BaseModel, Field

import cards

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orchestrator")
TOKEN = os.environ.get("ORCH_TOKEN", "")
TYPES = ["need.detected", "offer.sent", "dm.replied", "lead.qualified", "lead.handoff", "optout", "call.ended"]
DB = sqlite3.connect(os.environ.get("DB_PATH", "/data/processed.db"), check_same_thread=False, isolation_level=None)
DB.execute("CREATE TABLE IF NOT EXISTS done (event_id TEXT NOT NULL, role TEXT NOT NULL, at REAL NOT NULL, "
           "PRIMARY KEY (event_id, role))")
LOCK = threading.Lock()

hatchet = Hatchet()


class Resident(BaseModel):
    cw_contact_id: int
    name: str | None = None
    phone: str | None = None


class Event(BaseModel):
    v: int = 1
    id: str = Field(min_length=8, max_length=64)
    type: str
    at: float
    source: str
    key: str
    resident: Resident
    house: str | None = None
    data: dict = {}


def done(event_id, role):
    with LOCK:
        return DB.execute("SELECT 1 FROM done WHERE event_id=? AND role=?", (event_id, role)).fetchone() is not None


def mark(event_id, role):
    with LOCK:
        DB.execute("INSERT OR IGNORE INTO done VALUES (?,?,?)", (event_id, role, time.time()))


@hatchet.task(name="card", on_events=TYPES, input_validator=Event, retries=3, backoff_factor=4,
              backoff_max_seconds=120, execution_timeout=timedelta(seconds=90),
              concurrency=ConcurrencyExpression(expression="input.key", max_runs=1,
                                                limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN))
def card(ev: Event, ctx: Context) -> dict:
    if done(ev.id, "card"):
        return {"skipped": "already applied"}
    out = cards.apply(ev.model_dump())
    mark(ev.id, "card")
    log.info("card: %s %s -> %s", ev.type, ev.key, out["stage"])
    return out


class Ingress(BaseHTTPRequestHandler):
    def log_message(self, *a):  # no access log: the path is fixed, bodies carry personal data
        pass

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, {"ok": True}) if self.path == "/healthz" else self.reply(404, {})

    def do_POST(self):
        if self.path != "/events":
            return self.reply(404, {})
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
            return self.reply(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            ev = Event.model_validate_json(self.rfile.read(min(n, 256 * 1024)))
        except Exception as e:
            return self.reply(400, {"error": str(e)[:300]})
        if ev.type not in TYPES:
            return self.reply(422, {"error": f"unknown type {ev.type}"})
        try:
            hatchet.event.push(ev.type, ev.model_dump(), additional_metadata={"event_id": ev.id, "source": ev.source})
        except Exception as e:
            log.warning("push %s failed: %s", ev.type, str(e)[:200])
            return self.reply(503, {"error": "event bus unavailable"})
        self.reply(202, {"accepted": ev.id})


def main():
    if not (TOKEN and cards.TWENTY_KEY):
        raise SystemExit("ORCH_TOKEN and TWENTY_API_KEY are required")
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), Ingress)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("ingress on :8080, roles: card")
    hatchet.worker("orchestrator", slots=4, workflows=[card]).start()


if __name__ == "__main__":
    main()
