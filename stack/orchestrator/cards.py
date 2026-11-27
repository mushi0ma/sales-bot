"""A resident's card in Twenty from one event: the person, their house, the opportunity and its stage, the timeline.

The event (the contract every producer writes, see events/outbox.py):
    {"v": 1, "id": uuid, "type": "need.detected", "at": unix seconds, "source": "group-watcher",
     "key": "cw:<chatwoot contact id>",
     "resident": {"cw_contact_id": 12, "name": "Айгуль", "phone": "77011234567"},
     "house": "ЖК Тестовый, дом 12" | null,
     "data": {...the type's own fields}}
The person is keyed by the Chatwoot contact id: one card per contact. A WhatsApp and a Telegram contact of one
person stay two cards until an operator merges them in Twenty (merge is built in), never by name.
"""
import json, logging, os, time, urllib.error, urllib.request
from datetime import datetime, timezone

import funnel

log = logging.getLogger("cards")
TWENTY = os.environ.get("TWENTY_URL", "http://twenty:3000").rstrip("/")
TWENTY_KEY = os.environ.get("TWENTY_API_KEY", "")
CW_PUBLIC = os.environ.get("CW_PUBLIC_URL", "").rstrip("/")
CW_ACCOUNT_ID = os.environ.get("CW_ACCOUNT_ID", "1")


class TwentyError(RuntimeError):
    pass


