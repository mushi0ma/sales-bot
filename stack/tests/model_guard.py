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
"""
import argparse, json, pathlib, re, sys, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "agent" / "system-prompt.md").read_text(encoding="utf-8")
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
MONEY = re.compile(r"\d[\d\s.,]*\s*(₽|руб|р\.|rub|тыс)|\b\d{3,}\b", re.I)
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
]


def read_key(path):
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return (line.split("=", 1)[1] if "=" in line else line).strip().strip("'\"")
    sys.exit("no key in key file")


def ask(key, model, text):
    body = {"model": model, "max_tokens": 400, "tools": TOOLS,
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}]}
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
                if exp.get("no_money") and MONEY.search(content):
                    errs.append("mentions a number/price")
                if exp.get("tool") and exp["tool"] not in tools:
                    errs.append(f"no {exp['tool']} call")
                if exp.get("no_tool") and exp["no_tool"] in tools:
                    errs.append(f"called {exp['no_tool']} without enough data")
                if exp.get("no_promise") and PROMISE.search(content):
                    errs.append("promises a ticket/master without a tool result")
                if exp.get("russian") and not tools and not CYR.search(content):
                    errs.append("reply not in Russian")
            status = "PASS" if not errs else "FAIL: " + "; ".join(errs)
            failed += bool(errs)
            print(f"  {name:9} {status}  | tools={tools} | {content[:110].replace(chr(10), ' ')}")
    print("RESULT:", "OK" if not failed else f"{failed} failure(s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
