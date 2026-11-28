#!/usr/bin/env python3
"""group-watcher: finds e-KNOT sales needs in WhatsApp and Telegram groups and offers the matching service in a private
message. Telegram groups reach Chatwoot through tg-userbot as group conversations of its API inbox (TG_INBOX_ID),
so everything below holds for them too; the private offer goes through that inbox, as a Telegram message.

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
  A question to the chat about e-KNOT (price, tariff, how a service works, Аудитор ЖКХ) is answered in the group
  itself, under the question, at once (faq.py: cached answers by meaning, else the knowledge material); no private
  offer is sent for a message answered there. What the group answers itself (a member's WhatsApp reply to a
  question) is remembered for that group: everyone who asks the same later, and those who asked it in the last
  hours, get that answer quoted with its author. Every asker gets a reply, not only the first.
  «стоп» / «тоқта» from a member, in the group or in private, opts them out and deletes their stored messages.
  SEND_MODE: auto (send), approve (the card asks for a private note `/offer N` or `/skip N`), dry (cards only).

Stdlib only. State: SQLite at DB_PATH. Logs carry ids, lengths and statuses, never message text or names.
CLI: watcher.py serve | stats | eval SAMPLES.json (classifier precision/recall on labelled sample messages)
     | forget CONTACT_ID (tests: drop the contact's opt-out, offers and cooldown, untick the Chatwoot attribute)
     | report [DAYS] (metrics for the daily report over the last DAYS days, default 1: funnel from group message to
       lead, delivery and read status, replies within 24 h, opt-outs; reads offer conversations from Chatwoot)
     | faq (cached group answers) | faq-ask "QUESTION" (what the group would get; caches, posts nothing)
     | faq-off ID (retire a cached answer)
     | events-backfill (replay needs, offers, replies, handoffs, opt-outs into the cards in Twenty; idempotent)
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
BATCH_SECONDS = int(E("BATCH_SECONDS", "20"))
BATCH_MAX = int(E("BATCH_MAX", "20"))
CONTEXT_MESSAGES = int(E("CONTEXT_MESSAGES", "6"))
MIN_CONFIDENCE = float(E("MIN_CONFIDENCE", "0.7"))
OFFER_COOLDOWN_DAYS = int(E("OFFER_COOLDOWN_DAYS", "7"))
DAILY_SEND_CAP = int(E("DAILY_SEND_CAP", "20"))
QUIET_HOURS = E("QUIET_HOURS", "21-9")  # local hours [start, end): no sends
# Testers (contacts with TESTER_ATTR) test in the evening and again and again: a longer day and a short cooldown.
# Real residents keep QUIET_HOURS and OFFER_COOLDOWN_DAYS.
TESTER_QUIET_HOURS = E("TESTER_QUIET_HOURS", "23-8")
TESTER_COOLDOWN_MINUTES = int(E("TESTER_COOLDOWN_MINUTES", "30"))
# `/call CAMPAIGN` in a private note of a person's conversation: the voice agent rings them (campaigns.json of voice).
VOICE_URL = E("VOICE_URL", "http://voice:8090").rstrip("/")
CAMPAIGNS_PATH = E("CAMPAIGNS_PATH", "/app/campaigns.json")
UTC_OFFSET = int(E("UTC_OFFSET", "5"))  # Kazakhstan
HOLD_MAX_HOURS = int(E("HOLD_MAX_HOURS", "14"))  # a need held for quiet hours expires after this
HANDBACK_MACRO = E("HANDBACK_MACRO", "🤖 Вернуть боту")
OPTOUT_ATTR = "salesbot_optout"
# The agents service does not take our offer into the seller's memory (it was posted before the bot owned the
# conversation), and its memory is per contact, so an older chat can leak in. The offer travels as a conversation
# attribute the seller's prompt shows (bootstrap.py: attributeContext), which pins the topic of this conversation.
OFFER_ATTR = "salesbot_offer"
# Path A: WhatsApp Web session of the group's number (whatsmeow inbox). Path B: official WhatsApp Cloud API inbox,
# first message as an approved Meta template. SPLIT sends B to odd contact ids and A to even ones (stable per
# person) for the A/B comparison the mentor asked for. Without CLOUD_INBOX_ID everything goes by A.
SEND_PATH = E("SEND_PATH", "A").upper()
CLOUD_INBOX_ID = int(E("CLOUD_INBOX_ID", "0") or 0)
# Telegram groups come through tg-userbot's API inbox (group conversations like WhatsApp's). The offer goes to the
# author's private chat in that same inbox: paths A/B are WhatsApp's. Events say channel "telegram" for the cards.
TG_INBOX_ID = int(E("TG_INBOX_ID", "0") or 0)
TEMPLATES = {"ru": E("TEMPLATE_RU", "eknot_offer_ru"), "kk": E("TEMPLATE_KK", "eknot_offer_kk")}
# What the approved templates say, {{1}} name, {{2}} service, {{3}} link (texts submitted to Meta: docs/SETUP.md).
TEMPLATE_TEXT = {
    "ru": "Здравствуйте, {1}! Это помощник e-KNOT. Вы писали в чате дома о проблеме, и у нас есть подходящий "
          "сервис: {2}. Подробнее: {3}. Если интересно, ответьте на это сообщение. Чтобы больше не получать "
          "сообщения, ответьте «стоп».",
    "kk": "Сәлеметсіз бе, {1}! Бұл e-KNOT көмекшісі. Сіз үй чатында мәселе туралы жаздыңыз, бізде лайықты қызмет "
          "бар: {2}. Толығырақ: {3}. Қызықты болса, осы хабарламаға жауап беріңіз. Хабарламалар алмау үшін «стоп» "
          "деп жазыңыз.",
}
TESTER_ATTR = "salesbot_tester"
# Test environment: whoever writes in a watched group (whose description says a test bot reads it) becomes a tester,
# so the seller answers them in private without an operator ticking each classmate by hand. Off outside tests.
AUTO_TESTERS = E("AUTO_TESTERS", "false").lower() == "true"
MAX_SKEW = 300

log = logging.getLogger("group-watcher")
STOP_RE = re.compile(r"^\s*(стоп|stop|тоқта|токта|отписаться|отписка)\s*[.!]*\s*$", re.I)
LOCAL = timezone(timedelta(hours=UTC_OFFSET))


def load_catalog(path=CATALOG_PATH):
    with open(path, encoding="utf-8") as f:
        services = json.load(f)["services"]
    return {s["id"]: s for s in services}


CATALOG = load_catalog()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script's folder off sys.path
import faq as faqmod  # noqa: E402

# Events to the orchestrator (the resident's card and funnel in Twenty): need.detected, offer.sent, dm.replied,
# lead.handoff, optout. Set in `serve` (events/outbox.py, mounted next to this file); None in tests and CLI.
EVENTS = None


def emit(type, contact_id, data=None, **kw):
    if EVENTS is not None:
        try:
            EVENTS.emit(type, contact_id, data, **kw)
        except Exception as e:  # the card is a side channel: never break the group flow for it
            log.warning("event %s: %s", type, str(e)[:160])


def service_name(service, catalog=None):
    return ((catalog or CATALOG).get(service) or {}).get("name") or service


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
CREATE TABLE IF NOT EXISTS testers (contact_id INTEGER PRIMARY KEY, at REAL NOT NULL);  -- TESTER_ATTR seen or set
"""

