#!/usr/bin/env python3
"""Telegram userbot <-> Chatwoot bridge (plan v2, docs/V2-GROUPS-PLAN.md). TEST DATA ONLY.

A real Telegram account (MTProto, Telethon), not a bot: residents see a person with a username.

  private chats  -> Chatwoot API inbox (public client API: contact "tg:<user_id>", conversation, incoming message,
                    photos/voice/documents as attachments). Chatwoot's outgoing messages (bot or operator) come back
                    on the inbox webhook (HMAC-signed) and are sent from the account: read receipt, "typing…" for a
                    while that grows with the length, then the text and attachments.
                    What the owner types from their phone in an allowed private chat is mirrored into the conversation
                    as an operator message (CW_OPERATOR_TOKEN), so Chatwoot and the agent see the whole dialog.
  groups         -> rows in /data/userbot.db `group_messages`: the queue the profiler will read (plan §4).
  first contact  -> Chatwoot can write first to a member of an allowed group who never wrote to the account
                    (bootstrap.py tg-outreach): the bridge sends it and from then on handles that private chat.

DEFAULT DENY. The account is a person's account and has personal chats: nothing is read, stored or forwarded
unless the chat was allowed by the account owner. To allow a chat, write `/salesbot allow` in it FROM THIS
ACCOUNT (in a group, or in a private chat with a tester); `/salesbot deny` revokes, `/salesbot status` reports to
Saved Messages. The command message is deleted right away. Logs carry ids and lengths, never message text.

    python3 -I userbot.py login     # once, interactive: phone, code, 2FA password -> /data/userbot.session
    python3 -I userbot.py run       # the service (default)

Env: TG_API_ID, TG_API_HASH (my.telegram.org), CW_URL, CW_ACCOUNT_ID, CW_INBOX_IDENTIFIER (API inbox),
     CW_WEBHOOK_SECRET, CW_OPERATOR_TOKEN (optional: mirror the owner's phone messages),
     SEND_MIN_INTERVAL (s between sends to one chat, default 3), SEND_MAX_PER_HOUR (all chats, default 30),
     MEDIA_MAX_MB (default 20).
"""
import asyncio, hashlib, hmac, io, json, logging, os, sqlite3, sys, time
from urllib.parse import urlsplit

DATA = os.environ.get("DATA_DIR", "/data")
SESSION = os.path.join(DATA, "userbot")  # Telethon appends .session
DB = os.path.join(DATA, "userbot.db")
API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH", "")
CW_URL = os.environ.get("CW_URL", "http://cw-rails:3000").rstrip("/")
CW_ACCOUNT = os.environ.get("CW_ACCOUNT_ID", "")
INBOX = os.environ.get("CW_INBOX_IDENTIFIER", "")
WEBHOOK_SECRET = os.environ.get("CW_WEBHOOK_SECRET", "")
OPERATOR_TOKEN = os.environ.get("CW_OPERATOR_TOKEN", "")
SEND_MIN_INTERVAL = float(os.environ.get("SEND_MIN_INTERVAL", "3"))
SEND_MAX_PER_HOUR = int(os.environ.get("SEND_MAX_PER_HOUR", "30"))
MEDIA_MAX = int(float(os.environ.get("MEDIA_MAX_MB", "20")) * 1024 * 1024)
WEBHOOK_MAX_SKEW = 300
BACKFILL_EVERY = int(os.environ.get("BACKFILL_EVERY", "300"))
MIRROR_PREFIX = "tgm:"  # source_id of messages mirrored from the owner's phone: not sent back to Telegram
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


def allow(chat_id, kind, title):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO allowed_chats VALUES (?,?,?,?)", (chat_id, kind, title, now_iso()))


def seen_in_allowed_group(user_id):
    """A member who wrote in an allowed group: Chatwoot may contact them first (outreach)."""
    with db() as c:
        return c.execute("SELECT 1 FROM group_messages g JOIN allowed_chats a ON a.chat_id = g.chat_id "
                         "WHERE g.sender_id=? LIMIT 1", (user_id,)).fetchone() is not None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def display_name(u):
    if u is None:
        return ""
    name = " ".join(x for x in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if x)
    return name or getattr(u, "title", "") or ""


# ---------- Chatwoot ----------

