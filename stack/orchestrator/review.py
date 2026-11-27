"""The analyst role: a review of one finished dialog by an LLM judge, in the role of a sales coach and a client
psychologist. What it rates: the agent's answers (relevance, accuracy, discovery of the need, objection handling,
next step, tone, brevity), the techniques it used and whether each one worked (judged by the person's next turn),
the objections, the person (mood at the start and the end, engagement, predicted satisfaction with its drivers,
readiness to buy) and the case (outcome, the reason when lost, closed or not, what to do next).

    WhatsApp DM (seller)  every 10 min: individual conversations quiet for REVIEW_IDLE_MINUTES, reviewed again when
                          new messages came (sweep(), Hatchet cron)
    call                  call.ended with a conversation (review_call(), Hatchet event)

Where the review goes: the reviews table (processed.db, for the report), Langfuse scores on the session
(`dm-<conversation>` / `call-<id>`), the opportunity and the person in Twenty (score fields) and a note
«Разбор диалога» on the card. The judge never changes the funnel stage: stages move by facts (events) only.

CLI, in the orchestrator container:
    python3 review.py dm <conversation id>    the judge's JSON for one DM, writes nothing
    python3 review.py dm <conversation id> --save
    python3 review.py report [days]           averages, techniques that worked, objections, reasons, mistakes
"""
import base64, hashlib, json, logging, os, sqlite3, statistics, sys, threading, time, urllib.error, urllib.request
from collections import Counter

import cards

log = logging.getLogger("review")
E = os.environ.get
CW_URL = E("CW_URL", "http://cw-rails:3000").rstrip("/")
CW_TOKEN = E("CW_API_TOKEN", "")
CW_ACCOUNT_ID = E("CW_ACCOUNT_ID", "1")
LLM_URL = E("LLM_URL", "http://litellm:4000/v1").rstrip("/")
LLM_KEY = E("LLM_KEY", "")
JUDGE_MODEL = E("JUDGE_MODEL", "gemini-3.8-flash-judge")
LANGFUSE = E("LANGFUSE_HOST", "").rstrip("/")
LF_AUTH = base64.b64encode(f'{E("LANGFUSE_PUBLIC_KEY", "")}:{E("LANGFUSE_SECRET_KEY", "")}'.encode()).decode()
IDLE_MINUTES = int(E("REVIEW_IDLE_MINUTES", "45"))
LOOKBACK_DAYS = int(E("REVIEW_LOOKBACK_DAYS", "7"))
PER_SWEEP = int(E("REVIEW_PER_SWEEP", "5"))
CATALOG = json.loads(open(E("CATALOG_PATH", "/app/services.json"), encoding="utf-8").read())["services"]
_cp = E("CAMPAIGNS_PATH", "/app/campaigns.json")
CAMPAIGNS = json.loads(open(_cp, encoding="utf-8").read()) if os.path.exists(_cp) else {}

DB = sqlite3.connect(E("DB_PATH", "/data/processed.db"), check_same_thread=False, isolation_level=None)
# kind: dm | call | dm-skip (read, nothing new to judge); fp: a hash of the dialog lines the judge saw
DB.execute("""CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
  ref TEXT NOT NULL, contact_id INTEGER, fp TEXT, at REAL NOT NULL, score REAL, csat INTEGER,
  outcome TEXT, result TEXT NOT NULL)""")
DB.execute("CREATE INDEX IF NOT EXISTS reviews_ref ON reviews(kind, ref, id)")
LOCK = threading.Lock()

# ---------------------------------------------------------------- the rubric

AGENT = ["relevance", "accuracy", "discovery", "objection_handling", "next_step", "tone", "brevity"]
AGENT_RU = {"relevance": "по делу", "accuracy": "точность фактов", "discovery": "выявление потребности",
            "objection_handling": "работа с возражениями", "next_step": "следующий шаг", "tone": "тон",
            "brevity": "краткость"}