_db_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA + faqmod.SCHEMA)
        faqmod.Faq.migrate(_db)
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


def cooldown_secs(tester):
    return TESTER_COOLDOWN_MINUTES * 60 if tester else OFFER_COOLDOWN_DAYS * 86400


def quiet_for(now_utc, tester):
    return in_quiet_hours(now_utc, TESTER_QUIET_HOURS if tester else QUIET_HOURS)


def decide(need, now_utc, *, mode, opted_out, last_offer_at, sent_today, tester=False):
    """-> (status, why). Order matters: a person's refusal beats everything, the operator's mode comes last."""
    if opted_out:
        return "skipped", "optout"
    if need["confidence"] < MIN_CONFIDENCE:
        return "skipped", "low_confidence"
    if last_offer_at and now_utc.timestamp() - last_offer_at < cooldown_secs(tester):
        return "skipped", "cooldown"
    if sent_today >= DAILY_SEND_CAP:
        return "skipped", "daily_cap"
    if mode == "dry":
        return "skipped", "dry_run"
    if mode == "approve":
        return "awaiting", ""
    if quiet_for(now_utc, tester):
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
        "- Не отмечай аварии, где сейчас опасность для людей (газ, пожар, человек застрял, искрит проводка): там "
        "нужна экстренная служба, а не предложение услуги. Обычная поломка или протечка без угрозы — отмечай.\n"
        "- Вывоз мусора и уборка двора — работа акимата и подрядчика, а не услуг e-KNOT: не отмечай.\n"
        "- Потребность бывает и в рабочем вопросе совета или председателя: лист присутствия и запись собрания — "
        "online_meetings, квитанция после оплаты и оплата картой — payments, ручное разнесение платежей в 1С или "
        "бухгалтерии — accounting, статус поданной заявки — dispatcher, жиналысты өзіміз бастау (созвать самим) — "
        "convening_assembly.\n"
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


def channel_of(inbox_id):
    """Event data naming the channel when it is not WhatsApp (funnel.channel_for)."""
    return {"channel": "telegram"} if TG_INBOX_ID and inbox_id == TG_INBOX_ID else {}


def choose_path(contact_id, mode=None, cloud_inbox=None, inbox_id=None):
    mode = SEND_PATH if mode is None else mode
    cloud_inbox = CLOUD_INBOX_ID if cloud_inbox is None else cloud_inbox
    if not cloud_inbox or mode not in ("B", "SPLIT") or channel_of(inbox_id):
        return "A"
    return "B" if mode == "B" or contact_id % 2 else "A"


def first_name(need):
    """A greeting name: the first word of the author's name when it is a name (letters, a hyphen), not a number
    or a nickname like «whoisthat?» (Telegram display names often are)."""
    first = (need.get("author") or "").split()[0] if need.get("author") else ""
    return first if re.fullmatch(r"[^\W\d_]{2,}(-[^\W\d_]+)?", first) else ""


