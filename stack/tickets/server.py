#!/usr/bin/env python3
"""Mock dispatcher (ticket) service for the sales-bot TEST stack. Python stdlib only, SQLite storage.

Stands in for a real ЖКХ dispatcher system: the agent creates tickets through an HTTP tool and gets back
a real ticket number, so it never promises "передам мастеру" without a record. TEST DATA ONLY.

HTTP (internal docker network only, Bearer TICKETS_API_TOKEN on every /v1 route):
  POST  /v1/tickets                 create (dedupes an open ticket: same conversation, category, address)
  GET   /v1/tickets?phone=&contact_id=   the caller's latest tickets (both come from conversation context)
  PATCH /v1/tickets/<number>        operator: {"status": new|in_progress|done|cancelled}
  POST  /v1/tickets/<number>/notes  add a resident's detail to THEIR ticket (404 when not theirs)
  POST  /v1/readings                meter reading (409 when lower than the previous one of the same meter)
  GET   /healthz                    no auth
CLI (operator, inside the container):
  python3 -I /app/server.py list [N]
  python3 -I /app/server.py set-status <number> <status>
  python3 -I /app/server.py readings [N]
  python3 -I /app/server.py notes <number>
Logs carry method, path and status only, never bodies (they hold addresses and phones).
"""
import hmac, json, os, re, sqlite3, sys, threading
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DB = os.environ.get("TICKETS_DB", "/data/tickets.db")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 16 * 1024