TECHNIQUES = {"personalization": "обращение к ситуации человека", "question_discovery": "уточняющий вопрос",
              "value_framing": "выгода на языке человека", "social_proof": "пример других домов",
              "objection_reframe": "переформулировка возражения", "empathy": "сочувствие, признание проблемы",
              "free_trial_demo": "демо, пробный период", "urgency": "срочность, срок",
              "handoff_offer": "предложение связать с менеджером", "low_commitment_cta": "лёгкий следующий шаг",
              "other": "другое"}
OBJECTIONS = {"price": "цена", "already_have": "уже есть решение", "not_decision_maker": "не решает сам",
              "no_time": "нет времени", "distrust": "недоверие", "need_approval": "нужно согласовать с жильцами",
              "no_need": "нет потребности", "other": "другое"}
OUTCOMES = {"LEAD": "заявка", "INTERESTED": "интерес, не закрыт", "TASK_DONE": "задача звонка выполнена",
            "NO_INTEREST": "не интересно", "OPTED_OUT": "отказ от сообщений", "WRONG_TARGET": "не тот адресат",
            "UNRESOLVED": "вопрос не решён", "OTHER": "другое"}
SENTIMENT = {"POSITIVE": "позитивный", "NEUTRAL": "нейтральный", "NEGATIVE": "негативный"}
READINESS = {"NONE": "нет", "LOW": "низкая", "MEDIUM": "средняя", "HIGH": "высокая"}
ROLES = ["CHAIRMAN", "COUNCIL", "UK", "OWNER", "TENANT", "UNKNOWN"]


def schema():
    s = lambda **kw: {"type": "string", **kw}
    i = {"type": "integer"}
    return {"type": "object", "required": ["analysis", "agent", "techniques", "objections", "client", "case",
                                           "mistakes", "lesson"],
            "properties": {
        "analysis": s(description="Сначала рассуждение: что хотел человек, что сделал агент, где удачно, где нет; "
                                  "со ссылками на номера реплик"),
        "agent": {"type": "object", "required": AGENT, "properties": {k: i for k in AGENT}},
        "techniques": {"type": "array", "items": {"type": "object", "required": ["name", "turn", "effect"],
                       "properties": {"name": s(enum=list(TECHNIQUES)), "turn": i,
                                      "effect": s(enum=["positive", "neutral", "negative"])}}},
        "objections": {"type": "array", "items": {"type": "object", "required": ["type", "turn", "handled"],
                       "properties": {"type": s(enum=list(OBJECTIONS)), "turn": i, "handled": {"type": "boolean"}}}},
        "client": {"type": "object",
                   "required": ["sentiment_start", "sentiment_end", "engagement", "predicted_csat", "csat_drivers",
                                "readiness", "role"],
                   "properties": {"sentiment_start": s(enum=list(SENTIMENT)), "sentiment_end": s(enum=list(SENTIMENT)),
                                  "engagement": i, "predicted_csat": i,
                                  "csat_drivers": {"type": "array", "items": s()},
                                  "readiness": s(enum=list(READINESS)), "role": s(enum=ROLES)}},
        "case": {"type": "object", "required": ["outcome", "lost_reason", "closed", "closure_quality", "service_fit",
                                                "summary", "next_best_action"],
                 "properties": {"outcome": s(enum=list(OUTCOMES)), "lost_reason": s(enum=list(OBJECTIONS) + ["none"]),
                                "closed": {"type": "boolean"}, "closure_quality": i, "service_fit": i,
                                "summary": s(), "next_best_action": s()}},
        "mistakes": {"type": "array", "items": s()},
        "lesson": s(description="Одно конкретное улучшение для промпта или плейбука агента"),
    }}


