"""Answers in the group itself, under the question, as a neighbour who knows would.

Two kinds of answers:
  e-KNOT  a question about e-KNOT (price, tariff, how a service works, the free Аудитор ЖКХ), answered from the
          knowledge material (services.json + the eknot.me snapshots in stack/kb/pages). What belongs to one house
          is not known here: such a question gets no e-KNOT answer.
  chat    what the group itself already answered. When a member replies (WhatsApp "reply") to a question, the pair
          is remembered for that group, and the next person asking the same in other words gets that answer
          quoted with its author: «Здесь уже отвечали (Айгуль): ...». The bot never states it as its own fact.

Every asker gets an answer, as a reply to their own message: in a busy chat the same question comes from many
people, and answering only the first is worse than answering none. Cached answers cost one embedding call, so
repeats are cheap; only the same person asking the same again within REPEAT_HOURS is skipped.

Cache: an answer is stored with the embedding of its question; another wording with cosine >= SIMILAR is the same
question. e-KNOT answers are tied to a hash of the material (a changed site retires them), chat answers expire
after CHAT_TTL_DAYS and a newer reply to the same question replaces the older one.

Stdlib only. watcher.py owns the database, HTTP and Chatwoot; this module gets them as arguments.
"""
import hashlib, json, math, os, re, time