def gql(query, variables=None):
    req = urllib.request.Request(f"{TWENTY}/graphql", method="POST",
                                 data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TWENTY_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise TwentyError(f"HTTP {e.code}: {e.read()[:200]!r}") from None
    if b.get("errors"):
        raise TwentyError("; ".join(e.get("message", "")[:200] for e in b["errors"]))
    return b["data"]


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def phone_field(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return {"primaryPhoneNumber": digits[1:], "primaryPhoneCallingCode": "+7", "primaryPhoneCountryCode": "KZ"}
    if 10 <= len(digits) <= 15:
        return {"primaryPhoneNumber": digits, "primaryPhoneCallingCode": "", "primaryPhoneCountryCode": ""}
    return None


# ---------------------------------------------------------------- lookups

def find_person(cw_contact_id):
    d = gql("""query($id: Float) { people(filter: {chatwootContactId: {eq: $id}}, first: 1) {
               edges { node { id companyId optOut residentRole phones { primaryPhoneNumber } } } } }""", {"id": float(cw_contact_id)})
    e = d["people"]["edges"]
    return e[0]["node"] if e else None


def company_id(name):
    if not name:
        return None
    name = name.strip()[:120]
    d = gql("""query($n: String) { companies(filter: {name: {eq: $n}}, first: 1) { edges { node { id } } } }""",
            {"n": name})
    if d["companies"]["edges"]:
        return d["companies"]["edges"][0]["node"]["id"]
    return gql("mutation($d: CompanyCreateInput!) { createCompany(data: $d) { id } }", {"d": {"name": name}})[
        "createCompany"]["id"]


def opportunities(person_id):
    d = gql("""query($p: UUID) { opportunities(filter: {pointOfContactId: {eq: $p}}, first: 50) {
               edges { node { id stage service createdAt } } } }""", {"p": person_id})
    return [e["node"] for e in d["opportunities"]["edges"]]


# ---------------------------------------------------------------- writes

def upsert_person(ev, channel):
    r = ev.get("resident") or {}
    cid = int(r["cw_contact_id"])
    house = company_id(ev.get("house"))
    p = find_person(cid)
    data = {"lastTouchAt": iso(ev.get("at") or time.time())}
    if channel:
        data["lastChannel"] = channel
    if house and not (p and p.get("companyId")):
        data["companyId"] = house
    if ev["type"] == "optout":
        data["optOut"] = True
    role = (ev.get("data") or {}).get("role")
    if role and (not p or p.get("residentRole") in (None, "UNKNOWN")):
        data["residentRole"] = role
    if p and phone_field(r.get("phone")) and not ((p.get("phones") or {}).get("primaryPhoneNumber")):
        data["phones"] = phone_field(r.get("phone"))
    if p:
        gql("mutation($id: UUID!, $d: PersonUpdateInput!) { updatePerson(id: $id, data: $d) { id } }",
            {"id": p["id"], "d": data})
        return p["id"], house or p.get("companyId")
    data.update({"chatwootContactId": cid, "name": {"firstName": (r.get("name") or f"Контакт {cid}")[:80],
                                                   "lastName": ""},
                 "residentRole": data.get("residentRole", "UNKNOWN"), "optOut": data.get("optOut", False)})
    if phone_field(r.get("phone")):
        data["phones"] = phone_field(r.get("phone"))
    if CW_PUBLIC:
        data["chatwootLink"] = {"primaryLinkLabel": "Chatwoot",
                                "primaryLinkUrl": f"{CW_PUBLIC}/app/accounts/{CW_ACCOUNT_ID}/contacts/{cid}"}
    pid = gql("mutation($d: PersonCreateInput!) { createPerson(data: $d) { id } }", {"d": data})["createPerson"]["id"]
    log.info("card: new person for contact %s", cid)
    return pid, house


def move_opportunity(ev, person_id, company, channel):
    """Apply the event's stage; returns (opportunity id or None, old stage, new stage)."""
    target = funnel.stage_for(ev)
    d = ev.get("data") or {}
    service = (d.get("service") or "").upper() or None
    opps = opportunities(person_id)
    if ev["type"] == "optout":
        for o in opps:
            if o["stage"] not in ("LOST", "CUSTOMER"):
                gql("mutation($id: UUID!, $d: OpportunityUpdateInput!) { updateOpportunity(id: $id, data: $d) { id } }",
                    {"id": o["id"], "d": {"stage": "LOST", "lostReason": "Отказался от сообщений («стоп»)"}})
        return None, None, "LOST" if opps else None
    if target is None:
        return None, None, None
    o = funnel.pick_opportunity(opps, service)
    if o is None:
        name = d.get("service_name") or ("Заявка со звонка" if ev["type"] == "call.ended" else "Интерес к e-KNOT")
        data = {"name": name[:100], "stage": target, "pointOfContactId": person_id,
                "service": service or "OTHER", "source": channel}
        if company:
            data["companyId"] = company
        if d.get("quote"):
            data["needQuote"] = d["quote"][:400]
        if d.get("cw_conversation_id"):
            data["chatwootConversationId"] = int(d["cw_conversation_id"])
        oid = gql("mutation($d: OpportunityCreateInput!) { createOpportunity(data: $d) { id } }", {"d": data})[
            "createOpportunity"]["id"]
        return oid, None, target
    new = funnel.advance(o["stage"], target)
    if new != o["stage"]:
        upd = {"stage": new}
        if d.get("cw_conversation_id") and ev["type"] in ("offer.sent", "dm.replied"):
            upd["chatwootConversationId"] = int(d["cw_conversation_id"])
        gql("mutation($id: UUID!, $d: OpportunityUpdateInput!) { updateOpportunity(id: $id, data: $d) { id } }",
            {"id": o["id"], "d": upd})
    return o["id"], o["stage"], new


def note_body(ev, old, new):
    d = ev.get("data") or {}
    lines = []
    if new and new != old:
        lines.append(f"**Стадия:** {old or '—'} → {new}")
    for k, label in (("quote", "Цитата"), ("text", "Сообщение"), ("note", "Заметка"), ("group", "Группа"),
                     ("outcome_label", "Итог"), ("callback", "Когда перезвонить"), ("secs", "Длительность, с")):
        if d.get(k):
            lines.append(f"**{label}:** {str(d[k])[:600]}")
    if d.get("lead"):
        lead = d["lead"]
        lines.append("**Заявка:** " + ", ".join(str(lead[k]) for k in ("role", "house", "apartments", "wants", "name")
                                                if lead.get(k)))
    if d.get("cw_conversation_id") and CW_PUBLIC:
        lines.append(f"[Диалог в Chatwoot]({CW_PUBLIC}/app/accounts/{CW_ACCOUNT_ID}/conversations/"
                     f"{d['cw_conversation_id']})")
    if d.get("transcript"):
        lines += ["", "**Стенограмма:**", "", str(d["transcript"])[:6000]]
    lines.append(f"\n_{ev['source']} · {ev['type']} · {ev['id']}_")
    return "\n\n".join(l for l in lines if l is not None)


def add_note(ev, person_id, opp_id, old, new):
    nid = gql("mutation($d: NoteCreateInput!) { createNote(data: $d) { id } }",
              {"d": {"title": funnel.note_title(ev)[:140], "bodyV2": {"markdown": note_body(ev, old, new)}}})[
        "createNote"]["id"]
    gql("mutation($d: NoteTargetCreateInput!) { createNoteTarget(data: $d) { id } }",
        {"d": {"noteId": nid, "targetPersonId": person_id}})
    if opp_id:
        gql("mutation($d: NoteTargetCreateInput!) { createNoteTarget(data: $d) { id } }",
            {"d": {"noteId": nid, "targetOpportunityId": opp_id}})


def add_callback_task(ev, person_id):
    d = ev.get("data") or {}
    tid = gql("mutation($d: TaskCreateInput!) { createTask(data: $d) { id } }",
              {"d": {"title": f"Перезвонить: {d.get('callback') or 'время не названо'}"[:140], "status": "TODO",
                     "bodyV2": {"markdown": f"Повод: {d.get('campaign_title') or d.get('campaign') or 'звонок'}. "
                                            "Повторный звонок по списку: voice/obzvon.py."}}})["createTask"]["id"]
    gql("mutation($d: TaskTargetCreateInput!) { createTaskTarget(data: $d) { id } }",
        {"d": {"taskId": tid, "targetPersonId": person_id}})


def apply(ev):
    """One event onto the card. Returns a short summary for the task's output."""
    channel = funnel.channel_for(ev)
    person_id, company = upsert_person(ev, channel)
    opp_id, old, new = move_opportunity(ev, person_id, company, channel)
    d = ev.get("data") or {}
    if ev["type"] == "call.ended" and d.get("campaign") and d.get("outcome"):
        gql("mutation($id: UUID!, $d: PersonUpdateInput!) { updatePerson(id: $id, data: $d) { id } }",
            {"id": person_id, "d": {"lastOutcome": f"{d.get('campaign_title') or d['campaign']}: "
                                                   f"{d.get('outcome_label') or d['outcome']}"[:200]}})
    add_note(ev, person_id, opp_id, old, new)
    if ev["type"] == "call.ended" and d.get("outcome") == "callback":
        add_callback_task(ev, person_id)
    return {"person": person_id, "opportunity": opp_id, "stage": [old, new]}