JUDGE_PROMPT = """Ты старший тренер отдела продаж и психолог клиентского опыта. Разбираешь один завершённый диалог
ИИ-агента компании e-KNOT (сервисы для ОСИ и жильцов многоквартирных домов в Казахстане) с человеком и честно
оцениваешь работу агента, состояние человека и итог. Оценка нужна, чтобы агент учился: хвалить без причины вредно.

Что за диалог: {kind}
Цель агента в этом диалоге: {goal}
Что известно до разбора (факты системы, не оценка): {facts}

Услуги e-KNOT (единственные факты, которые агент вправе называть):
{catalog}

Шкала всех оценок 1–5: 1 вредно (человек раздражён, ушёл, факты выдуманы), 2 слабо, 3 приемлемо, 4 хорошо,
5 как лучший живой продавец. 0 значит «не применимо» (например, возражений не было: objection_handling = 0;
агенту не нужно было ничего выяснять: discovery = 0). Длинная реплика не лучше короткой. Не придумывай того, чего
нет в стенограмме; каждую ошибку и приём привязывай к номеру реплики [N].

- agent: relevance (отвечал на то, что спросили), accuracy (только факты из услуг выше и из повода; выдумка = 1),
  discovery (выяснил роль, дом, проблему, сроки вопросами, по одному), objection_handling, next_step (договорился
  о конкретном шаге: заявка, звонок менеджера, ссылка), tone (на «вы», без давления и канцелярита, без «я бот»),
  brevity (коротко, по одной мысли).
- techniques: приёмы агента; effect по следующей реплике человека (positive: ответил по делу, заинтересовался;
  negative: раздражился, замолчал, отказался).
- objections: возражения человека и закрыл ли их агент.
- client: настроение в начале и в конце, вовлечённость (1 односложно, 5 сам задаёт вопросы), predicted_csat:
  как сам человек оценил бы разговор, csat_drivers: 1–3 причины этой оценки; readiness: готовность купить или
  оставить заявку; role: кто он, если понятно из разговора.
- case: outcome; lost_reason (если не заявка: главная причина, иначе none); closed (кейс доведён до конца: заявка
  принята, вопрос решён или ясный отказ); closure_quality; service_fit (та ли услуга предложена, 0 если не
  предлагалась); summary (2–3 предложения для менеджера); next_best_action (что сделать менеджеру сейчас).
- mistakes: конкретные ошибки агента с номером реплики, не больше 5; пусто, если ошибок нет.
- lesson: одно правило, которое улучшит агента в похожих диалогах.
"""


def catalog_text():
    return "\n".join(f"- {s['id']}: {s['name']}. {s.get('what', '')[:220]}" for s in CATALOG)


def clamp(v, lo=0, hi=5):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return 0


def normalize(r):
    """Clamp the numbers, drop unknown enums, compute the agent's score (mean of the applicable criteria)."""
    a = {k: clamp((r.get("agent") or {}).get(k)) for k in AGENT}
    r["agent"] = a
    applicable = [v for v in a.values() if v > 0]
    r["score"] = round(statistics.mean(applicable), 2) if applicable else None
    c = r.get("client") or {}
    c["engagement"], c["predicted_csat"] = clamp(c.get("engagement"), 1), clamp(c.get("predicted_csat"), 1)
    r["client"] = c
    case = r.get("case") or {}
    case["closure_quality"], case["service_fit"] = clamp(case.get("closure_quality")), clamp(case.get("service_fit"))
    if case.get("outcome") not in OUTCOMES:
        case["outcome"] = "OTHER"
    r["case"] = case
    r["techniques"] = [t for t in r.get("techniques") or [] if t.get("name") in TECHNIQUES][:12]
    r["objections"] = [o for o in r.get("objections") or [] if o.get("type") in OBJECTIONS][:8]
    r["mistakes"] = [str(m)[:300] for m in r.get("mistakes") or []][:5]
    return r