def offer_template(need, catalog=CATALOG):
    """Chatwoot message body for path B: the template call and, as content, the text the resident will see."""
    s = catalog[need["service"]]
    lang = "kk" if need.get("lang") == "kk" else "ru"
    args = [first_name(need) or ("көрші" if lang == "kk" else "сосед"), s["name"], s["url"]]
    return {"content": TEMPLATE_TEXT[lang].format(None, *args),
            "template_params": {"name": TEMPLATES[lang], "language": lang, "category": "MARKETING",
                                "processed_params": {"body": {str(i + 1): a for i, a in enumerate(args)}}}}


def offer_text(need, catalog=CATALOG):
    s = catalog[need["service"]]
    first = first_name(need)
    hello = f"Здравствуйте, {first}!" if first else "Здравствуйте!"
    group = f" «{need['group_name']}»" if need.get("group_name") else ""
    return (f"{hello} Это помощник e-KNOT. Вы писали в группе{group}: «{need['quote']}».\n\n"
            f"{s['offer']}\nПодробнее: {s['url']}\n\n"
            "Если интересно, напишите, расскажу подробнее. Если неактуально, просто ответьте «стоп».")


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


def offer_context(need, catalog=CATALOG):
    s = catalog[need["service"]]
    return (f"Первое сообщение этого диалога написали мы: предложение услуги «{s['name']}» ({s['url']}). "
            f"Повод: человек написал в группе дома «{(need.get('quote') or '')[:160]}».")[:400]


def send_offer(need_id):
    n = q("SELECT * FROM needs WHERE id=?", (need_id,))[0]
    path = choose_path(n["contact_id"], inbox_id=n["inbox_id"])
    inbox = CLOUD_INBOX_ID if path == "B" else n["inbox_id"]
    st, b = cw("GET", f"/contacts/{n['contact_id']}/contactable_inboxes")
    entries = (b or {}).get("payload", []) if isinstance(b, dict) else []
    src = next((e.get("source_id") for e in entries if (e.get("inbox") or {}).get("id") == inbox), None)
    if not src:
        # Path B needs the phone number; a group member known only by a WhatsApp LID has none. Not re-routed to A:
        # the A/B split would stop being random.
        x("UPDATE needs SET status=?, path=? WHERE id=?", (f"failed:no_contact_inbox_{path}", path, need_id))
        log.warning("need=%s contact=%s: no contact inbox in inbox %s (path %s, HTTP %s)", need_id, n["contact_id"],
                    inbox, path, st)
        return False
    message = offer_template(dict(n)) if path == "B" else {"content": offer_text(dict(n))}
    st, c = cw("POST", "/conversations", {"inbox_id": inbox, "contact_id": n["contact_id"], "source_id": src,
                                          "message": message})
    if st not in (200, 201) or not isinstance(c, dict) or not c.get("id"):
        x("UPDATE needs SET status=?, path=? WHERE id=?", (f"failed:http_{st}", path, need_id))
        log.warning("need=%s: create conversation HTTP %s (path %s)", need_id, st, path)
        return False
    x("UPDATE needs SET status='sent', path=?, offer_conv_id=?, sent_at=? WHERE id=?",
      (path, c["id"], time.time(), need_id))
    log.info("need=%s sent: conversation=%s service=%s path=%s", need_id, c["id"], n["service"], path)
    emit("offer.sent", n["contact_id"], {"service": n["service"], "service_name": service_name(n["service"]),
                                         "need_id": need_id, "path": path, "cw_conversation_id": c["id"],
                                         "text": message.get("content") if isinstance(message, dict) else None,
                                         **channel_of(n["inbox_id"])},
         name=n["author"], house=n["group_name"], once=f"offer:{need_id}")
    st, _ = cw("POST", f"/conversations/{c['id']}/custom_attributes",
               {"custom_attributes": {OFFER_ATTR: offer_context(dict(n))}})
    if st >= 300:
        log.warning("need=%s: offer attribute HTTP %s", need_id, st)
    threading.Thread(target=handback, args=(c["id"],), daemon=True).start()
    return True


def policy_inputs(contact_id, now_utc):
    opted = bool(q("SELECT 1 FROM optout WHERE contact_id=?", (contact_id,)))
    last = q("SELECT MAX(sent_at) AS t FROM needs WHERE contact_id=? AND status='sent'", (contact_id,))[0]["t"]
    today = q("SELECT COUNT(*) AS n FROM needs WHERE status='sent' AND sent_at>=?", (local_midnight_ts(now_utc),))[0]["n"]
    tester = bool(q("SELECT 1 FROM testers WHERE contact_id=?", (contact_id,)))
    return {"opted_out": opted, "last_offer_at": last, "sent_today": today, "tester": tester}


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
        emit("need.detected", need["contact_id"], {
            "service": need["service"], "service_name": service_name(need["service"]), "quote": need["quote"],
            "group": need.get("group_name"), "need_id": need_id, "confidence": need["confidence"],
            "decision": stored_full, "cw_conversation_id": need["conv_id"], **channel_of(need["inbox_id"])},
            name=need.get("author"), house=need.get("group_name"), once=f"need:{need_id}", at=need.get("message_at"))
    log.info("need=%s message=%s service=%s conf=%.2f -> %s%s", need_id, need["message_id"], need["service"],
             need["confidence"], status, f" ({why})" if why else "")
    if status == "send":
        send_offer(need_id)


