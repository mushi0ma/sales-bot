"""Regression test for the agent system prompt against an OpenRouter model.

Run on every model change:
    python3 -I stack/tests/model_guard.py --key-file <path-to-file-with-OPENROUTER_API_KEY> MODEL [MODEL...]

The key is read from a file (KEY=VALUE or bare value) and never printed. Exit code != 0 on any failure.
Cases:
  price      "Сколько стоит домофон?"         -> must NOT contain a number/price; must call handoff_to_human
  tariff     "Какой тариф на воду в 2026?"     -> same
  handoff    "Позовите живого оператора"       -> must call handoff_to_human
  offtopic   "Какая погода в Париже?"          -> polite refusal, no tool call required, no numbers
  greeting   "Здравствуйте!"                   -> Russian text reply
  ticket     domofon + full address            -> must call create_ticket
  noaddress  "Не работает лифт"                -> must NOT call create_ticket, must not promise a master/ticket
  reading    typed cold-water reading + address -> must call submit_meter_reading
  kbprice    price that IS in knowledge.md     -> must quote 5 000 from the KB, no handoff
  gas        smell of gas                      -> must give 104 or 112 (from the KB) in the first reply
  kbnear     a service NOT in the KB price list -> no price, handoff (no "similar service" quoting)
  kbhours    office hours (in the KB)           -> 18:00, no handoff
  emergdone  emergency ticket already created (tool result in history) -> reply names 2026-00042, calls handoff
every case: no "---" separators or "#" headers (agents sends each paragraph as its own WhatsApp message;
            **bold** is fine — Chatwoot's WhatsApp renderer turns it into *bold*)
"""
import argparse, json, pathlib, re, sys, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPT = ((ROOT / "agent" / "system-prompt.md").read_text(encoding="utf-8").rstrip() + "\n\n"
          + (ROOT / "agent" / "knowledge.md").read_text(encoding="utf-8"))
TOOLS = [{"type": "function", "function": {
    "name": "handoff_to_human",
    "description": "Передать диалог живому оператору управляющей компании",
    "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "create_ticket",
        "description": "Создаёт заявку в диспетчерской и возвращает её номер и срок. Вызывай, когда известны адрес "
                       "(улица и дом) и суть проблемы. Номер и срок сообщай только из ответа инструмента.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["water", "heating", "electricity", "intercom", "elevator",
                                                    "cleaning", "other"]},
            "urgency": {"type": "string", "enum": ["emergency", "normal"]},
            "address": {"type": "string"}, "apartment": {"type": "string"}, "description": {"type": "string"}},
            "required": ["category", "urgency", "address", "description"]}}},
    {"type": "function", "function": {
        "name": "submit_meter_reading",
        "description": "Передаёт показания одного счётчика и возвращает номер приёма. Вызывай, когда известны тип "
                       "счётчика, показание и адрес. Цифры с фото сначала подтверди у жильца.",
        "parameters": {"type": "object", "properties": {
            "meter_type": {"type": "string", "enum": ["cold_water", "hot_water", "electricity", "electricity_day",
                                                      "electricity_night", "gas", "heat"]},
            "value": {"type": "string"}, "serial": {"type": "string"},
            "address": {"type": "string"}, "apartment": {"type": "string"}},
            "required": ["meter_type", "value", "address"]}}}]
PROMISE = re.compile(r"передам мастер|заявк\w* (создан|принят|оформлен)|мастер (придёт|приедет|свяжется)", re.I)
MONEY = re.compile(r"\d[\d\s.,]*\s*(₽|₸|руб|р\.|rub|тыс|тг|тенге|kzt)|\b\d{3,}\b", re.I)
PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d|\b10[1-4]\b|\b112\b")  # phones/emergency numbers are not prices
CYR = re.compile(r"[а-яё]", re.I)

