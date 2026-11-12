#!/usr/bin/env python3
"""Telegram -> Chatwoot bridge for the sales-bot TEST stack (long polling instead of a webhook).

Chatwoot's Telegram inbox registers a webhook at FRONTEND_URL, and Telegram only calls public HTTPS URLs; the VM has
none. This service polls getUpdates and hands each update to Chatwoot's own webhook route inside the docker network
(POST http://cw-rails:3000/webhooks/telegram/<token>), exactly the body Telegram would have sent. Replies go out the
normal way: Chatwoot calls api.telegram.org sendMessage itself.

  - On start (and whenever Telegram answers 409 because a webhook is set again) it calls deleteWebhook.
  - An update is confirmed to Telegram (offset) only after Chatwoot answered 2xx, so nothing is lost on a restart.
  - Logs carry update ids and types only; never the token, never message text.
"""
import json, os, sys, time, urllib.error, urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHATWOOT = os.environ.get("CHATWOOT_URL", "http://cw-rails:3000").rstrip("/")
API = f"https://api.telegram.org/bot{TOKEN}"
POLL_TIMEOUT = 50


def log(msg):
    sys.stderr.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")


def tg(method, params=None, timeout=15):
    req = urllib.request.Request(f"{API}/{method}", json.dumps(params or {}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:  # never str(e) of a URL-carrying error: the URL holds the token
        return 0, {"description": type(e).__name__}


def to_chatwoot(update):
    req = urllib.request.Request(f"{CHATWOOT}/webhooks/telegram/{TOKEN}", json.dumps(update).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def drop_webhook():
    st, b = tg("deleteWebhook", {"drop_pending_updates": False})
    log(f"deleteWebhook: HTTP {st} ok={b.get('ok')}")


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    st, me = tg("getMe")
    if st != 200:
        sys.exit(f"getMe failed: HTTP {st} {me.get('description', '')}")
    log(f"bot @{me['result'].get('username')} -> {CHATWOOT}/webhooks/telegram/<token>")
    drop_webhook()
    offset, backoff = None, 1
    while True:
        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        st, b = tg("getUpdates", params, timeout=POLL_TIMEOUT + 10)
        if st == 409:  # a webhook got set again (e.g. the inbox was saved in Chatwoot)
            log("getUpdates 409: webhook is set, removing it")
            drop_webhook(); time.sleep(1); continue
        if st != 200:
            log(f"getUpdates failed: HTTP {st} {b.get('description', '')}; retry in {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 60); continue
        backoff = 1
        for u in b.get("result", []):
            kind = next((k for k in u if k != "update_id"), "?")
            for attempt in range(5):
                code = to_chatwoot(u)
                if 200 <= code < 300:
                    break
                log(f"update {u['update_id']} ({kind}) -> chatwoot HTTP {code}; retry")
                time.sleep(2 * (attempt + 1))
            else:
                log(f"update {u['update_id']} ({kind}) dropped after 5 attempts")
            log(f"update {u['update_id']} ({kind}) -> chatwoot HTTP {code}")
            offset = u["update_id"] + 1


if __name__ == "__main__":
    main()