def opt_out(contact_id, conv_id=None, individual=False):
    x("INSERT OR IGNORE INTO optout (contact_id, at) VALUES (?,?)", (contact_id, time.time()))
    x("DELETE FROM messages WHERE contact_id=?", (contact_id,))
    FAQ.forget_contact(contact_id)
    x("UPDATE needs SET status='skipped:optout', quote=NULL WHERE contact_id=? AND status IN ('awaiting','held')",
      (contact_id,))
    cw("PATCH", f"/contacts/{contact_id}", {"custom_attributes": {OPTOUT_ATTR: True}})
    if individual and conv_id:
        # A public reply from us makes the agent step aside in this chat (a person answered): hand it back, so
        # when the person writes again on their own, the seller answers (it sees salesbot_optout and sells
        # nothing, seller-prompt.md rule 8). We never write first after «стоп».
        st, _ = cw("POST", f"/conversations/{conv_id}/messages", {
            "content": "Хорошо, больше не побеспокою. Хорошего дня!"})
        if st not in (200, 201):
            log.warning("optout reply conversation=%s HTTP %s", conv_id, st)
        else:
            handback(conv_id)
    log.info("optout contact=%s", contact_id)
    emit("optout", contact_id, {"cw_conversation_id": conv_id, "where": "dm" if individual else "group"})


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