def judge(kind, goal, facts, transcript, session):
    system = JUDGE_PROMPT.format(kind=kind, goal=goal, facts=facts or "нет", catalog=catalog_text())
    body = {"model": JUDGE_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": "Стенограмма, реплики пронумерованы:\n\n" + transcript}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": schema(),
                                                                       "strict": True}},
            "metadata": {"session_id": session, "trace_name": "review", "tags": ["review"]}}
    st, b = http("POST", f"{LLM_URL}/chat/completions", body, {"Authorization": f"Bearer {LLM_KEY}"}, timeout=120)
    if st != 200:
        raise RuntimeError(f"judge HTTP {st}: {str(b)[:200]}")
    return normalize(json.loads(b["choices"][0]["message"]["content"] or "{}"))


# ---------------------------------------------------------------- sources

def http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)[:200]


def cw(method, path, body=None):
    return http(method, f"{CW_URL}/api/v1/accounts/{CW_ACCOUNT_ID}{path}", body, {"api_access_token": CW_TOKEN})


_me = []


def my_user_id():
    """The Chatwoot user of CW_API_TOKEN (group-watcher's automation): its public messages are the assistant's
    (the offer, the opt-out reply); its private notes (call notes, these reviews) are not part of the dialog."""
    if not _me:
        st, b = http("GET", f"{CW_URL}/api/v1/profile", headers={"api_access_token": CW_TOKEN})
        _me.append((b or {}).get("id") if st == 200 and isinstance(b, dict) else None)
    return _me[0]


def messages(conv_id, cap=300):
    out, before = [], None
    while len(out) < cap:
        st, b = cw("GET", f"/conversations/{conv_id}/messages" + (f"?before={before}" if before else ""))
        page = (b or {}).get("payload") or [] if st == 200 and isinstance(b, dict) else []
        if not page:
            break
        out = page + out
        before = page[0]["id"]
        if len(page) < 20:
            break
    return sorted(out, key=lambda m: m["id"])


def dialog_lines(msgs):
    """[(who, text)] of the dialog itself, oldest first; who: Жилец, Помощник, Менеджер, Заметка помощника."""
    me = my_user_id()
    lines = []
    for m in msgs:
        sender = m.get("sender") or {}
        text = (m.get("content") or "").strip()
        if not text or m.get("message_type") == 2:
            continue
        if m.get("private"):
            if sender.get("type") == "agent_bot":
                lines.append(("Заметка помощника менеджеру", text))
            continue
        if m.get("message_type") == 0:
            lines.append(("Жилец", text))
        elif sender.get("type") == "agent_bot" or sender.get("id") == me or m.get("message_type") == 3:
            lines.append(("Помощник", text))
        else:
            lines.append(("Менеджер", text))
    return lines


def numbered(lines, limit=14000):
    text = "\n".join(f"[{i}] {who}: {t[:1200]}" for i, (who, t) in enumerate(lines, 1))
    return text[-limit:]


def reviewable(lines):
    return (any(w == "Жилец" for w, _ in lines) and any(w == "Помощник" for w, _ in lines) and len(lines) >= 2)


# ---------------------------------------------------------------- outputs

def last_review(ref):
    """(fp, at) of the latest look at this DM, a review or a skip."""
    with LOCK:
        return DB.execute("SELECT fp, at FROM reviews WHERE kind IN ('dm','dm-skip') AND ref=? ORDER BY id DESC "
                          "LIMIT 1", (str(ref),)).fetchone()


def mark_seen(ref, fp):
    with LOCK:
        DB.execute("INSERT INTO reviews (kind, ref, fp, at, result) VALUES ('dm-skip',?,?,?,'{}')",
                   (str(ref), fp, time.time()))


def save(kind, ref, contact_id, fp, r):
    with LOCK:
        DB.execute("INSERT INTO reviews (kind, ref, contact_id, fp, at, score, csat, outcome, result) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (kind, str(ref), contact_id, fp, time.time(), r["score"],
                                                  r["client"]["predicted_csat"], r["case"]["outcome"],
                                                  json.dumps(r, ensure_ascii=False)))