FAQ_MODE = os.environ.get("FAQ_MODE", "auto")  # auto | off
FAQ_MODEL = os.environ.get("FAQ_MODEL", "gemini-3.8-flash")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = 768
SIMILAR = float(os.environ.get("FAQ_SIMILAR", "0.86"))
TTL_DAYS = int(os.environ.get("FAQ_TTL_DAYS", "45"))
CHAT_TTL_DAYS = int(os.environ.get("FAQ_CHAT_TTL_DAYS", "30"))
MODEL_PER_HOUR = int(os.environ.get("FAQ_MODEL_PER_HOUR", "6"))  # new answers generated per group per hour (cost)
POSTS_PER_HOUR = int(os.environ.get("FAQ_POSTS_PER_HOUR", "40"))  # all answers per group per hour (flood guard)
REPEAT_HOURS = int(os.environ.get("FAQ_REPEAT_HOURS", "12"))  # the same person, the same answer
BACKFILL_HOURS = float(os.environ.get("FAQ_BACKFILL_HOURS", "3"))  # earlier askers answered once the chat answers
KB_DIR = os.environ.get("KB_DIR", "/app/kb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS faq (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL, answer TEXT NOT NULL, lang TEXT,
  embedding TEXT NOT NULL,            -- JSON list, EMBED_DIM floats, unit length
  kb_version TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS group_answers (
  message_id INTEGER PRIMARY KEY, conv_id INTEGER NOT NULL, faq_id INTEGER,
  source TEXT NOT NULL,               -- cache | model | chat | none:<why>
  latency REAL, at REAL NOT NULL
);
"""
# Columns added after the first deploy; ALTER is idempotent through the check in migrate().
COLUMNS = {"faq": {"conv_id": "INTEGER",            # NULL: e-KNOT knowledge; set: learned in that group
                   "author": "TEXT", "author_contact_id": "INTEGER", "source_message_id": "INTEGER"},
           "group_answers": {"contact_id": "INTEGER"}}

QUESTION = re.compile(r"\?|^\s*(сколько|как|где|когда|куда|можно ли|есть ли|что такое|кто|какой|какая|какие|на какой"
                      r"|қанша|қалай|қайда|қашан|кім|қандай|бар ма)\b|\b(ма|ме|ба|бе|па|пе)\s*\??\s*$", re.I)
TOPIC = re.compile(r"e-?knot|е-?кнот|екнот|и-?кнот|аудитор|zerde|зерде|приложени|сервис|платформ|тариф|стои|цен[аыу]"
                   r"|бесплатн|подключ|қосымша|баға|тегін|қанша тұрады", re.I)


def is_question(text):
    t = (text or "").strip()
    return len(t) >= 8 and bool(QUESTION.search(t.lower()))


def is_candidate(text, catalog_roots):
    """A question for the e-KNOT knowledge."""
    t = text.lower().replace("ё", "е")
    if len(t) < 10 or not is_question(t):
        return False
    return bool(TOPIC.search(t)) or any(r in t for r in catalog_roots)


NOT_ANSWER = re.compile(r"^\W*(\+1|тоже|и мне|мне тоже|присоединяюсь|не знаю|хз|білмеймін|мен де|маған да|спасибо"
                        r"|рахмет|ок|ok|ага|да|нет|иә|жоқ)\W*$|^\W*$", re.I)


def is_answer(text):
    t = (text or "").strip()
    return len(t) >= 2 and not is_question(t) and not NOT_ANSWER.search(t.lower())


def material(catalog_path, kb_dir=KB_DIR):
    """(text, version): the catalog without the detection fields, plus every page snapshot."""
    with open(catalog_path, encoding="utf-8") as f:
        cat = json.load(f)
    parts = ["О КОМПАНИИ: " + json.dumps(cat.get("company", {}), ensure_ascii=False)]
    for s in cat["services"]:
        parts.append(f"УСЛУГА {s['name']} ({s['url']}). {s['what']} {s.get('offer', '')} "
                     + json.dumps(s.get("details", {}), ensure_ascii=False))
    if os.path.isdir(kb_dir):
        for name in sorted(os.listdir(kb_dir)):
            if name.endswith(".md"):
                with open(os.path.join(kb_dir, name), encoding="utf-8") as f:
                    parts.append("СТРАНИЦА САЙТА\n" + f.read())
    text = "\n\n".join(parts)
    return text, hashlib.sha1(text.encode()).hexdigest()[:12]


def unit(v):
    n = math.sqrt(sum(a * a for a in v)) or 1.0
    return [a / n for a in v]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def best_match(vec, rows, now, kb_version=None):
    best, score = None, 0.0
    for r in rows:
        if r["disabled"] or r["expires_at"] < now or (kb_version and r["kb_version"] != kb_version):
            continue
        s = cosine(vec, json.loads(r["embedding"]))
        if s > score:
            best, score = r, s
    return (best, score) if best is not None and score >= SIMILAR else (None, score)


def chat_reply(row):
    who = f" ({row['author'].split()[0]})" if row["author"] else ""
    return f"Здесь уже отвечали{who}: «{row['answer'][:600]}»"


SYSTEM = (
    "Ты помощник e-KNOT и сидишь в общем чате жильцов дома в Казахстане. Кто-то задал в чате вопрос. Ответь, только "
    "если ответ есть в МАТЕРИАЛАХ и он одинаков для любого дома: цены и тарифы e-KNOT, что умеет услуга, как "
    "подключить, бесплатный Аудитор ЖКХ (zhkh.zerde.site) для жилищных споров.\n"
    "answerable=false, если: вопрос про конкретный дом (сумма взноса, реквизиты, долги, график, решения ОСИ или "
    "председателя, когда починят), вопрос к соседям, а не про e-KNOT, или ответа в материалах нет. Ничего не "
    "додумывай.\n"
    "Как писать answer: как живой человек в чате, 1-3 коротких предложения, на языке вопроса (русский или "
    "казахский), без markdown, списков и шаблонных фраз вроде «Отличный вопрос». Можно одну ссылку из материалов. "
    "Цену называй с тарифом и добавь, что точный расчёт для дома делает менеджер e-KNOT. Результатов не обещай.\n"
    "stable=true, если ответ верен минимум месяц (цены, тарифы, описание услуги); false, если зависит от даты, "
    "акции или чего-то, что меняется чаще.\nlang: ru или kk."
)
ANSWER_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["answerable", "stable", "lang", "answer"],
                 "properties": {"answerable": {"type": "boolean"}, "stable": {"type": "boolean"},
                                "lang": {"type": "string", "enum": ["ru", "kk"]}, "answer": {"type": "string"}}}


class Faq:
    def __init__(self, *, q, x, llm, cw, catalog_path, catalog_roots, log, kb_dir=KB_DIR):
        self.q, self.x, self.llm, self.cw, self.log = q, x, llm, cw, log
        self.catalog_path, self.roots, self.kb_dir = catalog_path, catalog_roots, kb_dir
        self._material = None

    @staticmethod
    def migrate(db):
        for table, cols in COLUMNS.items():
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for col, kind in cols.items():
                if col not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")

    def material(self):
        if self._material is None:
            self._material = material(self.catalog_path, self.kb_dir)
        return self._material

    def embed(self, text):
        st, b = self.llm("/embeddings", {"model": EMBED_MODEL, "input": [text], "dimensions": EMBED_DIM})
        if st != 200:
            raise RuntimeError(f"embedding HTTP {st}: {str(b)[:160]}")
        return unit(b["data"][0]["embedding"])

    def generate(self, question):
        text, _ = self.material()
        st, b = self.llm("/chat/completions", {
            "model": FAQ_MODEL, "temperature": 0.2,
            "messages": [{"role": "system", "content": SYSTEM + "\n\nМАТЕРИАЛЫ:\n" + text},
                         {"role": "user", "content": question}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "schema": ANSWER_SCHEMA, "strict": True}}})
        if st != 200:
            raise RuntimeError(f"answer HTTP {st}: {str(b)[:160]}")
        return json.loads(b["choices"][0]["message"]["content"] or "{}")

    # ------------------------------------------------------------ learning from the chat

    def has_chat_answers(self, conv_id, now):
        return bool(self.q("SELECT 1 FROM faq WHERE conv_id=? AND disabled=0 AND expires_at>=? LIMIT 1",
                           (conv_id, now)))

    def learn(self, row, question_row, now=None):
        """A member replied to a question in the group: remember the pair for this group. True when stored."""
        now = now or time.time()
        if (not question_row or question_row["contact_id"] == row["contact_id"]
                or not is_question(question_row["text"]) or not is_answer(row["text"])):
            return False
        vec = self.embed(question_row["text"])
        old, _ = best_match(vec, self.q("SELECT * FROM faq WHERE conv_id=?", (row["conv_id"],)), now)
        if old is not None:  # the chat answered the same again: the newer answer wins
            self.x("UPDATE faq SET disabled=1 WHERE id=?", (old["id"],))
        self.x("INSERT INTO faq (question, answer, embedding, kb_version, created_at, expires_at, conv_id, author, "
               "author_contact_id, source_message_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (question_row["text"][:500], row["text"][:1000], json.dumps([round(v, 6) for v in vec]), "chat", now,
                now + CHAT_TTL_DAYS * 86400, row["conv_id"], row.get("author"), row["contact_id"], row["id"]))
        faq_id = self.q("SELECT last_insert_rowid() AS id")[0]["id"]
        self.log.info("faq learned in group=%s from message=%s (replaces %s)", row["conv_id"], row["id"],
                      old["id"] if old is not None else None)
        self.backfill(row["conv_id"], faq_id, vec, {question_row["id"], row["id"]}, now)
        return True

    def backfill(self, conv_id, faq_id, vec, skip_ids, now):
        """The chat just answered: the others who asked the same in the last hours and got nothing hear it too."""
        hit = self.q("SELECT * FROM faq WHERE id=?", (faq_id,))[0]
        rows = self.q("SELECT m.* FROM messages m LEFT JOIN group_answers g ON g.message_id=m.id "
                      "WHERE m.conv_id=? AND m.created_at>=? AND (g.source IS NULL OR g.source LIKE 'none:%') "
                      "ORDER BY m.id DESC LIMIT 30", (conv_id, now - BACKFILL_HOURS * 3600))
        done = 0
        for r in rows:
            if (r["id"] in skip_ids or r["contact_id"] == hit["author_contact_id"] or not is_question(r["text"])
                    or cosine(vec, self.embed(r["text"])) < SIMILAR):
                continue
            done += self.post(dict(r), chat_reply(hit), faq_id, "chat", now)
        if done:
            self.x("UPDATE faq SET hits=hits+? WHERE id=?", (done, faq_id))
            self.log.info("faq backfill group=%s faq=%s: %s earlier askers answered", conv_id, faq_id, done)
        return done

    # ------------------------------------------------------------ answering

    def lookup_or_generate(self, question, now=None, vec=None, allow_model=True):
        """(answer, faq_id, source) or (None, None, 'none:<why>'). e-KNOT knowledge only; posts nothing."""
        now = now or time.time()
        _, version = self.material()
        vec = vec or self.embed(question)
        hit, _ = best_match(vec, self.q("SELECT * FROM faq WHERE conv_id IS NULL"), now, version)
        if hit is not None:
            self.x("UPDATE faq SET hits=hits+1 WHERE id=?", (hit["id"],))
            return hit["answer"], hit["id"], "cache"
        if not allow_model:
            return None, None, "none:model_cap"
        a = self.generate(question)
        if not a.get("answerable") or not (a.get("answer") or "").strip():
            return None, None, "none:not_answerable"
        answer = a["answer"].strip()
        if not a.get("stable"):
            return answer, None, "model"
        self.x("INSERT INTO faq (question, answer, lang, embedding, kb_version, created_at, expires_at) "
               "VALUES (?,?,?,?,?,?,?)", (question, answer, a.get("lang"), json.dumps([round(v, 6) for v in vec]),
                                          version, now, now + TTL_DAYS * 86400))
        return answer, self.q("SELECT last_insert_rowid() AS id")[0]["id"], "model"

    def wants(self, row, now=None):
        """Whether a group message is worth an embedding call at all."""
        if FAQ_MODE != "auto" or not is_question(row["text"]):
            return False
        return is_candidate(row["text"], self.roots) or self.has_chat_answers(row["conv_id"], now or time.time())

    def posts_since(self, conv_id, since, sources):
        marks = ",".join("?" * len(sources))
        return self.q(f"SELECT faq_id, contact_id FROM group_answers WHERE conv_id=? AND at>=? AND source IN ({marks})",
                      (conv_id, since, *sources))

    def handle(self, row):
        """One group message (watcher's messages row). Returns True when an answer was posted in the group."""
        now = time.time()
        if not self.wants(row, now):
            return False
        answer, faq_id, source = None, None, "none:no_match"
        if len(self.posts_since(row["conv_id"], now - 3600, ("cache", "model", "chat"))) >= POSTS_PER_HOUR:
            source = "none:hour_cap"
        else:
            vec = self.embed(row["text"])
            hit, _ = best_match(vec, self.q("SELECT * FROM faq WHERE conv_id=?", (row["conv_id"],)), now)
            if hit is not None:
                self.x("UPDATE faq SET hits=hits+1 WHERE id=?", (hit["id"],))
                answer, faq_id, source = chat_reply(hit), hit["id"], "chat"
            elif is_candidate(row["text"], self.roots):
                generated = len(self.posts_since(row["conv_id"], now - 3600, ("model",)))
                answer, faq_id, source = self.lookup_or_generate(row["text"], now, vec,
                                                                 allow_model=generated < MODEL_PER_HOUR)
        if answer and faq_id and any(r["faq_id"] == faq_id and r["contact_id"] == row.get("contact_id")
                                     for r in self.posts_since(row["conv_id"], now - REPEAT_HOURS * 3600,
                                                               ("cache", "model", "chat"))):
            answer, source = None, "none:repeat_same_person"
        if not answer:
            self.x("INSERT OR IGNORE INTO group_answers (message_id, conv_id, faq_id, source, latency, at, contact_id)"
                   " VALUES (?,?,?,?,?,?,?)", (row["id"], row["conv_id"], None, source, None, now, row.get("contact_id")))
            self.log.info("faq message=%s: %s", row["id"], source)
            return False
        return self.post(row, answer, faq_id, source, now)

    def post(self, row, answer, faq_id, source, now):
        """Reply in the group under the asker's message, and record it."""
        latency = time.time() - row["created_at"]
        st, _ = self.cw("POST", f"/conversations/{row['conv_id']}/messages",
                        {"content": answer, "content_attributes": {"in_reply_to": row["id"]}})
        ok = st in (200, 201)
        self.x("INSERT OR REPLACE INTO group_answers (message_id, conv_id, faq_id, source, latency, at, contact_id)"
               " VALUES (?,?,?,?,?,?,?)", (row["id"], row["conv_id"], faq_id, source if ok else f"none:http_{st}",
                                           latency, now, row.get("contact_id")))
        self.log.info("faq message=%s answered in group: %s faq=%s %.1fs HTTP %s", row["id"], source, faq_id,
                      latency, st)
        return ok

    def forget_contact(self, contact_id):
        """Opt-out or a test reset: what this person taught the bot goes with their messages."""
        self.x("DELETE FROM faq WHERE author_contact_id=?", (contact_id,))
        self.x("UPDATE group_answers SET contact_id=NULL WHERE contact_id=?", (contact_id,))