def campaigns():
    try:
        with open(CAMPAIGNS_PATH, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except OSError:
        return {}


def call_refusal(contact, attrs, campaign, known, now_utc, voice):
    """Why the operator's /call must not ring now, or None. Only testers (who agreed), never after «стоп»."""
    if campaign not in known:
        return "повод: " + ", ".join(f"`/call {k}` ({v.get('title', k)})" for k, v in known.items())
    if not contact.get("id"):
        return "в этом диалоге нет контакта"
    if attrs.get(OPTOUT_ATTR) or q("SELECT 1 FROM optout WHERE contact_id=?", (contact["id"],)):
        return "человек написал «стоп», звонить нельзя"
    if not attrs.get(TESTER_ATTR):
        return f"звоним только тестировщикам (атрибут контакта {TESTER_ATTR})"
    if not (contact.get("phone_number") or "").strip("+"):
        return "у контакта нет номера"
    if quiet_for(now_utc, True):
        start, end = TESTER_QUIET_HOURS.split("-")
        return f"сейчас не звоним (тестировщикам с {end}:00 до {start}:00 по Астане)"
    if voice is None:
        return "голосовой агент недоступен"
    if voice["active"] >= voice["max_calls"]:
        return f"все линии заняты ({voice['active']} из {voice['max_calls']}), попробуйте через пару минут"
    if voice["today"] >= voice["daily"]:
        return f"дневной лимит звонков исчерпан ({voice['daily']})"
    return None


def call_command(p):
    """/call CAMPAIGN in a private note of a person's (not a group's) conversation: the voice agent rings them.
    The answer is a private note in the same conversation; the call's own note follows when it ends."""
    conv = p.get("conversation") or {}
    m = re.match(r"^\s*/call(?:\s+([a-z0-9_-]+))?\s*$", p.get("content") or "")
    conv_id = conv.get("id")
    if not m or not conv_id:
        return "ignored"
    if conv.get("group_type") == "group":
        note(conv_id, "📞 /call работает в личном диалоге с человеком, не в группе.")
        return "refused:group"
    contact = ((conv.get("meta") or {}).get("sender") or {})
    attrs = {}
    if contact.get("id"):
        st, c = cw("GET", f"/contacts/{contact['id']}")
        c = (c or {}).get("payload") or {} if st == 200 else {}
        attrs, contact = c.get("custom_attributes") or {}, {**contact, **{k: c[k] for k in ("phone_number", "name") if c.get(k)}}
    phone = "".join(ch for ch in (contact.get("phone_number") or "") if ch.isdigit())
    campaign = m.group(1)
    st, voice = _http("GET", f"{VOICE_URL}/outbound?campaign={campaign or 'none'}&peer={phone}", timeout=10)
    why = call_refusal(contact, attrs, campaign, campaigns(), datetime.now(timezone.utc),
                       voice if st == 200 and isinstance(voice, dict) else None)
    if why:
        note(conv_id, f"📞 Не звоню: {why}.")
        log.info("operator /call conversation=%s refused: %s", conv_id, why[:60])
        return "refused"
    st, r = cw("POST", f"/conversations/{conv_id}/voice_agent_call",
               {"campaign": campaign, "name": (contact.get("name") or "")[:60], "key": f"note-{p.get('id')}"})
    title = campaigns()[campaign].get("title", campaign)
    if st in (200, 201):
        note(conv_id, f"📞 Звоню: повод «{title}». Итог и стенограмма придут сюда заметкой после звонка.")
        log.info("operator /call conversation=%s campaign=%s placed", conv_id, campaign)
        return "placed"
    err = r.get("error", "") if isinstance(r, dict) else r
    note(conv_id, f"📞 Звонок не начался (HTTP {st}: {str(err or '')[:120]}).")
    log.warning("operator /call conversation=%s HTTP %s", conv_id, st)
    return "failed"


# ---------------------------------------------------------------- webhook intake

def _ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


_marked = set()


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def llm(path, body):
    return _http("POST", f"{LLM_URL}{path}", body, {"Authorization": f"Bearer {LLM_KEY}"}, timeout=60)


FAQ = faqmod.Faq(q=lambda *a: q(*a), x=lambda *a: x(*a), llm=lambda *a: llm(*a), cw=lambda *a: cw(*a),
                 catalog_path=CATALOG_PATH, catalog_roots=[r for s in CATALOG.values() for r in s["roots"]], log=log)
_faq_busy = set()


def answer_in_group(row):
    _faq_busy.add(row["id"])
    try:
        FAQ.handle(row)
    except Exception as e:  # an unanswered question is fine; the need path still sees the message
        log.warning("faq message=%s: %s", row["id"], str(e)[:200])
    finally:
        _faq_busy.discard(row["id"])


_me = []


def my_user_id():
    """The Chatwoot user of CW_API_TOKEN: its public messages in a group are this service's answers."""
    if not _me:
        st, b = _http("GET", f"{CW_URL}/api/v1/profile", headers={"api_access_token": CW_TOKEN})
        _me.append((b or {}).get("id") if st == 200 and isinstance(b, dict) else None)
    return _me[0]


def reply_chain(conv_id, message_id, depth=6, pages=4):
    """The thread a reply belongs to, oldest first: [{"id", "ours", "text"}], following in_reply_to from
    message_id up to `depth` messages, within the group's last pages * 20 messages."""
    by_id, before = {}, None
    for _ in range(pages):
        st, b = cw("GET", f"/conversations/{conv_id}/messages" + (f"?before={before}" if before else ""))
        page = (b or {}).get("payload") or [] if st == 200 and isinstance(b, dict) else []
        by_id.update({m["id"]: m for m in page})
        if message_id in by_id or len(page) < 20:
            break
        before = min(m["id"] for m in page)
    chain, cur = [], message_id
    while cur in by_id and len(chain) < depth:
        m = by_id[cur]
        sender = m.get("sender") or {}
        chain.append({"id": cur, "ours": m.get("message_type") == 1 and not m.get("private")
                                         and sender.get("type") == "user" and sender.get("id") == my_user_id(),
                      "text": (m.get("content") or "").strip()})
        cur = quoted_id(m)
    return list(reversed(chain))


def answer_in_thread(row, reply_to):
    """A reply in the group: to our message (directly or further down our thread) -> continue the thread; to
    anyone else's -> the usual question path."""
    _faq_busy.add(row["id"])
    try:
        chain = reply_chain(row["conv_id"], reply_to)
        if chain and chain[-1]["ours"]:
            FAQ.thread(row, [("Помощник e-KNOT" if m["ours"] else "Жилец", m["text"]) for m in chain if m["text"]]
                       + [("Жилец", row["text"])])
        elif FAQ.wants(row):
            FAQ.handle(row)
    except Exception as e:
        log.warning("faq thread message=%s: %s", row["id"], str(e)[:200])
    finally:
        _faq_busy.discard(row["id"])


def learn_from_reply(row, quoted_id):
    question = q("SELECT * FROM messages WHERE id=? AND conv_id=?", (quoted_id, row["conv_id"]))
    try:
        FAQ.learn(row, dict(question[0]) if question else None)
    except Exception as e:
        log.warning("faq learn message=%s: %s", row["id"], str(e)[:200])


def quoted_id(p):
    """The Chatwoot id of the message this one replies to (WhatsApp "reply"), if any."""
    attrs = p.get("content_attributes") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except ValueError:
            return None
    v = attrs.get("in_reply_to") if isinstance(attrs, dict) else None
    return int(v) if str(v or "").isdigit() else None


def answered_in_group(message_id):
    return message_id in _faq_busy or bool(q(
        "SELECT 1 FROM group_answers WHERE message_id=? AND source IN ('cache','model','thread')", (message_id,)))


def mark_tester(contact_id):
    st, _ = cw("PATCH", f"/contacts/{contact_id}", {"custom_attributes": {TESTER_ATTR: True}})
    if st >= 300:
        _marked.discard(contact_id)
    log.info("tester contact=%s HTTP %s", contact_id, st)


def intake(p):
    """One Chatwoot message_created payload. Returns what was done, for tests and logs."""
    if p.get("event") != "message_created":
        return "ignored:event"
    conv = p.get("conversation") or {}
    sender = p.get("sender") or {}
    text = (p.get("content") or "").strip()
    if p.get("private") and p.get("message_type") == "outgoing" and sender.get("type") == "user":
        if re.match(r"^\s*/call\b", text):
            threading.Thread(target=call_command, args=(p,), daemon=True).start()
            return "command"
        if text.startswith("/"):
            threading.Thread(target=operator_command, args=(text,), daemon=True).start()
            return "command"
        return "ignored:note"
    if (p.get("private") and sender.get("type") == "agent_bot" and conv.get("group_type") != "group"
            and conv.get("id")):
        # The seller leaves a private note for the manager right before handoff_to_human (seller-prompt.md).
        contact = ((conv.get("meta") or {}).get("sender") or {})
        offer = q("SELECT service FROM needs WHERE offer_conv_id=? ORDER BY id DESC LIMIT 1", (conv["id"],))
        emit("lead.handoff", contact.get("id") or (conv.get("contact_inbox") or {}).get("contact_id"), {
            "note": text[:800], "cw_conversation_id": conv["id"], "inbox_id": conv.get("inbox_id"),
            "service": offer[0]["service"] if offer else None,
            "service_name": service_name(offer[0]["service"]) if offer else None, **channel_of(conv.get("inbox_id"))},
            name=contact.get("name"), phone=contact.get("phone_number"), once=f"handoff:{p.get('id')}")
        return "lead"
    if p.get("message_type") != "incoming" or not sender.get("id") or sender.get("type") not in (None, "contact"):
        return "ignored:not_incoming"
    group = conv.get("group_type") == "group"
    if is_stop(text):
        threading.Thread(target=opt_out, args=(sender["id"], conv.get("id"), not group), daemon=True).start()
        return "optout"
    if not group:
        offer = q("SELECT service FROM needs WHERE offer_conv_id=? ORDER BY id DESC LIMIT 1", (conv.get("id"),))
        if offer:  # the first answer to our offer moves the card to REPLIED (once per conversation)
            emit("dm.replied", sender["id"], {"service": offer[0]["service"],
                                              "service_name": service_name(offer[0]["service"]),
                                              "text": text[:500], "cw_conversation_id": conv.get("id"),
                                              **channel_of(conv.get("inbox_id"))},
                 name=sender.get("name"), phone=sender.get("phone_number"), once=f"reply:{conv.get('id')}")
            return "ignored:individual:replied"
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
    row = {"id": p["id"], "conv_id": conv["id"], "contact_id": sender["id"], "author": sender.get("name"),
           "text": text[:2000], "created_at": _ts(p.get("created_at"))}
    reply_to = quoted_id(p)
    if faqmod.FAQ_MODE == "auto" and reply_to:
        _spawn(learn_from_reply, row, reply_to)
    if reply_to and faqmod.FAQ_MODE == "auto":
        _spawn(answer_in_thread, row, reply_to)
    elif FAQ.wants(row):
        _spawn(answer_in_group, row)
    if (sender.get("custom_attributes") or {}).get(TESTER_ATTR) or AUTO_TESTERS:
        x("INSERT OR IGNORE INTO testers (contact_id, at) VALUES (?,?)", (sender["id"], time.time()))
    if (AUTO_TESTERS and sender["id"] not in _marked
            and not (sender.get("custom_attributes") or {}).get(TESTER_ATTR)):
        _marked.add(sender["id"])
        _spawn(mark_tester, sender["id"])
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
        if result != "ignored:not_incoming":
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
        if answered_in_group(r["id"]):  # the group already has the answer; a private offer on top is pushy
            log.info("need on message=%s skipped: answered in the group", r["id"])
            continue
        handle_need({**n, "conv_id": conv_id, "inbox_id": r["inbox_id"], "contact_id": r["contact_id"],
                     "author": r["author"], "group_name": r["group_name"], "message_at": r["created_at"]})
    with _db_lock:
        db().execute(f"UPDATE messages SET state='done' WHERE id IN ({','.join('?' * len(ids))})", ids)


def release_held():
    now = datetime.now(timezone.utc)
    for n in q("SELECT id, contact_id, found_at FROM needs WHERE status='held'"):
        if time.time() - n["found_at"] > HOLD_MAX_HOURS * 3600:
            x("UPDATE needs SET status='skipped:expired' WHERE id=?", (n["id"],))
            continue
        inputs = policy_inputs(n["contact_id"], now)
        if quiet_for(now, inputs["tester"]):
            continue
        if inputs["opted_out"] or inputs["sent_today"] >= DAILY_SEND_CAP or (
                inputs["last_offer_at"] and time.time() - inputs["last_offer_at"] < cooldown_secs(inputs["tester"])):
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


SERVICE_NAMES = {k: s["name"] for k, s in CATALOG.items()}


def offer_outcome(n, optout_at):
    """What happened to one sent offer, read back from its Chatwoot conversation."""
    st, msgs = cw("GET", f"/conversations/{n['offer_conv_id']}/messages")
    _, conv = cw("GET", f"/conversations/{n['offer_conv_id']}")
    msgs = (msgs or {}).get("payload") or [] if st == 200 else []
    public = [m for m in msgs if not m.get("private")]
    offer = next((m for m in public if m.get("message_type") == 1), {})
    replies = [m for m in public if m.get("message_type") == 0 and m.get("created_at", 0) <= n["sent_at"] + 86400]
    labels = (conv or {}).get("labels") or []
    return {"delivery": offer.get("status") or "unknown", "replied": bool(replies),
            "optout": bool(optout_at and optout_at >= n["sent_at"]), "lead": "lead" in labels,
            "handoff": (conv or {}).get("status") == "open"}


def backfill_events():
    """Replay the history into the orchestrator (cards in Twenty): every need, offer, first reply, handoff and
    opt-out we have, in time order. Safe to rerun: each event has its `once` key, the same the live hooks use."""
    global EVENTS
    sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "events"))
    from outbox import Outbox
    EVENTS = Outbox(os.path.join(os.path.dirname(DB_PATH), "outbox.db"), "group-watcher")  # serve's thread sends
    if not EVENTS.enabled:
        sys.exit("ORCH_TOKEN is empty")
    queued = []
    for n in map(dict, q("SELECT * FROM needs WHERE confidence >= ? ORDER BY message_at", (MIN_CONFIDENCE,))):
        svc = {"service": n["service"], "service_name": service_name(n["service"]), **channel_of(n["inbox_id"])}
        queued.append((n["message_at"], "need.detected", n["contact_id"], dict(svc, quote=n["quote"],
                       group=n["group_name"], need_id=n["id"], confidence=n["confidence"], decision=n["status"],
                       cw_conversation_id=n["conv_id"]), n, f"need:{n['id']}"))
        if n["status"] != "sent" or not n["offer_conv_id"]:
            continue
        queued.append((n["sent_at"], "offer.sent", n["contact_id"], dict(svc, need_id=n["id"], path=n["path"],
                       cw_conversation_id=n["offer_conv_id"]), n, f"offer:{n['id']}"))
        st, msgs = cw("GET", f"/conversations/{n['offer_conv_id']}/messages")
        msgs = sorted((msgs or {}).get("payload") or [] if st == 200 else [], key=lambda m: m.get("created_at", 0))
        reply = next((m for m in msgs if not m.get("private") and m.get("message_type") == 0), None)
        if reply:
            queued.append((reply["created_at"], "dm.replied", n["contact_id"], dict(svc, text=(reply.get("content") or "")[:500],
                           cw_conversation_id=n["offer_conv_id"]), n, f"reply:{n['offer_conv_id']}"))
        hand = next((m for m in msgs if m.get("private") and (m.get("sender") or {}).get("type") == "agent_bot"), None)
        if hand:
            queued.append((hand["created_at"], "lead.handoff", n["contact_id"], dict(svc, note=(hand.get("content") or "")[:800],
                           cw_conversation_id=n["offer_conv_id"]), n, f"handoff:{hand['id']}"))
    for o in q("SELECT contact_id, at FROM optout"):
        queued.append((o["at"], "optout", o["contact_id"], {"where": "history"}, {}, f"optout:{o['contact_id']}"))
    sent = 0
    for at, type, contact, data, n, once in sorted(queued, key=lambda t: t[0] or 0):
        if EVENTS.emit(type, contact, data, name=n.get("author") if n else None,
                       house=n.get("group_name") if n else None, once=once, at=at):
            sent += 1
    print(f"queued {sent} of {len(queued)} events (the rest were sent before); the service delivers them")


