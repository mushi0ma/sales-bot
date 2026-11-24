#!/usr/bin/env python3
"""group-watcher: finds e-KNOT sales needs in WhatsApp groups and offers the matching service in a private message.

Chatwoot account webhook (message_created, HMAC-signed) -> this service:
  1. keeps the incoming messages of group conversations that carry the label WATCH_LABEL. Other groups are ignored:
     the operator allows a group by labelling it, as tg-userbot's `/salesbot allow`;
  2. every BATCH_SECONDS, per group: a cheap ru/kk keyword prefilter, then ONE Gemini Flash-Lite call (through
     LiteLLM) over the new messages with a few earlier ones as context. Authors go to the model as «A», «B», ...,
     never by name or phone. The answer is a JSON list of needs {message, service, confidence, quote};
  3. per need: a private note in the group conversation (the operator's card), then the send policy (opt-out, one
     offer per contact per OFFER_COOLDOWN_DAYS, quiet hours, daily cap, SEND_MODE) and path A: a private WhatsApp
     message to the author through Chatwoot, handed back to the agent with the «Вернуть боту» macro so the agent
     answers the reply.
  «стоп» / «тоқта» from a member, in the group or in private, opts them out and deletes their stored messages.
  SEND_MODE: auto (send), approve (the card asks for a private note `/offer N` or `/skip N`), dry (cards only).

Stdlib only. State: SQLite at DB_PATH. Logs carry ids, lengths and statuses, never message text or names.
CLI: watcher.py serve | stats | eval SAMPLES.json (classifier precision/recall on labelled sample messages)
"""
import hashlib, hmac, json, logging, os, re, sqlite3, statistics, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
CW_URL = E("CW_URL", "http://cw-rails:3000").rstrip("/")
CW_ACCOUNT_ID = E("CW_ACCOUNT_ID", "1")
CW_TOKEN = E("CW_API_TOKEN", "")
WEBHOOK_SECRET = E("WEBHOOK_SECRET", "")
LLM_URL = E("LLM_URL", "http://litellm:4000/v1").rstrip("/")
LLM_KEY = E("LLM_KEY", "")
LLM_MODEL = E("LLM_MODEL", "gemini-3.5-flash-lite")
DB_PATH = E("DB_PATH", "/data/watcher.db")
CATALOG_PATH = E("CATALOG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json"))
WATCH_LABEL = E("WATCH_LABEL", "salesbot-watch")
SEND_MODE = E("SEND_MODE", "approve")
BATCH_SECONDS = int(E("BATCH_SECONDS", "60"))
BATCH_MAX = int(E("BATCH_MAX", "20"))
CONTEXT_MESSAGES = int(E("CONTEXT_MESSAGES", "6"))
MIN_CONFIDENCE = float(E("MIN_CONFIDENCE", "0.7"))
OFFER_COOLDOWN_DAYS = int(E("OFFER_COOLDOWN_DAYS", "7"))
DAILY_SEND_CAP = int(E("DAILY_SEND_CAP", "20"))
QUIET_HOURS = E("QUIET_HOURS", "21-9")  # local hours [start, end): no sends
UTC_OFFSET = int(E("UTC_OFFSET", "5"))  # Kazakhstan
HOLD_MAX_HOURS = int(E("HOLD_MAX_HOURS", "14"))  # a need held for quiet hours expires after this
HANDBACK_MACRO = E("HANDBACK_MACRO", "🤖 Вернуть боту")
OPTOUT_ATTR = "salesbot_optout"
MAX_SKEW = 300

log = logging.getLogger("group-watcher")
STOP_RE = re.compile(r"^\s*(стоп|stop|тоқта|токта|отписаться|отписка)\s*[.!]*\s*$", re.I)
LOCAL = timezone(timedelta(hours=UTC_OFFSET))


def load_catalog(path=CATALOG_PATH):
    with open(path, encoding="utf-8") as f:
        services = json.load(f)["services"]
    return {s["id"]: s for s in services}