CATEGORIES = {
    "water": "Водоснабжение / протечка",
    "heating": "Отопление",
    "electricity": "Электричество",
    "intercom": "Домофон",
    "elevator": "Лифт",
    "cleaning": "Уборка / мусор",
    "other": "Другое",
}
URGENCY = {"emergency", "normal"}
STATUS = {
    "new": "принята, ожидает назначения мастера",
    "in_progress": "в работе у мастера",
    "done": "выполнена",
    "cancelled": "отменена",
}
OPEN = ("new", "in_progress")
# Fictional service levels of the test УК. The agent may quote them ONLY because they come from here.
SLA = {
    "emergency": "аварийная бригада выезжает в течение 2 часов",
    "normal": "мастер свяжется с вами в течение 1 рабочего дня",
}
METERS = {
    "cold_water": ("ХВС (холодная вода)", "м³"),
    "hot_water": ("ГВС (горячая вода)", "м³"),
    "electricity": ("Электроэнергия", "кВт·ч"),
    "electricity_day": ("Электроэнергия, день (T1)", "кВт·ч"),
    "electricity_night": ("Электроэнергия, ночь (T2)", "кВт·ч"),
    "gas": ("Газ", "м³"),
    "heat": ("Тепло", "Гкал"),
}
DIGITS = re.compile(r"^\d{1,20}$")
_lock = threading.Lock()


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            category TEXT NOT NULL,
            urgency TEXT NOT NULL,
            address TEXT NOT NULL,
            apartment TEXT NOT NULL,
            description TEXT NOT NULL,
            conversation_id TEXT,
            contact_phone TEXT,
            contact_id TEXT,
            contact_name TEXT)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(tickets)")}
        if "contact_id" not in cols:
            c.execute("ALTER TABLE tickets ADD COLUMN contact_id TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS t_phone ON tickets(contact_phone)")
        c.execute("CREATE INDEX IF NOT EXISTS t_conv ON tickets(conversation_id)")
        c.execute("CREATE INDEX IF NOT EXISTS t_contact ON tickets(contact_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT UNIQUE,
            created_at TEXT NOT NULL,
            meter_type TEXT NOT NULL,
            value TEXT NOT NULL,
            serial TEXT NOT NULL,
            address TEXT NOT NULL,
            apartment TEXT NOT NULL,
            conversation_id TEXT,
            contact_phone TEXT,
            contact_id TEXT,
            contact_name TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ticket_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES tickets(id),
            created_at TEXT NOT NULL,
            text TEXT NOT NULL,
            conversation_id TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS r_meter ON readings(meter_type, address, apartment, serial)")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def view(r, duplicate=False):
    return {
        "number": r["number"],
        "status": r["status"],
        "status_text": STATUS[r["status"]],
        "category": CATEGORIES[r["category"]],
        "urgency": r["urgency"],
        "address": r["address"],
        "apartment": r["apartment"],
        "created_at": r["created_at"],
        "sla_text": SLA[r["urgency"]] if r["status"] in OPEN else "",
        "duplicate": duplicate,
        # Instruction for the bot, not for the resident: an emergency always reaches a human too.
        "next_step": ("аварийная заявка: попроси жильца позвонить в аварийно-диспетчерскую службу и вызови "
                      "handoff_to_human") if r["urgency"] == "emergency" and r["status"] in OPEN else "",
    }


def norm(v):
    return "".join(ch for ch in v.lower() if ch.isalnum())


def text_field(body, key, lo, hi, required=True):
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{key} is required")
        return ""
    if not isinstance(v, (str, int)):
        raise ValueError(f"{key} must be a string")
    v = " ".join(str(v).split())
    if not lo <= len(v) <= hi:
        raise ValueError(f"{key} must be {lo}..{hi} characters")
    return v


def create(body):
    category = str(body.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
    urgency = str(body.get("urgency") or "normal").strip().lower()
    if urgency not in URGENCY:
        raise ValueError("urgency must be emergency or normal")
    address = text_field(body, "address", 3, 200)
    apartment = text_field(body, "apartment", 1, 20, required=False)
    description = text_field(body, "description", 3, 1000)
    conv = str(body.get("conversation_id") or "").strip()
    conv = conv if DIGITS.match(conv) else ""
    phone = text_field(body, "contact_phone", 3, 32, required=False)
    contact = str(body.get("contact_id") or "").strip()
    contact = contact if DIGITS.match(contact) else ""
    name = text_field(body, "contact_name", 1, 120, required=False)
    t = now()
    with _lock, db() as c:
        if conv:
            # A model may call the tool twice for one problem: hand back the open ticket instead. Same
            # conversation + category + address/apartment only, so a second problem gets its own ticket.
            since = (t - timedelta(hours=24)).isoformat()
            for dup in c.execute(
                    "SELECT * FROM tickets WHERE conversation_id=? AND category=? AND status IN (?,?) "
                    "AND created_at>=? ORDER BY id DESC", (conv, category, *OPEN, since)).fetchall():
                if norm(dup["address"]) != norm(address) or norm(dup["apartment"]) != norm(apartment):
                    continue
                if urgency == "emergency" and dup["urgency"] != "emergency":
                    # The problem escalated: the open ticket becomes an emergency (and gets its next_step).
                    c.execute("UPDATE tickets SET urgency='emergency', description=?, updated_at=? WHERE id=?",
                              (f"{dup['description']} | ЭСКАЛАЦИЯ: {description}"[:1000], t.isoformat(),
                               dup["id"]))
                    dup = c.execute("SELECT * FROM tickets WHERE id=?", (dup["id"],)).fetchone()
                return 200, view(dup, duplicate=True)
        cur = c.execute(
            "INSERT INTO tickets (created_at, updated_at, status, category, urgency, address, apartment, "
            "description, conversation_id, contact_phone, contact_id, contact_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.isoformat(), t.isoformat(), "new", category, urgency, address, apartment, description,
             conv or None, phone or None, contact or None, name or None))
        number = f"{t.year}-{cur.lastrowid:05d}"
        c.execute("UPDATE tickets SET number=? WHERE id=?", (number, cur.lastrowid))
        row = c.execute("SELECT * FROM tickets WHERE id=?", (cur.lastrowid,)).fetchone()
    return 201, view(row)


def parse_value(raw):
    v = str(raw if raw is not None else "").strip().replace(" ", "").replace(",", ".")
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError("value must be a number, e.g. 123.456")
    if not d.is_finite() or d < 0 or d >= Decimal(10) ** 9:
        raise ValueError("value must be between 0 and 999999999")
    return d.normalize() if d != d.to_integral() else d.quantize(Decimal(1))


def submit_reading(body):
    meter = str(body.get("meter_type", "")).strip().lower()
    if meter not in METERS:
        raise ValueError(f"meter_type must be one of {sorted(METERS)}")
    value = parse_value(body.get("value"))
    serial = text_field(body, "serial", 1, 32, required=False)
    address = text_field(body, "address", 3, 200)
    apartment = text_field(body, "apartment", 1, 20, required=False)
    conv = str(body.get("conversation_id") or "").strip()
    conv = conv if DIGITS.match(conv) else ""
    phone = text_field(body, "contact_phone", 3, 32, required=False)
    contact = str(body.get("contact_id") or "").strip()
    contact = contact if DIGITS.match(contact) else ""
    name = text_field(body, "contact_name", 1, 120, required=False)
    title, unit = METERS[meter]
    t = now()
    with _lock, db() as c:
        # The same meter: type + address/apartment (+ serial when both readings carry one).
        prev = None
        for r in c.execute("SELECT * FROM readings WHERE meter_type=? ORDER BY id DESC", (meter,)).fetchall():
            if norm(r["address"]) != norm(address) or norm(r["apartment"]) != norm(apartment):
                continue
            if serial and r["serial"] and norm(r["serial"]) != norm(serial):
                continue
            prev = r
            break
        out = {"meter": title, "unit": unit, "value": str(value), "serial": serial,
               "previous_value": prev["value"] if prev else "", "consumption": ""}
        if prev:
            pv = Decimal(prev["value"])
            if value < pv:
                return 409, dict(out, accepted=False, error=(
                    f"показание {value} меньше предыдущего {pv} {unit}: попроси жильца перепроверить цифры"))
            if value == pv and prev["created_at"] >= (t - timedelta(hours=24)).isoformat():
                return 200, dict(out, accepted=True, duplicate=True, number=prev["number"],
                                 accepted_at=prev["created_at"])
            out["consumption"] = str(value - pv)
        cur = c.execute(
            "INSERT INTO readings (created_at, meter_type, value, serial, address, apartment, conversation_id, "
            "contact_phone, contact_id, contact_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t.isoformat(), meter, str(value), serial, address, apartment, conv or None, phone or None,
             contact or None, name or None))
        number = f"П-{t.year}-{cur.lastrowid:05d}"
        c.execute("UPDATE readings SET number=? WHERE id=?", (number, cur.lastrowid))
    return 201, dict(out, accepted=True, duplicate=False, number=number, accepted_at=t.isoformat())