def report(days=1.0):
    since = time.time() - days * 86400
    msgs = q("SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?", (since,))[0]["n"]
    needs = q("SELECT * FROM needs WHERE found_at >= ? ORDER BY id", (since,))
    optouts = {r["contact_id"]: r["at"] for r in q("SELECT * FROM optout")}
    sent = [n for n in needs if n["status"] == "sent" and n["offer_conv_id"]]
    outs = [offer_outcome(n, optouts.get(n["contact_id"])) for n in sent]
    lat = sorted(n["sent_at"] - n["message_at"] for n in sent)
    count = lambda k: sum(o[k] for o in outs)  # noqa: E731
    by_status, by_service = {}, {}
    for n in needs:
        by_status[n["status"]] = by_status.get(n["status"], 0) + 1
        by_service[n["service"]] = by_service.get(n["service"], 0) + 1
    delivery = {}
    for o in outs:
        delivery[o["delivery"]] = delivery.get(o["delivery"], 0) + 1
    pct = lambda a: f"{a} ({round(100 * a / len(outs))}%)" if outs else str(a)  # noqa: E731
    lines = [
        f"group-watcher за {days:g} сут. (режим {SEND_MODE}, модель {LLM_MODEL})",
        f"Сообщений в группах: {msgs}. Найдено потребностей: {len(needs)}. Отправлено предложений: {len(sent)}.",
        "По услугам: " + (", ".join(f"{SERVICE_NAMES.get(k, k)} {v}" for k, v in
                                     sorted(by_service.items(), key=lambda kv: -kv[1])) or "нет"),
        "Решения: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "нет"),
        f"От сообщения в группе до предложения: медиана {round(statistics.median(lat))} с, "
        f"максимум {round(lat[-1])} с." if lat else "От сообщения до предложения: нет отправок.",
        "Доставка: " + (", ".join(f"{k} {v}" for k, v in sorted(delivery.items())) or "нет"),
        f"Ответили за 24 ч: {pct(count('replied'))}. Отказ «стоп»: {pct(count('optout'))}. "
        f"Лид (метка lead): {pct(count('lead'))}. Передано менеджеру: {pct(count('handoff'))}.",
    ]
    ga = q("SELECT source, latency FROM group_answers WHERE at >= ?", (since,))
    posted = [r for r in ga if r["source"] in ("cache", "model")]
    if ga:
        gl = sorted(r["latency"] for r in posted if r["latency"] is not None)
        lines.append(f"Вопросы в группе: {len(ga)}, ответили в группе {len(posted)} "
                     f"(из кеша {sum(r['source'] == 'cache' for r in posted)}), "
                     f"медиана до ответа {round(statistics.median(gl))} с." if gl else
                     f"Вопросы в группе: {len(ga)}, ответов в группе нет.")
    paths = sorted({n["path"] or "A" for n in sent})
    if len(paths) > 1:  # A/B comparison: same funnel per send path
        for path in paths:
            po = [o for n, o in zip(sent, outs) if (n["path"] or "A") == path]
            pl = sorted(n["sent_at"] - n["message_at"] for n in sent if (n["path"] or "A") == path)
            share = lambda k: f"{sum(o[k] for o in po)}/{len(po)}"  # noqa: E731
            lines.append(f"Путь {path}: отправлено {len(po)}, медиана до предложения {round(statistics.median(pl))} с, "
                         f"ответили {share('replied')}, стоп {share('optout')}, лид {share('lead')}, "
                         f"менеджеру {share('handoff')}.")
    print("\n".join(lines))
    return {"messages": msgs, "needs": len(needs), "sent": len(sent), "by_status": by_status,
            "by_service": by_service, "delivery": delivery, "outcomes": outs}