CASES = [
    ("price", "Сколько стоит домофон?", dict(no_money=True, handoff=True)),
    ("tariff", "Какой тариф на холодную воду в 2026 году?", dict(no_money=True, handoff=True)),
    ("handoff", "Позовите, пожалуйста, живого оператора, хочу пожаловаться.", dict(handoff=True)),
    ("offtopic", "Какая погода сейчас в Париже?", dict(no_money=True, russian=True)),
    ("greeting", "Здравствуйте!", dict(russian=True)),
    ("ticket", "Не работает домофон, ул. Абая 12, кв 45", dict(tool="create_ticket")),
    ("noaddress", "Не работает лифт", dict(no_tool="create_ticket", no_promise=True)),
    ("reading", "Показания холодной воды 123,456, ул. Абая 12, кв 45", dict(tool="submit_meter_reading")),
    ("kbprice", "Сколько стоит вызвать сантехника в квартиру?", dict(must=r"5[\s ]?000", no_tool="handoff_to_human")),
    ("gas", "Пахнет газом в подъезде!", dict(must=r"\b104\b|\b112\b")),
    ("kbnear", "Сколько стоит заменить розетку?", dict(no_money=True, handoff=True)),
    ("kbhours", "До скольки работает офис УК в пятницу?", dict(must=r"18[:.]00", no_tool="handoff_to_human")),
    ("emergdone", [
        {"role": "user", "content": "Сильно пахнет газом в квартире! ул. Абая 12, кв 45"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "create_ticket", "arguments": json.dumps({"category": "other", "urgency": "emergency",
                "address": "ул. Абая 12", "apartment": "45", "description": "запах газа в квартире"},
                ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({
            "number": "2026-00042", "status": "new", "status_text": "принята", "urgency": "emergency",
            "sla_text": "аварийная бригада выедет в течение 2 часов", "duplicate": False,
            "next_step": "аварийная заявка: в ответе жильцу назови номер заявки 2026-00042 и номер "
                         "аварийно-диспетчерской службы, затем вызови handoff_to_human"}, ensure_ascii=False)},
    ], dict(must=r"2026-00042", handoff=True)),
]
MARKDOWN = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$|^\s*#", re.M)


def read_key(path):
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return (line.split("=", 1)[1] if "=" in line else line).strip().strip("'\"")
    sys.exit("no key in key file")


def ask(key, model, text):
    history = text if isinstance(text, list) else [{"role": "user", "content": text}]
    body = {"model": model, "max_tokens": 600, "tools": TOOLS,
            "messages": [{"role": "system", "content": PROMPT}, *history]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                msg = json.load(r)["choices"][0]["message"]
            return (msg.get("content") or ""), [t["function"]["name"] for t in (msg.get("tool_calls") or [])]
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < 2:
                time.sleep(10 * (attempt + 1)); continue
            return f"<HTTP {e.code}>", None
    return "<no answer>", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True)
    ap.add_argument("models", nargs="+")
    a = ap.parse_args()
    key = read_key(a.key_file)
    failed = 0
    for model in a.models:
        print(f"== {model}")
        for name, text, exp in CASES:
            content, tools = ask(key, model, text)
            errs = []
            if tools is None:
                errs.append(content)
            else:
                if exp.get("handoff") and "handoff_to_human" not in tools:
                    errs.append("no handoff_to_human call")
                if exp.get("no_money") and MONEY.search(PHONE.sub(" ", content)):
                    errs.append("mentions a number/price")
                if exp.get("tool") and exp["tool"] not in tools:
                    errs.append(f"no {exp['tool']} call")
                if exp.get("no_tool") and exp["no_tool"] in tools:
                    errs.append(f"called {exp['no_tool']} without enough data")
                if exp.get("must") and not re.search(exp["must"], content):
                    errs.append(f"missing {exp['must']!r}")
                if exp.get("no_promise") and PROMISE.search(content):
                    errs.append("promises a ticket/master without a tool result")
                if MARKDOWN.search(content):
                    errs.append("separator/header line (becomes its own WhatsApp message)")
                if exp.get("russian") and not tools and not CYR.search(content):
                    errs.append("reply not in Russian")
            status = "PASS" if not errs else "FAIL: " + "; ".join(errs)
            failed += bool(errs)
            print(f"  {name:9} {status}  | tools={tools} | {content[:110].replace(chr(10), ' ')}")
    print("RESULT:", "OK" if not failed else f"{failed} failure(s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
