#!/usr/bin/env python3
"""Telegram userbot <-> Chatwoot bridge (spike S2, docs/V2-GROUPS-PLAN.md). TEST DATA ONLY.

A real Telegram account (MTProto, Telethon), not a bot: residents see a person with a username.

  private chats  -> Chatwoot API inbox (public client API: contact "tg:<user_id>", conversation, incoming message);
                    Chatwoot's outgoing messages come back on its webhook (HMAC-signed) and are sent from the account.
  groups         -> rows in /data/userbot.db `group_messages`: the queue the profiler will read (plan §4).

DEFAULT DENY. The account is a person's account and has personal chats: nothing is read, stored or forwarded
unless the chat was allowed by the account owner. To allow a chat, write `/salesbot allow` in it FROM THIS
ACCOUNT (in a group, or in a private chat with a tester); `/salesbot deny` revokes, `/salesbot status` answers
in place. The command message is deleted right away. Logs carry chat/user ids and lengths, never message text.

    python3 -I userbot.py login     # once, interactive: phone, code, 2FA password -> /data/userbot.session
    python3 -I userbot.py run       # the service (default)

Env: TG_API_ID, TG_API_HASH (my.telegram.org), CW_URL, CW_INBOX_IDENTIFIER (API inbox), CW_WEBHOOK_SECRET,
     SEND_MIN_INTERVAL (s between sends to one chat, default 3), SEND_MAX_PER_HOUR (all chats, default 30).
"""
import asyncio, hashlib, hmac, json, logging, os, sqlite3, sys, time

