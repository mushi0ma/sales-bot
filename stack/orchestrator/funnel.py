"""The sales funnel as code: which event moves a resident's opportunity to which stage. No I/O here.

Stages (Twenty opportunity.stage, platform/setup.py STAGES), in order:
    NEW -> OFFERED -> REPLIED -> QUALIFIED -> LEAD -> CUSTOMER, and LOST from anywhere.
A card moves only forward: a late or replayed event never pulls it back. LOST is final for automatic events,
except a new explicit request (a lead), which reopens the card.
"""
RANK = {"NEW": 0, "OFFERED": 1, "REPLIED": 2, "QUALIFIED": 3, "LEAD": 4, "CUSTOMER": 5}
REOPENS_LOST = {"LEAD", "CUSTOMER"}

# event type -> the stage it means for the opportunity it concerns (None: no stage change, only the timeline)
EVENT_STAGE = {
    "need.detected": "NEW",
    "offer.sent": "OFFERED",
    "dm.replied": "REPLIED",
    "lead.qualified": "QUALIFIED",
    "lead.handoff": "LEAD",
    "optout": "LOST",
    "call.ended": None,  # a lead on the call makes it LEAD (stage_for below); a campaign call is not a sale
}

CHANNEL = {"need.detected": "WA_GROUP", "offer.sent": "WA_DM", "dm.replied": "WA_DM", "lead.qualified": "WA_DM",
           "lead.handoff": "WA_DM", "optout": None}


def stage_for(event):
    """The target stage of an event, or None."""
    if event["type"] == "call.ended":
        return "LEAD" if (event.get("data") or {}).get("lead") else None
    return EVENT_STAGE.get(event["type"])


def channel_for(event):
    if event["type"] == "call.ended":
        return "CALL_OUT" if (event.get("data") or {}).get("direction") == "out" else "CALL_IN"
    ch = CHANNEL.get(event["type"])
    if ch and (event.get("data") or {}).get("channel") == "telegram":
        return "TELEGRAM"
    return ch


def advance(current, target):
    """The stage after applying `target` to `current` (both may be None)."""
    if target is None:
        return current
    if current is None:
        return target
    if current == "LOST":
        return target if target in REOPENS_LOST else "LOST"
    if target == "LOST":
        return "LOST"
    return target if RANK[target] > RANK[current] else current


def pick_opportunity(opps, service):
    """The opportunity an event concerns: the same service, else (no service named) the latest open one.
    opps: [{"id", "stage", "service", "createdAt"}]. Returns one of them or None (create a new one)."""
    if service:
        same = [o for o in opps if o.get("service") == service]
        return max(same, key=lambda o: o.get("createdAt") or "") if same else None
    open_ = [o for o in opps if o.get("stage") not in ("LOST", "CUSTOMER")]
    return max(open_, key=lambda o: o.get("createdAt") or "") if open_ else None


def note_title(event):
    d = event.get("data") or {}
    t = event["type"]
    if t == "need.detected":
        return f"Потребность в группе: {d.get('service_name') or d.get('service') or 'услуга'}"
    if t == "offer.sent":
        return f"Отправлено предложение в личку: {d.get('service_name') or d.get('service') or ''}".rstrip(": ")
    if t == "dm.replied":
        return "Ответил в личке"
    if t == "lead.handoff":
        return "Заявка: продавец передал менеджеру"
    if t == "lead.qualified":
        return "Квалифицирован"
    if t == "optout":
        return "Отказался от сообщений («стоп»)"
    if t == "call.ended":
        kind = "Исходящий звонок" if d.get("direction") == "out" else "Входящий звонок"
        if d.get("campaign_title"):
            kind += f" ({d['campaign_title']})"
        return kind + (f": {d['outcome_label']}" if d.get("outcome_label") else "")
    return t
