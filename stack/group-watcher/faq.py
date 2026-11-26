"""Answers in the group itself: a resident asks the chat about e-KNOT (price, tariff, how a service works, the
free Аудитор ЖКХ) and the bot replies under the question, as a neighbour who knows would.

Only facts from the knowledge material (services.json + the eknot.me page snapshots in stack/kb/pages). What
belongs to one house (its fee, its debts, its board's decisions) is not known here, and such a question gets no
answer. A stable answer (true for a month or more: prices, what a service does) is cached with the embedding of
its question, so the same question asked again in other words is answered at once without a model call. The cache
is tied to a hash of the material: a changed price on the site (kb/refresh.py + redeploy) retires every entry.

Stdlib only. watcher.py owns the database, HTTP and Chatwoot; this module gets them as arguments.
"""
import hashlib, json, math, os, re, time

FAQ_MODE = os.environ.get("FAQ_MODE", "auto")  # auto | off
FAQ_MODEL = os.environ.get("FAQ_MODEL", "gemini-3.8-flash")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = 768
SIMILAR = float(os.environ.get("FAQ_SIMILAR", "0.86"))  # cosine: same question in other words
TTL_DAYS = int(os.environ.get("FAQ_TTL_DAYS", "45"))
MAX_PER_HOUR = int(os.environ.get("FAQ_MAX_PER_HOUR", "6"))  # answers per group per hour
REPEAT_HOURS = int(os.environ.get("FAQ_REPEAT_HOURS", "12"))  # the same entry is not posted twice in a group
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
  source TEXT NOT NULL,               -- cache | model | none:<why>
  latency REAL, at REAL NOT NULL
);
"""

QUESTION = re.compile(r"\?|^\s*(сколько|как|где|можно ли|есть ли|что такое|кто|қанша|қалай|қайда|бар ма)\b"
                      r"|\b(ма|ме|ба|бе|па|пе)\s*$", re.I)
TOPIC = re.compile(r"e-?knot|е-?кнот|екнот|и-?кнот|аудитор|zerde|зерде|приложени|сервис|платформ|тариф|стои|цен[аыу]"
                   r"|бесплатн|подключ|қосымша|баға|тегін|қанша тұрады", re.I)


def is_candidate(text, catalog_roots):
    t = text.lower().replace("ё", "е")
    if len(t) < 10 or not QUESTION.search(t):
        return False
    return bool(TOPIC.search(t)) or any(r in t for r in catalog_roots)


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


def best_match(vec, rows, kb_version, now):
    best, score = None, 0.0
    for r in rows:
        if r["disabled"] or r["kb_version"] != kb_version or r["expires_at"] < now:
            continue
        s = cosine(vec, json.loads(r["embedding"]))
        if s > score:
            best, score = r, s
    return (best, score) if best is not None and score >= SIMILAR else (None, score)


SYSTEM = (
    "Ты помощник e-KNOT и сидишь в общем чате жильцов дома в Казахстане. Кто-то задал в чате вопрос. Ответь, только "
    "если ответ есть в МАТЕРИАЛАХ и он одинаков для любого дома: цены и тарифы e-KNOT, что умеет услуга, как "
    "подключить, бесплатный Аудитор ЖКХ (zhkh.zerde.site) для жилищных споров.\n"
    "answerable=false, если: вопрос про конкретный дом (сумма взноса, долги, график, решения ОСИ или председателя, "
    "когда починят), вопрос к соседям, а не про e-KNOT, или ответа в материалах нет. Ничего не додумывай.\n"
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

    def recent_posts(self, conv_id, since):
        return self.q("SELECT faq_id FROM group_answers WHERE conv_id=? AND at>=? AND source IN ('cache','model')",
                      (conv_id, since))

    def lookup_or_generate(self, question, now=None):
        """(answer, faq_id, source) or (None, None, 'none:<why>'). No Chatwoot, no posting: also the eval path."""
        now = now or time.time()
        _, version = self.material()
        vec = self.embed(question)
        hit, score = best_match(vec, self.q("SELECT * FROM faq"), version, now)
        if hit is not None:
            self.x("UPDATE faq SET hits=hits+1 WHERE id=?", (hit["id"],))
            return hit["answer"], hit["id"], "cache"
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

    def handle(self, row):
        """One group message (watcher's messages row). Returns True when an answer was posted in the group."""
        if FAQ_MODE != "auto" or not is_candidate(row["text"], self.roots):
            return False
        now = time.time()
        if len(self.recent_posts(row["conv_id"], now - 3600)) >= MAX_PER_HOUR:
            self.x("INSERT OR IGNORE INTO group_answers (message_id, conv_id, source, at) VALUES (?,?,?,?)",
                   (row["id"], row["conv_id"], "none:hour_cap", now))
            return False
        answer, faq_id, source = self.lookup_or_generate(row["text"], now)
        if answer and faq_id and any(r["faq_id"] == faq_id for r in self.recent_posts(row["conv_id"],
                                                                                   now - REPEAT_HOURS * 3600)):
            answer, source = None, "none:repeat"  # answered in this chat a moment ago, the reply is right above
        latency = time.time() - row["created_at"]
        self.x("INSERT OR IGNORE INTO group_answers (message_id, conv_id, faq_id, source, latency, at) "
               "VALUES (?,?,?,?,?,?)", (row["id"], row["conv_id"], faq_id, source, latency, now))
        if not answer:
            self.log.info("faq message=%s: %s", row["id"], source)
            return False
        st, _ = self.cw("POST", f"/conversations/{row['conv_id']}/messages",
                        {"content": answer, "content_attributes": {"in_reply_to": row["id"]}})
        self.log.info("faq message=%s answered in group: %s faq=%s %.1fs HTTP %s", row["id"], source, faq_id,
                      latency, st)
        return st in (200, 201)