def to_langfuse(session, r):
    if not (LANGFUSE and LF_AUTH):
        return
    scores = [("agent_score", r["score"], "NUMERIC"), ("predicted_csat", r["client"]["predicted_csat"], "NUMERIC"),
              ("engagement", r["client"]["engagement"], "NUMERIC"),
              ("closure_quality", r["case"]["closure_quality"] or None, "NUMERIC"),
              ("outcome", r["case"]["outcome"], "CATEGORICAL"),
              ("sentiment_end", r["client"].get("sentiment_end"), "CATEGORICAL"),
              ("case_closed", 1 if r["case"].get("closed") else 0, "BOOLEAN")]
    scores += [(f"agent.{k}", v, "NUMERIC") for k, v in r["agent"].items() if v]
    for name, value, kind in scores:
        if value is None:
            continue
        st, b = http("POST", f"{LANGFUSE}/api/public/scores",
                     {"sessionId": session, "name": name, "value": value, "dataType": kind, "source": "API",
                      "comment": r["case"].get("summary", "")[:500] if name == "agent_score" else None},
                     {"Authorization": f"Basic {LF_AUTH}"}, timeout=15)
        if st not in (200, 201):
            log.warning("langfuse score %s HTTP %s %s", name, st, str(b)[:120])
            return


def note_markdown(r, link):
    a, c, case = r["agent"], r["client"], r["case"]
    lines = [f"**Итог:** {OUTCOMES.get(case['outcome'])}"
             + (f", причина: {OBJECTIONS.get(case.get('lost_reason'))}" if case.get("lost_reason") in OBJECTIONS else "")
             + (", кейс закрыт" if case.get("closed") else ", кейс не закрыт"),
             f"**Кратко:** {case.get('summary', '')}",
             f"**Что сделать сейчас:** {case.get('next_best_action', '')}",
             f"**Оценка агента:** {r['score']} из 5 ("
             + ", ".join(f"{AGENT_RU[k]} {v}" for k, v in a.items() if v) + ")",
             f"**Человек:** настроение {SENTIMENT.get(c.get('sentiment_start'), '?')} → "
             f"{SENTIMENT.get(c.get('sentiment_end'), '?')}, вовлечённость {c['engagement']}, удовлетворённость "
             f"(прогноз) {c['predicted_csat']} из 5: {'; '.join(c.get('csat_drivers') or [])}; готовность "
             f"{READINESS.get(c.get('readiness'), '?')}"]
    if r["techniques"]:
        mark = {"positive": "сработал", "neutral": "без эффекта", "negative": "навредил"}
        lines.append("**Приёмы:** " + "; ".join(f"[{t['turn']}] {TECHNIQUES[t['name']]}: {mark.get(t['effect'], '?')}"
                                                for t in r["techniques"]))
    if r["objections"]:
        lines.append("**Возражения:** " + "; ".join(f"[{o['turn']}] {OBJECTIONS[o['type']]}: "
                                                    f"{'закрыто' if o['handled'] else 'не закрыто'}"
                                                    for o in r["objections"]))
    if r["mistakes"]:
        lines.append("**Ошибки агента:** " + "; ".join(r["mistakes"]))
    lines.append(f"**Урок для агента:** {r.get('lesson', '')}")
    if link:
        lines.append(link)
    lines.append("\n_Разбор сделал ИИ-аналитик (orchestrator/review.py)._")
    return "\n\n".join(lines)