class Chatwoot:
    """Incoming messages through the public client API (inbox identifier only, no token); the owner's phone
    messages through the account API with a dedicated operator user's token (agent role, this inbox only)."""

    def __init__(self, session):
        self.s = session
        self.base = f"{CW_URL}/public/api/v1/inboxes/{INBOX}"

    async def _post(self, url, json_body=None, form=None, headers=None):
        async with self.s.post(url, json=json_body, data=form, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status >= 300:
                raise RuntimeError(f"chatwoot {urlsplit(url).path.rsplit('/', 1)[-1]}: HTTP {r.status}")
            return data

    async def conversation(self, user):
        """(source_id, conversation display id) for a Telegram user; creates contact/conversation on first use."""
        with db() as c:
            row = c.execute("SELECT * FROM contacts WHERE tg_user_id=?", (user.id,)).fetchone()
        if row is None:
            contact = await self._post(self.base + "/contacts",
                                       {"identifier": f"tg:{user.id}", "name": display_name(user) or f"tg {user.id}"})
            source_id, conv = contact["source_id"], None
        else:
            source_id, conv = row["source_id"], row["conversation_id"]
        if conv is None:
            conv = (await self._post(f"{self.base}/contacts/{source_id}/conversations", {}))["id"]
            self._remember(user.id, source_id, conv)
        return source_id, conv

    def _remember(self, user_id, source_id, conv):
        with db() as c:
            c.execute("INSERT INTO contacts (tg_user_id, source_id, conversation_id) VALUES (?,?,?) "
                      "ON CONFLICT(tg_user_id) DO UPDATE SET source_id=excluded.source_id, "
                      "conversation_id=excluded.conversation_id", (user_id, source_id, conv))

    async def incoming(self, user, text, media=None):
        """A resident's private message (media = (bytes, filename, mime) or None)."""
        source_id, conv = await self.conversation(user)

        def body():
            if media is None:
                return {"json_body": {"content": text}}
            from aiohttp import FormData
            f = FormData()
            if text:
                f.add_field("content", text)
            f.add_field("attachments[]", media[0], filename=media[1], content_type=media[2])
            return {"form": f}

        try:
            await self._post(f"{self.base}/contacts/{source_id}/conversations/{conv}/messages", **body())
        except RuntimeError:
            # The conversation may be gone (deleted in Chatwoot): open a new one once.
            conv = (await self._post(f"{self.base}/contacts/{source_id}/conversations", {}))["id"]
            self._remember(user.id, source_id, conv)
            await self._post(f"{self.base}/contacts/{source_id}/conversations/{conv}/messages", **body())
        return conv

    async def mirror(self, user, text, msg_id):
        """The owner wrote to a resident from their phone: an operator message in the same conversation."""
        _, conv = await self.conversation(user)
        await self._post(f"{CW_URL}/api/v1/accounts/{CW_ACCOUNT}/conversations/{conv}/messages",
                         {"content": text, "message_type": "outgoing", "source_id": f"{MIRROR_PREFIX}{msg_id}"},
                         headers={"api_access_token": OPERATOR_TOKEN})
        return conv

    async def download(self, data_url):
        """Attachment of an outgoing message. Its URL carries FRONTEND_URL's host; fetch it inside the network."""
        u = urlsplit(data_url)
        async with self.s.get(f"{CW_URL}{u.path}" + (f"?{u.query}" if u.query else "")) as r:
            if r.status >= 300:
                raise RuntimeError(f"attachment: HTTP {r.status}")
            if (r.content_length or 0) > MEDIA_MAX:
                raise RuntimeError("attachment too large")
            return await r.read()


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


def typing_seconds(text):
    """How long a person would type it: ~15 characters a second, 1.5 to 6 s."""
    return min(6.0, max(1.5, len(text) / 15))


# ---------- service ----------

async def run():
    from aiohttp import ClientSession, ClientTimeout, web
    from telethon import TelegramClient, events, errors

    if not (API_ID and API_HASH and INBOX and WEBHOOK_SECRET and CW_ACCOUNT):
        sys.exit("TG_API_ID, TG_API_HASH, CW_ACCOUNT_ID, CW_INBOX_IDENTIFIER and CW_WEBHOOK_SECRET are required "
                 "(bootstrap.py tg-userbot writes the Chatwoot ones)")
    init_db()
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("not logged in: run `userbot.py login` once (see SETUP.md)")
    me = await client.get_me()
    log.info("account id=%s username=%s; phone mirror %s", me.id, "@" + me.username if me.username else "-",
             "on" if OPERATOR_TOKEN else "off (no CW_OPERATOR_TOKEN)")
    http = ClientSession(timeout=ClientTimeout(total=60))
    cw = Chatwoot(http)
    pacer = Pacer()
    bridge_sent = set()  # Telegram ids of messages this process sent: not the owner typing on the phone
    outbox = {}  # user id -> queue of Chatwoot messages to send, in order

    async def media_of(msg):
        if msg.media is None or msg.file is None:
            return None
        if (msg.file.size or 0) > MEDIA_MAX:
            log.warning("media of msg %s too large (%s bytes); text only", msg.id, msg.file.size)
            return None
        data = await msg.download_media(file=bytes)
        if not data:
            return None
        kind = "voice" if msg.voice else "video_note" if msg.video_note else "photo" if msg.photo else "file"
        name = msg.file.name or f"{kind}{msg.file.ext or ''}"
        return data, name, msg.file.mime_type or "application/octet-stream"

    async def resolve(user_id):
        """Input entity for a user; members of allowed groups are cached from the participant lists."""
        try:
            return await client.get_input_entity(user_id)
        except ValueError:
            with db() as c:
                groups = [r[0] for r in c.execute("SELECT chat_id FROM allowed_chats WHERE kind='group'")]
            for g in groups:
                await client.get_participants(g)
            return await client.get_input_entity(user_id)

    @client.on(events.NewMessage(outgoing=True, pattern=rf"^{CMD}\b"))
    async def command(ev):
        arg = (ev.raw_text.split() + ["", ""])[1].lower()
        chat = await ev.get_chat()
        kind = "private" if ev.is_private else "group"
        if arg == "allow":
            allow(ev.chat_id, kind, display_name(chat))
        elif arg == "deny":
            with db() as c:
                c.execute("DELETE FROM allowed_chats WHERE chat_id=?", (ev.chat_id,))
        state = "allowed" if allowed(ev.chat_id) else "not allowed"
        log.info("command %s chat=%s kind=%s -> %s", arg or "?", ev.chat_id, kind, state)
        await ev.delete()
        if arg == "status":
            note = await client.send_message("me", f"salesbot: chat {ev.chat_id} ({kind}) is {state}")
            log.info("status sent to Saved Messages (msg %s)", note.id)

    @client.on(events.NewMessage())
    async def message(ev):
        if ev.out and (ev.raw_text or "").startswith(CMD):
            return
        if not allowed(ev.chat_id):
            return
        text = ev.raw_text or ""
        if ev.is_private:
            if ev.out:
                await mirror(ev, text)
                return
            sender = await ev.get_sender()
            if getattr(sender, "bot", False):
                return
            media = await media_of(ev.message)
            if not (text or media):
                return
            try:
                conv = await cw.incoming(sender, text, media)
                log.info("private in: user=%s len=%d media=%s -> conversation %s", sender.id, len(text),
                         media[2] if media else "-", conv)
            except Exception as e:
                log.error("private in: user=%s failed: %s", sender.id, e)
            return
        sender = await ev.get_sender()
        store_group(ev.chat_id, display_name(await ev.get_chat()), ev.message, sender)
        log.info("group %s: chat=%s sender=%s len=%d", "out" if ev.out else "in", ev.chat_id,
                 getattr(sender, "id", None), len(text))

    def store_group(chat_id, title, msg, sender):
        with db() as c:
            return c.execute(
                "INSERT OR IGNORE INTO group_messages (chat_id, msg_id, chat_title, sender_id, sender_name, "
                "sender_username, sent_at, text, reply_to, has_media) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chat_id, msg.id, title, getattr(sender, "id", None), display_name(sender),
                 getattr(sender, "username", None), msg.date.strftime("%Y-%m-%dT%H:%M:%SZ"), msg.raw_text or "",
                 msg.reply_to_msg_id, int(msg.media is not None))).rowcount

    async def backfill():
        """Updates are lost while the service is down or Telegram drops them: read each allowed group's history
        since it was allowed and store what is missing. At start, then every BACKFILL_EVERY seconds."""
        while True:
            with db() as c:
                groups = c.execute("SELECT chat_id, title, allowed_at FROM allowed_chats WHERE kind='group'").fetchall()
            for g in groups:
                since = time.mktime(time.strptime(g["allowed_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                added = 0
                try:
                    async for msg in client.iter_messages(g["chat_id"], limit=500):
                        if msg.date.timestamp() < since:
                            break
                        if msg.action is not None or (msg.out and (msg.raw_text or "").startswith(CMD)):
                            continue
                        added += store_group(g["chat_id"], g["title"], msg, await msg.get_sender())
                except Exception as e:
                    log.error("backfill chat=%s failed: %s", g["chat_id"], type(e).__name__)
                if added:
                    log.warning("backfill chat=%s: %d messages were missing, stored", g["chat_id"], added)
            await asyncio.sleep(BACKFILL_EVERY)

    async def mirror(ev, text):
        # Telethon may deliver our own sends as events before send_message returns their ids: give it a moment.
        await asyncio.sleep(2)
        if ev.id in bridge_sent:
            return
        if not OPERATOR_TOKEN:
            log.info("phone out: user=%s len=%d (not mirrored: no operator token)", ev.chat_id, len(text))
            return
        if not text:
            log.info("phone out: user=%s media without text, not mirrored", ev.chat_id)
            return
        try:
            conv = await cw.mirror(await ev.get_chat(), text, ev.id)
            log.info("phone out: user=%s len=%d -> conversation %s", ev.chat_id, len(text), conv)
        except Exception as e:
            log.error("phone out: user=%s failed: %s", ev.chat_id, e)

    async def deliver(user_id, content, attachments):
        entity = await resolve(user_id)
        await pacer.wait(user_id)
        await client.send_read_acknowledge(entity)
        async with client.action(entity, "typing"):
            await asyncio.sleep(typing_seconds(content))
        if not attachments:
            bridge_sent.add((await client.send_message(entity, content)).id)
            return
        for i, a in enumerate(attachments):
            f = io.BytesIO(await cw.download(a["data_url"]))
            f.name = urlsplit(a["data_url"]).path.rsplit("/", 1)[-1] or "file"
            m = await client.send_file(entity, f, caption=content if i == 0 else None,
                                       voice_note=a.get("file_type") == "audio" and f.name.endswith((".ogg", ".oga")))
            bridge_sent.add(m.id)

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
        if str(p.get("source_id") or "").startswith(MIRROR_PREFIX):
            return web.Response(status=200)  # came from Telegram in the first place
        delivery = request.headers.get("X-Chatwoot-Delivery") or f"msg:{p.get('id')}"
        with db() as c:
            if c.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?)", (delivery, now_iso())).rowcount == 0:
                return web.Response(status=200)
        ident = ((p.get("conversation") or {}).get("meta") or {}).get("sender", {}).get("identifier") or ""
        if not ident.startswith("tg:") or not ident[3:].isdigit():
            log.warning("webhook: message %s has no tg contact", p.get("id"))
            return web.Response(status=200)
        user_id = int(ident[3:])
        first_contact = not allowed(user_id)
        if first_contact and not seen_in_allowed_group(user_id):
            log.warning("webhook: user %s is neither an allowed chat nor a member of an allowed group; not sent",
                        user_id)
            return web.Response(status=200)
        content = (p.get("content") or "").strip()
        attachments = [a for a in (p.get("attachments") or []) if a.get("data_url")]
        if not (content or attachments):
            return web.Response(status=200)
        # Typing takes seconds: answer Chatwoot now, send in order per resident in the background.
        if user_id not in outbox:
            outbox[user_id] = asyncio.Queue()
            asyncio.create_task(sender_loop(user_id, outbox[user_id]))
        outbox[user_id].put_nowait((p, content, attachments, first_contact))
        return web.Response(status=200)

    async def sender_loop(user_id, q):
        while True:
            item = await q.get()
            try:
                await send_one(user_id, *item)
            except Exception as e:  # never let one message stop the resident's queue
                log.error("private out: user=%s failed: %s", user_id, type(e).__name__)

    async def send_one(user_id, p, content, attachments, first_contact):
        try:
            await deliver(user_id, content, attachments)
        except errors.FloodWaitError as e:
            log.error("private out: FLOOD_WAIT %ss (user %s), message %s not sent", e.seconds, user_id, p.get("id"))
            return
        except (errors.PeerFloodError, errors.UserPrivacyRestrictedError) as e:
            log.error("private out: user=%s refused by Telegram: %s", user_id, type(e).__name__)
            return
        if first_contact:
            allow(user_id, "outreach", "")
            conv = (p.get("conversation") or {}).get("id")
            src = ((p.get("conversation") or {}).get("contact_inbox") or {}).get("source_id")
            if conv and src:
                cw._remember(user_id, src, conv)
        log.info("private out: user=%s len=%d attachments=%d%s", user_id, len(content), len(attachments),
                 " (first contact)" if first_contact else "")

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
    asyncio.create_task(backfill())
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