def add_note(number, body):
    text = text_field(body, "text", 3, 2000)
    conv = str(body.get("conversation_id") or "").strip()
    phone = str(body.get("contact_phone") or "").strip()
    contact = str(body.get("contact_id") or "").strip()
    with _lock, db() as c:
        r = c.execute("SELECT * FROM tickets WHERE number=?", (number,)).fetchone()
        # Only the resident who owns the ticket may add to it; anything else reads as "not found".
        owner = r is not None and (
            (conv and r["conversation_id"] == conv) or (len(phone) >= 3 and r["contact_phone"] == phone)
            or (contact and r["contact_id"] == contact))
        if not owner:
            return 404, {"error": "заявка с таким номером у этого жильца не найдена", "added": False}
        if r["status"] not in OPEN:
            return 409, {"error": f"заявка {number} уже {STATUS[r['status']]}, дополнить нельзя", "added": False}
        c.execute("INSERT INTO ticket_notes (ticket_id, created_at, text, conversation_id) VALUES (?,?,?,?)",
                  (r["id"], now().isoformat(), text, conv or None))
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now().isoformat(), r["id"]))
        n = c.execute("SELECT count(*) FROM ticket_notes WHERE ticket_id=?", (r["id"],)).fetchone()[0]
    return 201, {"added": True, "number": number, "notes_count": n, "status_text": STATUS[r["status"]]}


def list_mine(phone, contact):
    phone = (phone or "").strip()
    contact = (contact or "").strip()
    phone = phone if len(phone) >= 3 else ""
    contact = contact if DIGITS.match(contact) else ""
    if not phone and not contact:
        # No identity in context (e.g. the playground): say so instead of listing everything.
        return 200, {"tickets": [], "note": "жилец не определён (нет телефона в диалоге), заявки найти нельзя"}
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tickets WHERE (?<>'' AND contact_phone=?) OR (?<>'' AND contact_id=?) "
            "ORDER BY id DESC LIMIT 5", (phone, phone, contact, contact)).fetchall()
    return 200, {"tickets": [view(r) for r in rows],
                 "note": "" if rows else "заявок от этого номера не найдено"}