def to_card(contact, r, conv_id, kind, session, at):
    if not cards.TWENTY_KEY or not contact.get("id"):
        return
    ev = {"type": "review", "id": session, "source": "review", "at": at,
          "resident": {"cw_contact_id": contact["id"], "name": contact.get("name"), "phone": contact.get("phone")},
          "house": None, "data": {"role": r["client"].get("role") if r["client"].get("role") != "UNKNOWN" else None}}
    person_id, _ = cards.upsert_person(ev, None)
    cards.gql("mutation($id: UUID!, $d: PersonUpdateInput!) { updatePerson(id: $id, data: $d) { id } }",
              {"id": person_id, "d": {"lastCsat": r["client"]["predicted_csat"],
                                      "lastSentiment": r["client"].get("sentiment_end") or "NEUTRAL"}})
    opps = cards.opportunities(person_id)
    # Only the opportunity of this very conversation: a guess would score one service with another's dialog.
    opp = next((o for o in opps if conv_id and o.get("chatwootConversationId") == conv_id), None)
    if opp:
        cards.gql("mutation($id: UUID!, $d: OpportunityUpdateInput!) { updateOpportunity(id: $id, data: $d) { id } }",
                  {"id": opp["id"], "d": {"reviewScore": r["score"], "reviewCsat": r["client"]["predicted_csat"],
                                          "reviewOutcome": r["case"]["outcome"],
                                          "reviewNextStep": r["case"].get("next_best_action", "")[:300],
                                          "reviewedAt": cards.iso(time.time())}})
    link = (f"[Диалог в Chatwoot]({cards.CW_PUBLIC}/app/accounts/{CW_ACCOUNT_ID}/conversations/{conv_id})"
            if conv_id and cards.CW_PUBLIC else "")
    title = f"Разбор {'звонка' if kind == 'call' else 'диалога'}: {r['score']} из 5, {OUTCOMES[r['case']['outcome']]}"
    nid = cards.gql("mutation($d: NoteCreateInput!) { createNote(data: $d) { id } }",
                    {"d": {"title": title[:140], "bodyV2": {"markdown": note_markdown(r, link)}}})["createNote"]["id"]
    for target in ({"targetPersonId": person_id}, {"targetOpportunityId": opp["id"]} if opp else None):
        if target:
            cards.gql("mutation($d: NoteTargetCreateInput!) { createNoteTarget(data: $d) { id } }",
                      {"d": {"noteId": nid, **target}})


def to_chatwoot(conv_id, kind, r):
    """A short private note for the operator, where they work; the full review is on the card in Twenty."""
    if not conv_id:
        return
    c, case = r["client"], r["case"]
    text = (f"📋 Разбор {'звонка' if kind == 'call' else 'диалога'} (ИИ-аналитик): агент {r['score']} из 5, "
            f"итог: {OUTCOMES[case['outcome']]}, удовлетворённость (прогноз) {c['predicted_csat']} из 5, "
            f"настроение в конце: {SENTIMENT.get(c.get('sentiment_end'), '?')}.\n{case.get('summary', '')}\n"
            f"Что сделать: {case.get('next_best_action', '')}")
    if r["mistakes"]:
        text += "\nОшибки агента: " + "; ".join(r["mistakes"][:3])
    st, _ = cw("POST", f"/conversations/{conv_id}/messages", {"content": text[:3000], "private": True})
    if st not in (200, 201):
        log.warning("review note conversation=%s HTTP %s", conv_id, st)


def publish(kind, ref, contact, conv_id, fp, r, at):
    session = f"{'call' if kind == 'call' else 'dm'}-{ref}"
    save(kind, ref, contact.get("id"), fp, r)
    for name, fn in (("langfuse", lambda: to_langfuse(session, r)),
                     ("card", lambda: to_card(contact, r, conv_id, kind, session, at)),
                     ("chatwoot", lambda: to_chatwoot(conv_id, kind, r))):
        try:
            fn()
        except Exception as e:  # the review is saved either way; the next one writes the card again
            log.warning("review %s %s: %s failed: %s", kind, ref, name, str(e)[:200])
    log.info("review %s %s: score=%s csat=%s outcome=%s", kind, ref, r["score"], r["client"]["predicted_csat"],
             r["case"]["outcome"])


# ---------------------------------------------------------------- the two triggers

def stage_of(contact_id):
    try:
        p = cards.find_person(contact_id)
        return ", ".join(f"{o['service']}: {o['stage']}" for o in cards.opportunities(p["id"])) if p else "карточки нет"
    except Exception:
        return "неизвестно"