def forget(contact_id):
    x("DELETE FROM optout WHERE contact_id=?", (contact_id,))
    x("DELETE FROM needs WHERE contact_id=?", (contact_id,))
    x("DELETE FROM messages WHERE contact_id=?", (contact_id,))
    FAQ.forget_contact(contact_id)
    st, _ = cw("PATCH", f"/contacts/{contact_id}", {"custom_attributes": {OPTOUT_ATTR: False}})
    print(f"contact {contact_id}: opt-out, offers and cooldown cleared; Chatwoot attribute HTTP {st}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif cmd == "eval" and len(sys.argv) == 3:
        evaluate(sys.argv[2])
    elif cmd == "report" and len(sys.argv) <= 3:
        report(float(sys.argv[2]) if len(sys.argv) == 3 else 1.0)
    elif cmd == "forget" and len(sys.argv) == 3 and sys.argv[2].isdigit():
        forget(int(sys.argv[2]))
    elif cmd == "events-backfill":
        db()
        backfill_events()
    elif cmd == "faq":
        for r in q("SELECT id, hits, kb_version, disabled, datetime(created_at,'unixepoch') AS at, question, answer, "
                   "conv_id, author FROM faq ORDER BY hits DESC, id DESC"):
            where = f"chat group={r['conv_id']} by {r['author']}" if r["conv_id"] else f"kb={r['kb_version']}"
            print(f"#{r['id']} hits={r['hits']} {where}{' OFF' if r['disabled'] else ''} {r['at']}\n"
                  f"  ? {r['question'][:150]}\n  > {r['answer'][:300]}")
    elif cmd == "faq-ask" and len(sys.argv) == 3:
        t = time.time()
        answer, faq_id, source = FAQ.lookup_or_generate(sys.argv[2])
        print(f"{source} faq={faq_id} {time.time() - t:.1f}s candidate={faqmod.is_candidate(sys.argv[2], FAQ.roots)}"
              f"\n{answer}")
    elif cmd == "faq-off" and len(sys.argv) == 3 and sys.argv[2].isdigit():
        x("UPDATE faq SET disabled=1 WHERE id=?", (int(sys.argv[2]),))
        print("disabled", sys.argv[2])
    elif cmd == "serve":
        if not (CW_TOKEN and WEBHOOK_SECRET and LLM_KEY):
            log.warning("CW_API_TOKEN / WEBHOOK_SECRET / LLM_KEY missing: run bootstrap.py group-watcher; "
                        "webhooks are refused until then")
        if SEND_MODE not in ("auto", "approve", "dry"):
            sys.exit(f"SEND_MODE must be auto, approve or dry, not {SEND_MODE!r}")
        db()
        global EVENTS
        sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "events"))
        from outbox import Outbox
        EVENTS = Outbox(os.path.join(os.path.dirname(DB_PATH), "outbox.db"), "group-watcher").start()
        log.info("events: %s", "on" if EVENTS.enabled else "off (no ORCH_TOKEN)")
        threading.Thread(target=loop, daemon=True).start()
        log.info("serving :8080 mode=%s label=%s model=%s", SEND_MODE, WATCH_LABEL, LLM_MODEL)
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