DATA = os.environ.get("DATA_DIR", "/data")
SESSION = os.path.join(DATA, "userbot")  # Telethon appends .session
DB = os.path.join(DATA, "userbot.db")
API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")
CW_URL = os.environ.get("CW_URL", "http://cw-rails:3000").rstrip("/")
INBOX = os.environ.get("CW_INBOX_IDENTIFIER", "")
WEBHOOK_SECRET = os.environ.get("CW_WEBHOOK_SECRET", "")
SEND_MIN_INTERVAL = float(os.environ.get("SEND_MIN_INTERVAL", "3"))
SEND_MAX_PER_HOUR = int(os.environ.get("SEND_MAX_PER_HOUR", "30"))
WEBHOOK_MAX_SKEW = 300
CMD = "/salesbot"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("tg-userbot")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    os.umask(0o077)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS allowed_chats (chat_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT,
            allowed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts (tg_user_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL,
            conversation_id INTEGER);
        CREATE TABLE IF NOT EXISTS group_messages (chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
            chat_title TEXT, sender_id INTEGER, sender_name TEXT, sender_username TEXT, sent_at TEXT NOT NULL,
            text TEXT, reply_to INTEGER, has_media INTEGER NOT NULL DEFAULT 0, consumed_at TEXT,
            PRIMARY KEY (chat_id, msg_id));
        CREATE TABLE IF NOT EXISTS deliveries (delivery_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
        """)


def allowed(chat_id):
    with db() as c:
        return c.execute("SELECT 1 FROM allowed_chats WHERE chat_id=?", (chat_id,)).fetchone() is not None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def display_name(u):
    if u is None:
        return ""
    name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
    return name or getattr(u, "title", "") or ""


# ---------- Chatwoot public client API (inbox identifier only, no admin token) ----------

class Chatwoot:
    def __init__(self, session):
        self.s = session
        self.base = f"{CW_URL}/public/api/v1/inboxes/{INBOX}"

    async def _post(self, path, body):
        async with self.s.post(self.base + path, json=body) as r:
            data = await r.json(content_type=None)
            if r.status >= 300:
                raise RuntimeError(f"chatwoot {path.split('/')[1] if '/' in path else path}: HTTP {r.status}")
            return data

    async def incoming(self, user, text):
        """Post a resident's private message; creates the contact and an open conversation on first use."""
        with db() as c:
            row = c.execute("SELECT * FROM contacts WHERE tg_user_id=?", (user.id,)).fetchone()
        if row is None:
            body = {"identifier": f"tg:{user.id}", "name": display_name(user) or f"tg {user.id}"}
            contact = await self._post("/contacts", body)
            source_id = contact["source_id"]
            conv = None
        else:
            source_id, conv = row["source_id"], row["conversation_id"]
        if conv is None:
            conv = (await self._post(f"/contacts/{source_id}/conversations", {}))["id"]
        try:
            await self._post(f"/contacts/{source_id}/conversations/{conv}/messages", {"content": text})
        except RuntimeError:
            # The conversation may be gone (deleted in Chatwoot): open a new one once.
            conv = (await self._post(f"/contacts/{source_id}/conversations", {}))["id"]
            await self._post(f"/contacts/{source_id}/conversations/{conv}/messages", {"content": text})
        with db() as c:
            c.execute("INSERT INTO contacts (tg_user_id, source_id, conversation_id) VALUES (?,?,?) "
                      "ON CONFLICT(tg_user_id) DO UPDATE SET source_id=excluded.source_id, "
                      "conversation_id=excluded.conversation_id", (user.id, source_id, conv))
        return conv


# ---------- outgoing pacing (Telegram antispam: PEER_FLOOD / FLOOD_WAIT) ----------

class Pacer:
    def __init__(self):
        self.last = {}
        self.sent = []
        self.lock = asyncio.Lock()

    async def wait(self, chat_id):
        async with self.lock:
            t = time.monotonic()
            self.sent = [x for x in self.sent if t - x < 3600]
            if len(self.sent) >= SEND_MAX_PER_HOUR:
                raise RuntimeError("hourly send limit reached")
            gap = SEND_MIN_INTERVAL - (t - self.last.get(chat_id, -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            self.last[chat_id] = time.monotonic()
            self.sent.append(time.monotonic())


# ---------- service ----------

async def run():
    from aiohttp import ClientSession, ClientTimeout, web
    from telethon import TelegramClient, events, errors

    if not (API_ID and API_HASH and INBOX and WEBHOOK_SECRET):
        sys.exit("TG_API_ID, TG_API_HASH, CW_INBOX_IDENTIFIER and CW_WEBHOOK_SECRET are required "
                 "(bootstrap.py tg-userbot writes the last two)")
    init_db()
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("not logged in: run `userbot.py login` once (see SETUP.md)")
    me = await client.get_me()
    log.info("account id=%s username=%s", me.id, "@" + me.username if me.username else "-")
    http = ClientSession(timeout=ClientTimeout(total=20))
    cw = Chatwoot(http)
    pacer = Pacer()

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{CMD}\b"))
    async def command(ev):
        arg = (ev.raw_text.split() + ["", ""])[1].lower()
        chat = await ev.get_chat()
        kind = "private" if ev.is_private else "group"
        if arg == "allow":
            with db() as c:
                c.execute("INSERT OR REPLACE INTO allowed_chats VALUES (?,?,?,?)",
                          (ev.chat_id, kind, display_name(chat), now_iso()))
        elif arg == "deny":
            with db() as c:
                c.execute("DELETE FROM allowed_chats WHERE chat_id=?", (ev.chat_id,))
        state = "allowed" if allowed(ev.chat_id) else "not allowed"
        log.info("command %s chat=%s kind=%s -> %s", arg or "?", ev.chat_id, kind, state)
        await ev.delete()
        if arg == "status":
            note = await client.send_message("me", f"salesbot: chat {ev.chat_id} ({kind}) is {state}")
            log.info("status sent to Saved Messages (msg %s)", note.id)

    @client.on(events.NewMessage(incoming=True))
    async def incoming(ev):
        if not allowed(ev.chat_id):
            return
        sender = await ev.get_sender()
        text = ev.raw_text or ""
        if ev.is_private:
            if getattr(sender, "bot", False):
                return
            content = text or "[вложение без текста]"
            try:
                conv = await cw.incoming(sender, content)
                log.info("private in: user=%s len=%d -> conversation %s", sender.id, len(text), conv)
            except Exception as e:
                log.error("private in: user=%s failed: %s", sender.id, e)
            return
        chat = await ev.get_chat()
        with db() as c:
            c.execute("INSERT OR IGNORE INTO group_messages (chat_id, msg_id, chat_title, sender_id, sender_name, "
                      "sender_username, sent_at, text, reply_to, has_media) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ev.chat_id, ev.id, display_name(chat), getattr(sender, "id", None), display_name(sender),
                       getattr(sender, "username", None), ev.date.strftime("%Y-%m-%dT%H:%M:%SZ"), text,
                       ev.reply_to_msg_id, int(ev.media is not None)))
        log.info("group in: chat=%s sender=%s len=%d", ev.chat_id, getattr(sender, "id", None), len(text))

    async def webhook(request):
        raw = await request.read()
        ts = request.headers.get("X-Chatwoot-Timestamp", "")
        sig = request.headers.get("X-Chatwoot-Signature", "")
        want = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        if not (ts.isdigit() and abs(time.time() - int(ts)) <= WEBHOOK_MAX_SKEW and hmac.compare_digest(sig, want)):
            log.warning("webhook: bad signature")
            return web.Response(status=401)
        p = json.loads(raw or b"{}")
        if p.get("event") != "message_created" or p.get("message_type") != "outgoing" or p.get("private"):
            return web.Response(status=200)
        delivery = request.headers.get("X-Chatwoot-Delivery") or f"msg:{p.get('id')}"
        with db() as c:
            if c.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?)", (delivery, now_iso())).rowcount == 0:
                return web.Response(status=200)
        ident = ((p.get("conversation") or {}).get("meta") or {}).get("sender", {}).get("identifier") or ""
        if not ident.startswith("tg:") or not ident[3:].isdigit():
            log.warning("webhook: message %s has no tg contact", p.get("id"))
            return web.Response(status=200)
        user_id = int(ident[3:])
        if not allowed(user_id):
            log.warning("webhook: user %s is not an allowed chat; not sent", user_id)
            return web.Response(status=200)
        content = (p.get("content") or "").strip()
        if not content:
            return web.Response(status=200)
        try:
            await pacer.wait(user_id)
            await client.send_message(user_id, content)
            log.info("private out: user=%s len=%d", user_id, len(content))
        except errors.FloodWaitError as e:
            log.error("private out: FLOOD_WAIT %ss (user %s)", e.seconds, user_id)
            return web.Response(status=503)
        except Exception as e:
            log.error("private out: user=%s failed: %s", user_id, type(e).__name__)
            return web.Response(status=500)
        return web.Response(status=200)

    async def healthz(_):
        return web.Response(text="ok" if client.is_connected() else "telegram disconnected",
                            status=200 if client.is_connected() else 503)

    app = web.Application(client_max_size=1 << 20)
    app.router.add_post("/chatwoot", webhook)
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    log.info("listening :8080; %d allowed chats", len(db().execute("SELECT 1 FROM allowed_chats").fetchall()))
    try:
        await client.run_until_disconnected()
    finally:
        await http.close()


async def login():
    from telethon import TelegramClient
    if not (API_ID and API_HASH):
        sys.exit("TG_API_ID and TG_API_HASH are required")
    os.umask(0o077)
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()  # asks for phone, code and 2FA password on the terminal
    me = await client.get_me()
    print(f"logged in: id={me.id} username={'@' + me.username if me.username else '-'}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(login() if sys.argv[1:2] == ["login"] else run())