def review_dm(conv, write=True, prev_fp=None):
    """None when there is nothing (new) to judge: not a dialog yet, or the same lines as the last review."""
    conv_id = conv["id"]
    lines = dialog_lines(messages(conv_id))
    text = numbered(lines)
    fp = hashlib.sha256(text.encode()).hexdigest()[:16]
    if not reviewable(lines) or (prev_fp and fp == prev_fp):
        if write:
            mark_seen(conv_id, fp)
        return None
    contact = (conv.get("meta") or {}).get("sender") or {}
    offer = (conv.get("custom_attributes") or {}).get("salesbot_offer")
    goal = ("Ответить на предложение из группы дома: выяснить роль (председатель, совет, УК, собственник), дом, "
            "проблему; рассказать о подходящей услуге только фактами; закрыть возражения; принять заявку и "
            "передать менеджеру. Не давить; на «стоп» сразу прекратить.")
    facts = f"Стадия карточки: {stage_of(contact['id']) if contact.get('id') else 'нет контакта'}." + (
        f" Повод первого сообщения: {str(offer)[:400]}" if offer else "")
    r = judge("Переписка в WhatsApp: помощник e-KNOT (продавец) и человек из чата дома", goal, facts, text,
              f"dm-{conv_id}")
    if write:
        publish("dm", conv_id, {"id": contact.get("id"), "name": contact.get("name"),
                                "phone": contact.get("phone_number")}, conv_id, fp, r,
                conv.get("last_activity_at") or time.time())
    return r


def review_call(ev):
    """call.ended (the card's event): a call that was answered and has a transcript."""
    d = ev.get("data") or {}
    transcript = d.get("transcript") or ""
    if not d.get("answered") or transcript.count("\n") < 1:
        return None
    lines = [(t.split(": ", 1) + [""])[:2] for t in transcript.split("\n") if t.strip()]
    lines = [("Помощник" if who.startswith("Помощник") else "Жилец", text) for who, text in lines]
    c = CAMPAIGNS.get(d.get("campaign") or "") or {}
    if d.get("direction") == "out" and c:
        kind = f"Исходящий звонок голосового помощника по поручению председателя ОСИ, повод «{c.get('title')}»"
        goal = f"{c.get('goal', '')} {c.get('ask', '')} Факты повода: {' '.join(c.get('facts', []))[:1500]}"
    else:
        kind = "Входящий звонок голосовому помощнику e-KNOT"
        goal = ("Понять, кто звонит и зачем; рассказать об услугах только фактами; записать заявку председателя или "
                "УК (роль, дом, квартиры, что нужно, имя) или ответить на вопрос; вежливо завершить.")
    facts = (f"Итог, записанный агентом: {d.get('outcome_label') or d.get('outcome') or 'нет'}; заявка: "
             f"{json.dumps(d.get('lead'), ensure_ascii=False) if d.get('lead') else 'нет'}; длительность "
             f"{d.get('secs')} с.")
    r = judge(kind, goal, facts, numbered(lines), f"call-{d.get('call_id')}")
    res = ev.get("resident") or {}
    publish("call", d.get("call_id"), {"id": res.get("cw_contact_id"), "name": res.get("name"),
                                       "phone": res.get("phone")}, d.get("cw_conversation_id"), None, r,
            ev.get("at") or time.time())
    return r