CATALOG = load_catalog()


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,            -- Chatwoot message id
  conv_id INTEGER NOT NULL,          -- Chatwoot conversation display id (the group thread)
  inbox_id INTEGER NOT NULL,
  group_name TEXT,
  contact_id INTEGER NOT NULL,       -- the author
  author TEXT,
  text TEXT NOT NULL,
  created_at REAL NOT NULL,
  state TEXT NOT NULL DEFAULT 'new'  -- new | done
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv_id, state, id);
CREATE TABLE IF NOT EXISTS needs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER UNIQUE NOT NULL,
  conv_id INTEGER NOT NULL, inbox_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
  author TEXT, group_name TEXT, service TEXT NOT NULL, confidence REAL NOT NULL, quote TEXT, lang TEXT,
  message_at REAL NOT NULL, found_at REAL NOT NULL,
  status TEXT NOT NULL,              -- sent | awaiting | held | skipped:<why> | failed:<why>
  path TEXT, offer_conv_id INTEGER, sent_at REAL
);
CREATE TABLE IF NOT EXISTS optout (contact_id INTEGER PRIMARY KEY, at REAL NOT NULL);
"""

_db_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA)
    return _db


def q(sql, args=()):
    with _db_lock:
        return db().execute(sql, args).fetchall()


def x(sql, args=()):
    with _db_lock:
        return db().execute(sql, args).lastrowid


# ---------------------------------------------------------------- pure logic (tested in test_watcher.py)

def verify_signature(secret, ts, sig, raw, now=None):
    """Chatwoot signs `{ts}.{body}` with HMAC-SHA256 (lib/webhooks/trigger.rb). No secret configured -> refuse all."""
    if not secret or not ts.isdigit() or abs((now or time.time()) - int(ts)) > MAX_SKEW:
        return False
    want = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig or "", want)


def is_stop(text):
    return bool(STOP_RE.match(text or ""))


def prefilter(text, catalog=CATALOG):
    """Services whose keyword roots occur in the text. Empty -> the message never reaches the model."""
    t = (text or "").lower().replace("ё", "е")
    if len(t.strip()) < 8:
        return []
    return [sid for sid, s in catalog.items() if any(r.replace("ё", "е") in t for r in s["roots"])]


def in_quiet_hours(now_utc, spec=QUIET_HOURS):
    start, end = (int(v) for v in spec.split("-"))
    h = now_utc.astimezone(LOCAL).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def local_midnight_ts(now_utc):
    d = now_utc.astimezone(LOCAL)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def decide(need, now_utc, *, mode, opted_out, last_offer_at, sent_today):
    """-> (status, why). Order matters: a person's refusal beats everything, the operator's mode comes last."""
    if opted_out:
        return "skipped", "optout"
    if need["confidence"] < MIN_CONFIDENCE:
        return "skipped", "low_confidence"
    if last_offer_at and now_utc.timestamp() - last_offer_at < OFFER_COOLDOWN_DAYS * 86400:
        return "skipped", "cooldown"
    if sent_today >= DAILY_SEND_CAP:
        return "skipped", "daily_cap"
    if mode == "dry":
        return "skipped", "dry_run"
    if mode == "approve":
        return "awaiting", ""
    if in_quiet_hours(now_utc):
        return "held", "quiet_hours"
    return "send", ""


def pseudonyms(rows):
    """contact_id -> «A», «B», ... in order of first appearance: the model never sees names or phones."""
    out = {}
    for r in rows:
        if r["contact_id"] not in out:
            n = len(out)
            out[r["contact_id"]] = chr(65 + n % 26) + (str(n // 26) if n >= 26 else "")
    return out


def build_prompt(context_rows, new_rows, catalog=CATALOG):
    names = pseudonyms(list(context_rows) + list(new_rows))
    services = "\n".join(f"- {sid}: {s['name']}. Что даёт: {s['what']} Признаки потребности: {s['signals']}"
                         for sid, s in catalog.items())
    system = (
        "Ты анализируешь переписку жильцов многоквартирного дома в Казахстане (русский или казахский язык) и находишь "
        "сообщения, где автор САМ описывает проблему или потребность, которую решает одна из услуг e-KNOT.\n"
        f"Услуги:\n{services}\n\n"
        "Правила:\n"
        "- Отмечай только сообщения с пометкой NEW. CONTEXT дан лишь для понимания (например, ответ «у нас тоже» на "
        "чужую жалобу — это потребность автора ответа).\n"
        "- Не отмечай: шутки, приветствия, благодарности, вопросы не по теме, случаи, когда услугой уже пользуются, "
        "советы другим без своей проблемы.\n"
        "- Одно сообщение — не больше одной услуги, самой подходящей.\n"
        "- confidence от 0 до 1: 0.9+ — явная проблема из признаков, 0.7 — вероятная, ниже 0.5 — не выдавай вовсе.\n"
        "- quote — дословный отрывок сообщения до 120 символов.\n"
        "- lang — ru, kk или other.\n"
        "Если подходящих сообщений нет, верни пустой список."
    )
    lines = [f"{r['id']} CONTEXT {names[r['contact_id']]}: {r['text']}" for r in context_rows]
    lines += [f"{r['id']} NEW {names[r['contact_id']]}: {r['text']}" for r in new_rows]
    return system, "\n".join(lines)


def response_schema(catalog=CATALOG):
    return {"type": "object", "required": ["needs"], "properties": {"needs": {"type": "array", "items": {
        "type": "object", "required": ["message_id", "service", "confidence", "quote", "lang"],
        "properties": {"message_id": {"type": "integer"}, "service": {"type": "string", "enum": list(catalog)},
                       "confidence": {"type": "number"}, "quote": {"type": "string"},
                       "lang": {"type": "string", "enum": ["ru", "kk", "other"]}}}}}}


def parse_needs(content, new_rows, catalog=CATALOG):
    """Validate the model's JSON: only NEW messages, known services, one need per message, a quote that is really
    in the message (else its first 120 characters)."""
    by_id = {r["id"]: r for r in new_rows}
    try:
        items = json.loads(content).get("needs") or []
    except (ValueError, AttributeError):
        return []
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            mid, conf = int(it.get("message_id")), float(it.get("confidence"))
        except (TypeError, ValueError):
            continue
        sid = it.get("service")
        if mid not in by_id or mid in seen or sid not in catalog:
            continue
        seen.add(mid)
        text = by_id[mid]["text"]
        quote = (it.get("quote") or "").strip().strip("«»\"")
        if not quote or quote not in text:
            quote = text[:120]
        out.append({"message_id": mid, "service": sid, "confidence": max(0.0, min(1.0, conf)),
                    "quote": quote[:120], "lang": it.get("lang") if it.get("lang") in ("ru", "kk") else "other"})
    return out


def offer_text(need, catalog=CATALOG):
    s = catalog[need["service"]]
    first = (need.get("author") or "").split()[0] if need.get("author") else ""
    hello = f"Здравствуйте, {first}!" if first and not first.lstrip("+").isdigit() else "Здравствуйте!"
    group = f" «{need['group_name']}»" if need.get("group_name") else ""
    return (f"{hello} Это тестовый бот e-KNOT. В группе{group} вы писали: «{need['quote']}».\n\n"
            f"{s['offer']}\nПодробнее: {s['url']}\n\n"
            "Если интересно, ответьте здесь, расскажу подробнее. Не хотите таких сообщений, напишите «стоп».")


# ---------------------------------------------------------------- HTTP clients

def _http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)[:200]


def cw(method, path, body=None):
    return _http(method, f"{CW_URL}/api/v1/accounts/{CW_ACCOUNT_ID}{path}", body, {"api_access_token": CW_TOKEN})


def classify(context_rows, new_rows):
    system, user = build_prompt(context_rows, new_rows)
    st, b = _http("POST", f"{LLM_URL}/chat/completions", {
        "model": LLM_MODEL, "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "needs", "schema": response_schema(), "strict": True}},
    }, {"Authorization": f"Bearer {LLM_KEY}"}, timeout=90)
    if st != 200:
        raise RuntimeError(f"LLM HTTP {st}: {str(b)[:200]}")
    return parse_needs(b["choices"][0]["message"]["content"] or "{}", new_rows)


# ---------------------------------------------------------------- Chatwoot actions

def note(conv_id, text):
    st, _ = cw("POST", f"/conversations/{conv_id}/messages", {"content": text, "private": True})
    if st not in (200, 201):
        log.warning("note conversation=%s HTTP %s (is the offers user a member of this inbox? bootstrap.py "
                    "group-watcher)", conv_id, st)
    return st


def card_text(need_id, need, status, why):
    s = CATALOG[need["service"]]
    decision = {
        "sent": "отправлено автору в личку (путь A)",
        "awaiting": f"ждёт одобрения: приватная заметка `/offer {need_id}` — отправить, `/skip {need_id}` — нет",
        "held": "отложено до конца тихих часов",
    }.get(status, f"не отправлено ({why or status})")
    return (f"💡 Потребность #{need_id}: {s['name']} (уверенность {need['confidence']:.2f})\n"
            f"Автор: {need.get('author') or need['contact_id']}\nЦитата: «{need['quote']}»\nРешение: {decision}")


def handback(conv_id):
    """Our message is a person's message to the agents service, so it steps aside (status open). Wait for that, then
    apply the operator's hand-back macro: the agent answers the resident's reply with the offer in its memory."""
    for _ in range(20):
        st, c = cw("GET", f"/conversations/{conv_id}")
        if st == 200 and c.get("status") == "open":
            break
        time.sleep(1)
    st, b = cw("GET", "/macros")
    macros = (b.get("payload", b) if isinstance(b, dict) else b) or []
    m = next((m for m in macros if isinstance(m, dict) and m.get("name") == HANDBACK_MACRO), None)
    if not m:
        log.warning("handback: macro missing (run bootstrap.py operator); conversation %s stays with operators", conv_id)
        return
    st, _ = cw("POST", f"/macros/{m['id']}/execute", {"conversation_ids": [conv_id]})
    log.info("handback conversation=%s macro HTTP %s", conv_id, st)


def send_offer(need_id):
    n = q("SELECT * FROM needs WHERE id=?", (need_id,))[0]
    st, b = cw("GET", f"/contacts/{n['contact_id']}/contactable_inboxes")
    entries = (b or {}).get("payload", []) if isinstance(b, dict) else []
    src = next((e.get("source_id") for e in entries if (e.get("inbox") or {}).get("id") == n["inbox_id"]), None)
    if not src:
        x("UPDATE needs SET status='failed:no_contact_inbox' WHERE id=?", (need_id,))
        log.warning("need=%s contact=%s: no contact inbox in inbox %s (HTTP %s)", need_id, n["contact_id"],
                    n["inbox_id"], st)
        return False
    st, c = cw("POST", "/conversations", {"inbox_id": n["inbox_id"], "contact_id": n["contact_id"], "source_id": src,
                                          "message": {"content": offer_text(dict(n))}})
    if st not in (200, 201) or not isinstance(c, dict) or not c.get("id"):
        x("UPDATE needs SET status=? WHERE id=?", (f"failed:http_{st}", need_id))
        log.warning("need=%s: create conversation HTTP %s", need_id, st)
        return False
    x("UPDATE needs SET status='sent', path='A', offer_conv_id=?, sent_at=? WHERE id=?", (c["id"], time.time(), need_id))
    log.info("need=%s sent: conversation=%s service=%s", need_id, c["id"], n["service"])
    threading.Thread(target=handback, args=(c["id"],), daemon=True).start()
    return True


def policy_inputs(contact_id, now_utc):
    opted = bool(q("SELECT 1 FROM optout WHERE contact_id=?", (contact_id,)))
    last = q("SELECT MAX(sent_at) AS t FROM needs WHERE contact_id=? AND status='sent'", (contact_id,))[0]["t"]
    today = q("SELECT COUNT(*) AS n FROM needs WHERE status='sent' AND sent_at>=?", (local_midnight_ts(now_utc),))[0]["n"]
    return {"opted_out": opted, "last_offer_at": last, "sent_today": today}


def handle_need(need, mode=None):
    now = datetime.now(timezone.utc)
    status, why = decide(need, now, mode=mode or SEND_MODE, **policy_inputs(need["contact_id"], now))
    stored = "held" if status == "held" else ("sent" if status == "send" else status)
    stored_full = f"{stored}:{why}" if stored == "skipped" else stored
    need_id = x("INSERT OR IGNORE INTO needs (message_id, conv_id, inbox_id, contact_id, author, group_name, service, "
                "confidence, quote, lang, message_at, found_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (need["message_id"], need["conv_id"], need["inbox_id"], need["contact_id"], need.get("author"),
                 need.get("group_name"), need["service"], need["confidence"], need["quote"], need["lang"],
                 need["message_at"], time.time(), "pending" if status == "send" else stored_full))
    if not need_id:
        return
    if need["confidence"] >= MIN_CONFIDENCE:
        note(need["conv_id"], card_text(need_id, need, stored, why))
    log.info("need=%s message=%s service=%s conf=%.2f -> %s%s", need_id, need["message_id"], need["service"],
             need["confidence"], status, f" ({why})" if why else "")
    if status == "send":
        send_offer(need_id)


def opt_out(contact_id, conv_id=None, individual=False):
    x("INSERT OR IGNORE INTO optout (contact_id, at) VALUES (?,?)", (contact_id, time.time()))
    x("DELETE FROM messages WHERE contact_id=?", (contact_id,))
    x("UPDATE needs SET status='skipped:optout', quote=NULL WHERE contact_id=? AND status IN ('awaiting','held')",
      (contact_id,))
    cw("PATCH", f"/contacts/{contact_id}", {"custom_attributes": {OPTOUT_ATTR: True}})
    if individual and conv_id:
        # A public reply from us also makes the agent step aside in this chat (a person answered).
        st, _ = cw("POST", f"/conversations/{conv_id}/messages", {
            "content": "Хорошо, больше не буду писать. Ваши сообщения из группы удалены из анализа."})
        if st not in (200, 201):
            log.warning("optout reply conversation=%s HTTP %s", conv_id, st)
    log.info("optout contact=%s", contact_id)


def operator_command(text):
    m = re.match(r"^\s*/(offer|skip)\s+#?(\d+)\s*$", text or "")
    if not m:
        return
    action, need_id = m.group(1), int(m.group(2))
    rows = q("SELECT * FROM needs WHERE id=?", (need_id,))
    if not rows or rows[0]["status"] not in ("awaiting", "held"):
        log.info("operator /%s %s: not awaiting", action, need_id)
        return
    if action == "skip":
        x("UPDATE needs SET status='skipped:operator' WHERE id=?", (need_id,))
        return
    n = rows[0]
    now = datetime.now(timezone.utc)
    inputs = policy_inputs(n["contact_id"], now)
    if inputs["opted_out"]:
        x("UPDATE needs SET status='skipped:optout' WHERE id=?", (need_id,))
        return
    x("UPDATE needs SET status='pending' WHERE id=?", (need_id,))
    send_offer(need_id)


# ---------------------------------------------------------------- webhook intake

def _ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def intake(p):
    """One Chatwoot message_created payload. Returns what was done, for tests and logs."""
    if p.get("event") != "message_created":
        return "ignored:event"
    conv = p.get("conversation") or {}
    sender = p.get("sender") or {}
    text = (p.get("content") or "").strip()
    if p.get("private") and p.get("message_type") == "outgoing" and sender.get("type") == "user":
        if text.startswith("/"):
            threading.Thread(target=operator_command, args=(text,), daemon=True).start()
            return "command"
        return "ignored:note"
    if p.get("message_type") != "incoming" or not sender.get("id") or sender.get("type") not in (None, "contact"):
        return "ignored:not_incoming"
    group = conv.get("group_type") == "group"
    if is_stop(text):
        threading.Thread(target=opt_out, args=(sender["id"], conv.get("id"), not group), daemon=True).start()
        return "optout"
    if not group:
        return "ignored:individual"
    if WATCH_LABEL not in (conv.get("labels") or []):
        return "ignored:group_not_allowed"
    if (sender.get("custom_attributes") or {}).get(OPTOUT_ATTR) or q("SELECT 1 FROM optout WHERE contact_id=?",
                                                                       (sender["id"],)):
        return "ignored:optout"
    if not text:
        return "ignored:empty"
    group_name = (((conv.get("meta") or {}).get("sender") or {}).get("name"))
    x("INSERT OR IGNORE INTO messages (id, conv_id, inbox_id, group_name, contact_id, author, text, created_at) "
      "VALUES (?,?,?,?,?,?,?,?)", (p["id"], conv["id"], conv.get("inbox_id") or (p.get("inbox") or {}).get("id"),
                                   group_name, sender["id"], sender.get("name"), text[:2000], _ts(p.get("created_at"))))
    return "stored"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(200, b'{"ok":true}')
        if self.path == "/stats":
            return self._reply(200, json.dumps(stats(), ensure_ascii=False).encode())
        self._reply(404)

    def do_POST(self):
        if self.path != "/chatwoot":
            return self._reply(404)
        raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2_000_000))
        if not verify_signature(WEBHOOK_SECRET, self.headers.get("X-Chatwoot-Timestamp", ""),
                                self.headers.get("X-Chatwoot-Signature", ""), raw):
            log.warning("webhook: bad signature")
            return self._reply(401)
        try:
            result = intake(json.loads(raw))
        except Exception:  # never echo payloads into logs: they hold residents' text
            log.exception("webhook: intake failed")
            return self._reply(200)
        if not result.startswith("ignored"):
            log.info("webhook: %s", result)
        self._reply(200)


# ---------------------------------------------------------------- batch loop

def process_group(conv_id):
    new = q("SELECT * FROM messages WHERE conv_id=? AND state='new' ORDER BY id LIMIT ?", (conv_id, BATCH_MAX))
    if not new:
        return
    ids = [r["id"] for r in new]
    candidates = [r for r in new if prefilter(r["text"])]
    needs = []
    if candidates:
        context = q("SELECT * FROM messages WHERE conv_id=? AND id<? ORDER BY id DESC LIMIT ?",
                    (conv_id, ids[0], CONTEXT_MESSAGES))[::-1]
        needs = classify(context, new)  # all new messages go in: a reply may only make sense next to a candidate
        log.info("group=%s batch=%s candidates=%s needs=%s", conv_id, len(new), len(candidates), len(needs))
    by_id = {r["id"]: r for r in new}
    for n in needs:
        r = by_id[n["message_id"]]
        handle_need({**n, "conv_id": conv_id, "inbox_id": r["inbox_id"], "contact_id": r["contact_id"],
                     "author": r["author"], "group_name": r["group_name"], "message_at": r["created_at"]})
    with _db_lock:
        db().execute(f"UPDATE messages SET state='done' WHERE id IN ({','.join('?' * len(ids))})", ids)


def release_held():
    now = datetime.now(timezone.utc)
    if in_quiet_hours(now):
        return
    for n in q("SELECT id, contact_id, found_at FROM needs WHERE status='held'"):
        if time.time() - n["found_at"] > HOLD_MAX_HOURS * 3600:
            x("UPDATE needs SET status='skipped:expired' WHERE id=?", (n["id"],))
            continue
        inputs = policy_inputs(n["contact_id"], now)
        if inputs["opted_out"] or inputs["sent_today"] >= DAILY_SEND_CAP or (
                inputs["last_offer_at"] and time.time() - inputs["last_offer_at"] < OFFER_COOLDOWN_DAYS * 86400):
            x("UPDATE needs SET status='skipped:policy_on_release' WHERE id=?", (n["id"],))
            continue
        x("UPDATE needs SET status='pending' WHERE id=?", (n["id"],))
        send_offer(n["id"])


def loop():
    while True:
        time.sleep(BATCH_SECONDS)
        try:
            for r in q("SELECT DISTINCT conv_id FROM messages WHERE state='new'"):
                try:
                    process_group(r["conv_id"])
                except Exception as e:  # keep the batch for the next tick (LLM down, 429 from the gateway)
                    log.warning("group=%s: %s", r["conv_id"], str(e)[:200])
            release_held()
        except Exception:
            log.exception("loop")


# ---------------------------------------------------------------- metrics and offline evaluation

def stats():
    by_status = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM needs GROUP BY status")}
    by_service = {r["service"]: r["n"] for r in q("SELECT service, COUNT(*) AS n FROM needs GROUP BY service")}
    lat = [r["d"] for r in q("SELECT sent_at - message_at AS d FROM needs WHERE status='sent'")]
    return {"messages_stored": q("SELECT COUNT(*) AS n FROM messages")[0]["n"], "needs_by_status": by_status,
            "needs_by_service": by_service, "optouts": q("SELECT COUNT(*) AS n FROM optout")[0]["n"],
            "median_seconds_message_to_offer": round(statistics.median(lat)) if lat else None,
            "send_mode": SEND_MODE}


def evaluate(path):
    """Samples: [{"id": 1, "author": "A", "text": "...", "expect": "service id" | null}, ...]. One batch per 20."""
    samples = json.load(open(path, encoding="utf-8"))
    rows = [{"id": s["id"], "contact_id": hash(s.get("author", s["id"])), "text": s["text"]} for s in samples]
    got = {}
    for i in range(0, len(rows), BATCH_MAX):
        chunk = rows[i:i + BATCH_MAX]
        if any(prefilter(r["text"]) for r in chunk):
            got.update({n["message_id"]: n for n in classify([], chunk) if n["confidence"] >= MIN_CONFIDENCE})
    tp = fp = fn = 0
    for s in samples:
        g = got.get(s["id"])
        ok = (g and g["service"]) == s.get("expect")
        tp += bool(ok and s.get("expect"))
        fp += bool(g and not ok)
        fn += bool(s.get("expect") and not ok)
        mark = "ok  " if ok or (not g and not s.get("expect")) else "MISS"
        print(f"{mark} #{s['id']:<3} expect={s.get('expect')} got={g and g['service']} "
              f"conf={g and round(g['confidence'], 2)} | {s['text'][:70]}")
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    print(f"\nprecision {p:.2f}  recall {r:.2f}  (tp {tp}, fp {fp}, fn {fn}, n {len(samples)}, model {LLM_MODEL})")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif cmd == "eval" and len(sys.argv) == 3:
        evaluate(sys.argv[2])
    elif cmd == "serve":
        if not (CW_TOKEN and WEBHOOK_SECRET and LLM_KEY):
            log.warning("CW_API_TOKEN / WEBHOOK_SECRET / LLM_KEY missing: run bootstrap.py group-watcher; "
                        "webhooks are refused until then")
        if SEND_MODE not in ("auto", "approve", "dry"):
            sys.exit(f"SEND_MODE must be auto, approve or dry, not {SEND_MODE!r}")
        db()
        threading.Thread(target=loop, daemon=True).start()
        log.info("serving :8080 mode=%s label=%s model=%s", SEND_MODE, WATCH_LABEL, LLM_MODEL)
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