def set_status(number, status):
    if status not in STATUS:
        raise ValueError(f"status must be one of {sorted(STATUS)}")
    with _lock, db() as c:
        n = c.execute("UPDATE tickets SET status=?, updated_at=? WHERE number=?",
                      (status, now().isoformat(), number)).rowcount
        row = c.execute("SELECT * FROM tickets WHERE number=?", (number,)).fetchone()
    return (200, view(row)) if n else (404, {"error": "ticket not found"})


class Handler(BaseHTTPRequestHandler):
    server_version = "salesbot-tickets/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {urlsplit(self.path).path} {code}\n")

    def send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        token = os.environ.get("TICKETS_API_TOKEN", "")
        got = self.headers.get("Authorization", "")
        ok = bool(token) and hmac.compare_digest(got.encode(), f"Bearer {token}".encode())
        if not ok:
            self.send(401, {"error": "unauthorized"})
        return ok

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def route(self, method):
        u = urlsplit(self.path)
        if method == "GET" and u.path == "/healthz":
            return self.send(200, {"ok": True})
        if not u.path.startswith("/v1/"):
            return self.send(404, {"error": "not found"})
        if not self.authed():
            return
        try:
            if method == "POST" and u.path == "/v1/readings":
                return self.send(*submit_reading(self.body()))
            if method == "POST" and u.path == "/v1/tickets":
                return self.send(*create(self.body()))
            if method == "GET" and u.path == "/v1/tickets":
                q = parse_qs(u.query)
                return self.send(*list_mine(q.get("phone", [""])[0], q.get("contact_id", [""])[0]))
            m = re.fullmatch(r"/v1/tickets/(\d{4}-\d{5,})/notes", u.path)
            if method == "POST" and m:
                return self.send(*add_note(m.group(1), self.body()))
            m = re.fullmatch(r"/v1/tickets/(\d{4}-\d{5,})", u.path)
            if method == "PATCH" and m:
                return self.send(*set_status(m.group(1), str(self.body().get("status", ""))))
            return self.send(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as e:
            return self.send(400, {"error": str(e)})
        except sqlite3.Error as e:
            sys.stderr.write(f"db error: {type(e).__name__}\n")
            return self.send(500, {"error": "storage error"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")


def cli(argv):
    if argv[0] == "list":
        with db() as c:
            rows = c.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT ?",
                             (int(argv[1]) if len(argv) > 1 else 20,)).fetchall()
        for r in rows:
            print(f"{r['number']}  {r['status']:11} {r['urgency']:9} {CATEGORIES[r['category']]:26} "
                  f"{r['address']}, кв. {r['apartment'] or '-'} | {r['description'][:60]} | conv={r['conversation_id']}")
    elif argv[0] == "notes" and len(argv) == 2:
        with db() as c:
            for r in c.execute("SELECT n.* FROM ticket_notes n JOIN tickets t ON t.id=n.ticket_id WHERE t.number=? "
                               "ORDER BY n.id", (argv[1],)):
                print(f"{r['created_at']}  {r['text']}")
    elif argv[0] == "readings":
        with db() as c:
            rows = c.execute("SELECT * FROM readings ORDER BY id DESC LIMIT ?",
                             (int(argv[1]) if len(argv) > 1 else 20,)).fetchall()
        for r in rows:
            print(f"{r['number']}  {METERS[r['meter_type']][0]:28} {r['value']:>12} {METERS[r['meter_type']][1]:6} "
                  f"№{r['serial'] or '-':10} {r['address']}, кв. {r['apartment'] or '-'} | conv={r['conversation_id']}")
    elif argv[0] == "set-status" and len(argv) == 3:
        code, out = set_status(argv[1], argv[2])
        print(code, json.dumps(out, ensure_ascii=False))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        if not os.environ.get("TICKETS_API_TOKEN"):
            sys.exit("TICKETS_API_TOKEN is not set")
        sys.stderr.write(f"tickets: listening on :{PORT}\n")
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