def sweep():
    """Individual conversations that went quiet for IDLE_MINUTES within LOOKBACK_DAYS and changed since the last
    review. Returns the ids reviewed."""
    now, done = time.time(), []
    for page in range(1, 5):
        st, b = cw("GET", f"/conversations?status=all&assignee_type=all&page={page}")
        convs = ((b or {}).get("data") or {}).get("payload") or [] if st == 200 and isinstance(b, dict) else []
        for c in convs:
            last = c.get("last_activity_at") or 0
            if c.get("group_type") == "group" or not (now - LOOKBACK_DAYS * 86400 < last < now - IDLE_MINUTES * 60):
                continue
            prev = last_review(c["id"])
            if prev and prev[1] >= last:  # nothing happened since we last looked
                continue
            try:
                if review_dm(c, prev_fp=prev[0] if prev else None) is not None:
                    done.append(c["id"])
            except Exception as e:
                log.warning("review dm %s failed: %s", c["id"], str(e)[:200])
            if len(done) >= PER_SWEEP:
                return done
        if len(convs) < 25:
            break
    return done


# ---------------------------------------------------------------- report

def report(days=7.0):
    since = time.time() - days * 86400
    with LOCK:
        rows = DB.execute("SELECT kind, ref, score, csat, outcome, result FROM reviews WHERE kind IN ('dm','call') "
                          "AND at >= ? ORDER BY id", (since,)).fetchall()
    latest = {}
    for kind, ref, score, csat, outcome, result in rows:  # the last review of each dialog counts
        latest[(kind, ref)] = (score, csat, outcome, json.loads(result))
    if not latest:
        return {"days": days, "dialogs": 0}
    vals = list(latest.values())
    tech = Counter()
    for *_, r in vals:
        for t in r["techniques"]:
            tech[(t["name"], t["effect"])] += 1
    by_tech = {}
    for (name, effect), n in tech.items():
        by_tech.setdefault(TECHNIQUES[name], Counter())[effect] += n
    mean = lambda xs: round(statistics.mean(xs), 2) if xs else None
    return {
        "days": days, "dialogs": len(vals),
        "by_kind": dict(Counter(k for k, _ in latest)),
        "agent_score": mean([s for s, *_ in vals if s]),
        "agent_criteria": {AGENT_RU[k]: mean([r["agent"][k] for *_, r in vals if r["agent"].get(k)]) for k in AGENT},
        "predicted_csat": mean([c for _, c, *_ in vals if c]),
        "closed_share": round(sum(1 for *_, r in vals if r["case"].get("closed")) / len(vals), 2),
        "outcomes": {OUTCOMES.get(o, o): n for o, n in Counter(o for _, _, o, _ in vals).most_common()},
        "lost_reasons": {OBJECTIONS.get(x, x): n for x, n in Counter(r["case"].get("lost_reason") for *_, r in vals
                                                                     if r["case"].get("lost_reason") in OBJECTIONS
                                                                     ).most_common()},
        "techniques": {k: {"сработал": v["positive"], "без эффекта": v["neutral"], "навредил": v["negative"]}
                       for k, v in sorted(by_tech.items(), key=lambda kv: -sum(kv[1].values()))},
        "objections": {OBJECTIONS[t]: {"закрыто": sum(1 for *_, r in vals for o in r["objections"]
                                                      if o["type"] == t and o["handled"]),
                                       "всего": sum(1 for *_, r in vals for o in r["objections"] if o["type"] == t)}
                       for t in OBJECTIONS if any(o["type"] == t for *_, r in vals for o in r["objections"])},
        "sentiment_end": dict(Counter(r["client"].get("sentiment_end") for *_, r in vals)),
        "mistakes": [m for *_, r in vals for m in r["mistakes"]][-10:],
        "lessons": [r.get("lesson") for *_, r in vals if r.get("lesson")][-10:],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:]
    if args[:1] == ["dm"] and len(args) >= 2 and args[1].isdigit():
        st, c = cw("GET", f"/conversations/{args[1]}")
        c = c if st == 200 and isinstance(c, dict) else sys.exit(f"conversation HTTP {st}")
        print(json.dumps(review_dm(c, write="--save" in args), ensure_ascii=False, indent=2))
    elif args[:1] == ["report"]:
        print(json.dumps(report(float(args[1]) if len(args) > 1 else 7.0), ensure_ascii=False, indent=2))
    else:
        sys.exit(__doc__)
