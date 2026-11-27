#!/usr/bin/env python3
"""The chairman's обзвон over a list: one pass, one call at a time, safe to rerun (once a day, or after lunch).

    python3 -I voice/obzvon.py <list.csv> <campaign>        on the VM, in /srv/salesbot/stack
    python3 -I voice/obzvon.py <list.csv> <campaign> --dry  only print what it would do

The list: one person per line, `phone,name` (name optional; a header line and # comments are skipped). It holds real
numbers, so it lives on the VM outside the repo (e.g. ~/obzvon/votes.csv, chmod 600). Only people who agreed.

Per person, from the voice agent's records (GET /outbound):
  - a final outcome (voted, will_vote, send_link, paid, will_pay, dispute, refused, not_owner, wrong_person): skip;
  - no answer, hung up without an outcome, or `callback`: call again, but not sooner than RETRY_HOURS after the last
    attempt and at most MAX_ATTEMPTS times in all. The time someone asked for is printed for the operator: the agent
    does not parse «после шести» into a schedule.
Calls are placed only while no other call is on (the second slot stays for incoming calls), 09:00-21:00 Astana, and
below the agent's daily cap. Each call is waited out before the next. The printout masks phone numbers.
"""
import csv, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

VOICE = os.environ.get("VOICE_URL", "http://127.0.0.1:8090")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
RETRY_HOURS = float(os.environ.get("RETRY_HOURS", "3"))
GAP_SECS = 20  # between two calls
FINAL = {"voted", "will_vote", "send_link", "paid", "will_pay", "dispute", "refused", "not_owner", "wrong_person"}
HERE = Path(__file__).resolve().parent


def get(path, **q):
    with urllib.request.urlopen(f"{VOICE}{path}?{urllib.parse.urlencode(q)}", timeout=10) as r:
        return json.load(r)


def masked(phone):
    return "+" + phone[:4] + "…" + phone[-2:]


def read_list(path):
    people = []
    for row in csv.reader(open(path, encoding="utf-8")):
        if not row or row[0].lstrip().startswith("#"):
            continue
        phone = re.sub(r"\D", "", row[0])
        if not 11 <= len(phone) <= 15:
            continue  # the header or a bad line
        people.append((phone, (row[1].strip() if len(row) > 1 else "")))
    return people


def decide(st, now):
    """('skip'|'later'|'call', why) for one person's /outbound status."""
    tries = st["attempts"]
    last = tries[-1] if tries else None
    done = next((t for t in reversed(tries) if t["result"] in FINAL), None)
    if done:
        return "skip", f"итог: {done['result']}"
    if len(tries) >= MAX_ATTEMPTS:
        return "skip", f"{len(tries)} попытки без итога, передать председателю"
    if last and now - last["at"] < RETRY_HOURS * 3600:
        return "later", f"последняя попытка {int((now - last['at']) / 60)} мин назад"
    if last and last["result"] == "callback":
        return "call", f"просил перезвонить: {last['callback'] or 'время не назвал'}"
    return "call", "повтор: не ответил" if last else "первый звонок"


def wait_idle(limit=15 * 60):
    t = time.time()
    while time.time() - t < limit:
        if get("/healthz")["active"] == 0:
            return True
        time.sleep(5)
    return False


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    if len(args) != 2:
        sys.exit(__doc__)
    path, campaign = args
    people = read_list(path)
    print(f"список: {len(people)} человек, повод {campaign}" + (" (dry)" if dry else ""))
    placed = 0
    for phone, name in people:
        st = get("/outbound", campaign=campaign, peer=phone)
        what, why = decide(st, time.time())
        print(f"{masked(phone)} {name or '-'}: {why}" + ("" if what == "call" else f" -> {what}"))
        if what != "call" or dry:
            continue
        if st["today"] >= st["daily"]:
            print(f"дневной лимит звонков ({st['daily']}) исчерпан, остальные завтра")
            break
        if not wait_idle():
            print("линия занята 15 минут, остановка")
            break
        r = subprocess.run([str(HERE / "dial.sh"), phone, campaign, name], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 3:  # quiet hours
            print(out)
            break
        if not out.startswith("placed"):
            print(f"  не удалось позвонить: {out[:200]}")
            continue
        placed += 1
        time.sleep(10)  # the agent's slot opens once the connector dials the stream
        wait_idle()
        st = get("/outbound", campaign=campaign, peer=phone)
        t = st["attempts"][-1] if st["attempts"] else {}
        print(f"  -> {t.get('result') or ('без итога' if t.get('talked') else 'не ответил')}")
        time.sleep(GAP_SECS)
    print(f"позвонили: {placed}")


if __name__ == "__main__":
    main()
