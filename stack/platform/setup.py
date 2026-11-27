#!/usr/bin/env python3
"""Configure the platform (stack/platform.yml). Runs ON the VM: cd /srv/salesbot/stack && python3 -I platform/setup.py <step>

Steps (idempotent; tokens go to stack/.env, mode 600, and are never printed):
  hatchet  an API token of the default tenant -> HATCHET_CLIENT_TOKEN, HATCHET_TENANT_ID
  twenty   the admin, the workspace «e-KNOT», an API key -> TWENTY_API_KEY
  model    Twenty data model: resident fields on People, the sales funnel on Opportunities (cards.py writes them)
  all      hatchet twenty model
Logins (emails; the passwords are LANGFUSE_/TWENTY_/HATCHET_ADMIN_PASSWORD in stack/.env): ~/secrets/platform-admin.env
"""
import json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

STACK = pathlib.Path(__file__).resolve().parent.parent
SECRETS = pathlib.Path.home() / "secrets"
ADMIN_EMAIL = "admin@sales-bot.eknot.kz"
TWENTY = "http://127.0.0.1:3300"
TWENTY_ORIGIN = "http://localhost:3300"  # SERVER_URL of the twenty service
HATCHET = "http://127.0.0.1:8888"


def die(msg):
    sys.exit(f"platform: {msg}")


def env(name):
    for line in (STACK / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return None


def set_env(values):
    p = STACK / ".env"
    lines = [l for l in p.read_text().splitlines() if l.split("=", 1)[0] not in values]
    p.write_text("\n".join(lines + [f"{k}={v}" for k, v in values.items()]) + "\n")
    p.chmod(0o600)
    print(f"  stack/.env: {', '.join(values)}")


def note_logins():
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    p = SECRETS / "platform-admin.env"
    old = os.umask(0o077)
    try:
        p.write_text("# Platform UIs through the IAP tunnel (stack/platform.yml). Passwords: stack/.env, names below.\n"
                     f"LANGFUSE_URL=http://localhost:3200\nLANGFUSE_EMAIL={ADMIN_EMAIL}\n# LANGFUSE_ADMIN_PASSWORD\n"
                     f"TWENTY_URL=http://localhost:3300\nTWENTY_EMAIL={ADMIN_EMAIL}\n# TWENTY_ADMIN_PASSWORD\n"
                     f"HATCHET_URL=http://localhost:8888\nHATCHET_EMAIL={ADMIN_EMAIL}\n# HATCHET_ADMIN_PASSWORD\n")
    finally:
        os.umask(old)
    p.chmod(0o600)


def http(method, url, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300].decode(errors="replace")}


def gql(path, query, variables=None, token=None):
    """Twenty GraphQL: /metadata (workspace, auth, data model) or /graphql (records). Dies on errors."""
    # No Origin header: Twenty refuses to issue a session cookie to an origin it does not know, and a server-side
    # client needs no cookie (the workspace origin goes as the `origin` argument where a mutation asks for it).
    st, b = http("POST", TWENTY + path, {"query": query, "variables": variables or {}}, token)
    if st >= 300 or b.get("errors"):
        msgs = [e.get("message", "")[:200] + (f" {json.dumps(e.get('extensions'), ensure_ascii=False)[:600]}"
                                               if e.get("extensions") else "")
                for e in (b.get("errors") or [])] or [str(b)[:200]]
        raise RuntimeError(f"HTTP {st}: {'; '.join(msgs)}")
    return b["data"]


# ---------------------------------------------------------------- hatchet

def step_hatchet():
    token = env("HATCHET_CLIENT_TOKEN")
    tenant = env("HATCHET_TENANT_ID")
    if token and tenant:
        st, _ = http("GET", f"{HATCHET}/api/v1/tenants/{tenant}", token=token)
        if st == 200:
            print("  hatchet: token works, kept")
            return
    r = subprocess.run(["docker", "compose", "exec", "-T", "platform-postgres", "psql", "-U", "postgres", "-d", "hatchet",
                        "-tAc", "SELECT id FROM \"Tenant\" WHERE slug <> 'internal' ORDER BY \"createdAt\" LIMIT 1"],
                       cwd=STACK, capture_output=True, text=True)
    tenant = r.stdout.strip()
    if r.returncode or len(tenant) != 36:
        die(f"hatchet tenant not found: {r.stderr.strip()[:200]}")
    r = subprocess.run(["docker", "compose", "exec", "-T", "hatchet", "/hatchet-admin", "token", "create", "--config",
                        "/config", "--tenant-id", tenant, "--name", "salesbot", "--expiresIn", "8760h"],
                       cwd=STACK, capture_output=True, text=True)
    token = next((l.strip() for l in r.stdout.splitlines() if l.strip().count(".") == 2), "")
    if r.returncode or not token:
        die("hatchet token create failed (output withheld: it may hold the token)")
    set_env({"HATCHET_CLIENT_TOKEN": token, "HATCHET_TENANT_ID": tenant})


# ---------------------------------------------------------------- twenty: admin, workspace, API key

def twenty_key_works(key):
    if not key:
        return False
    try:
        gql("/graphql", "{ people(first: 1) { totalCount } }", token=key)
        return True
    except RuntimeError:
        return False


def step_twenty():
    if twenty_key_works(env("TWENTY_API_KEY")):
        print("  twenty: API key works, kept")
        return
    password = env("TWENTY_ADMIN_PASSWORD") or die("TWENTY_ADMIN_PASSWORD is empty (render-env.sh)")
    try:
        d = gql("/metadata", """mutation($email: String!, $password: String!) {
                  signUp(email: $email, password: $password) {
                    tokens { accessOrWorkspaceAgnosticToken { token } } } }""",
                {"email": ADMIN_EMAIL, "password": password})
        agnostic = d["signUp"]["tokens"]["accessOrWorkspaceAgnosticToken"]["token"]
        print(f"  twenty: user {ADMIN_EMAIL}")
    except RuntimeError as e:
        if "already" not in str(e).lower():
            raise
        d = gql("/metadata", """mutation($email: String!, $password: String!) {
                  signIn(email: $email, password: $password) {
                    tokens { accessOrWorkspaceAgnosticToken { token } } } }""",
                {"email": ADMIN_EMAIL, "password": password})
        agnostic = d["signIn"]["tokens"]["accessOrWorkspaceAgnosticToken"]["token"]
        print(f"  twenty: user {ADMIN_EMAIL} exists, signed in")
    d = gql("/metadata", """mutation($input: SignUpInNewWorkspaceInput) {
              signUpInNewWorkspace(input: $input) { loginToken { token } workspace { id } } }""",
            {"input": {"displayName": "e-KNOT"}}, agnostic)
    login = d["signUpInNewWorkspace"]["loginToken"]["token"]
    d = gql("/metadata", """mutation($loginToken: String!, $origin: String!) {
              getAuthTokensFromLoginToken(loginToken: $loginToken, origin: $origin) {
                tokens { accessOrWorkspaceAgnosticToken { token } } } }""",
            {"loginToken": login, "origin": TWENTY_ORIGIN})
    access = d["getAuthTokensFromLoginToken"]["tokens"]["accessOrWorkspaceAgnosticToken"]["token"]
    for _ in range(30):  # the workspace schema is created asynchronously after activation
        try:
            gql("/metadata", """mutation($input: ActivateWorkspaceInput!) {
                  activateWorkspace(data: $input) { id activationStatus } }""",
                {"input": {"displayName": "e-KNOT"}}, access)
            break
        except RuntimeError as e:
            if "already" in str(e).lower() or "active" in str(e).lower():
                break
            time.sleep(4)
    print("  twenty: workspace «e-KNOT» active")
    roles = gql("/metadata", "{ getRoles { id label } }", token=access)["getRoles"]
    admin = next((r for r in roles if r["label"].lower() == "admin"), None) or die(f"no Admin role: {roles}")
    expires = "2099-12-31T00:00:00.000Z"
    key_id = gql("/metadata", """mutation($input: CreateApiKeyInput!) { createApiKey(input: $input) { id } }""",
                 {"input": {"name": "salesbot cards", "expiresAt": expires, "roleId": admin["id"]}},
                 access)["createApiKey"]["id"]
    key = gql("/metadata", """mutation($apiKeyId: UUID!, $expiresAt: String!) {
                generateApiKeyToken(apiKeyId: $apiKeyId, expiresAt: $expiresAt) { token } }""",
              {"apiKeyId": key_id, "expiresAt": expires}, access)["generateApiKeyToken"]["token"]
    if not twenty_key_works(key):
        die("the new API key does not work")
    set_env({"TWENTY_API_KEY": key})


# ---------------------------------------------------------------- twenty: data model

CHANNELS = [("WA_GROUP", "Группа WhatsApp", "green"), ("WA_DM", "Личка WhatsApp", "turquoise"),
            ("CALL_IN", "Входящий звонок", "blue"), ("CALL_OUT", "Исходящий звонок", "purple"),
            ("TELEGRAM", "Telegram", "sky")]
# The funnel, in order. orchestrator/cards.py moves a card only forward along it (LOST from anywhere).
STAGES = [("NEW", "Потребность замечена", "gray"), ("OFFERED", "Предложение отправлено", "sky"),
          ("REPLIED", "Ответил", "blue"), ("QUALIFIED", "Квалифицирован", "purple"),
          ("LEAD", "Заявка менеджеру", "orange"), ("CUSTOMER", "Подключили", "green"), ("LOST", "Отказ", "red")]
ROLES = [("CHAIRMAN", "Председатель ОСИ", "purple"), ("COUNCIL", "Член совета дома", "blue"),
         ("UK", "Управляющая компания", "orange"), ("OWNER", "Собственник", "green"),
         ("TENANT", "Арендатор", "yellow"), ("UNKNOWN", "Не известно", "gray")]


REVIEW_OUTCOMES = [("LEAD", "Заявка", "green"), ("INTERESTED", "Интерес не закрыт", "sky"),
                   ("TASK_DONE", "Задача звонка выполнена", "turquoise"), ("NO_INTEREST", "Не интересно", "gray"),
                   ("OPTED_OUT", "Отказ от сообщений", "red"), ("WRONG_TARGET", "Не тот адресат", "yellow"),
                   ("UNRESOLVED", "Вопрос не решён", "orange"), ("OTHER", "Другое", "purple")]
SENTIMENTS = [("POSITIVE", "Позитивное", "green"), ("NEUTRAL", "Нейтральное", "gray"), ("NEGATIVE", "Негативное", "red")]


def options(items):
    return [{"value": v, "label": l, "color": c, "position": i} for i, (v, l, c) in enumerate(items)]


def services():
    cat = json.loads((STACK / "group-watcher" / "services.json").read_text(encoding="utf-8"))
    colors = ["blue", "green", "purple", "orange", "turquoise", "sky", "pink", "yellow", "red"]
    items = [(s["id"].upper(), s["name"][:60], colors[i % len(colors)]) for i, s in enumerate(cat["services"])]
    return items + [("OTHER", "Другое / не определено", "gray")]


def model_fields():
    """(object, name, label, type, options or None). Names are what cards.py writes."""
    return [
        ("person", "residentRole", "Роль", "SELECT", options(ROLES)),
        ("person", "chatwootContactId", "Контакт Chatwoot", "NUMBER", None),
        ("person", "chatwootLink", "Chatwoot", "LINKS", None),
        ("person", "optOut", "Отказался от сообщений", "BOOLEAN", None),
        ("person", "lastChannel", "Последний канал", "SELECT", options(CHANNELS)),
        ("person", "lastTouchAt", "Последнее касание", "DATE_TIME", None),
        ("person", "lastOutcome", "Итог обзвона", "TEXT", None),
        ("opportunity", "service", "Услуга", "SELECT", options(services())),
        ("opportunity", "source", "Источник", "SELECT", options(CHANNELS)),
        ("opportunity", "needQuote", "Повод", "TEXT", None),
        ("opportunity", "chatwootConversationId", "Диалог Chatwoot", "NUMBER", None),
        ("opportunity", "lostReason", "Причина отказа", "TEXT", None),
        # The analyst's review (orchestrator/review.py): the last one of this opportunity's dialog or call.
        ("opportunity", "reviewScore", "Оценка агента (1–5)", "NUMBER", None),
        ("opportunity", "reviewCsat", "Удовлетворённость (прогноз 1–5)", "NUMBER", None),
        ("opportunity", "reviewOutcome", "Итог по разбору", "SELECT", options(REVIEW_OUTCOMES)),
        ("opportunity", "reviewNextStep", "Следующий шаг (разбор)", "TEXT", None),
        ("opportunity", "reviewedAt", "Разобрано", "DATE_TIME", None),
        ("person", "lastCsat", "Удовлетворённость (прогноз 1–5)", "NUMBER", None),
        ("person", "lastSentiment", "Настроение в конце разговора", "SELECT", options(SENTIMENTS)),
    ]


def step_model():
    key = env("TWENTY_API_KEY")
    twenty_key_works(key) or die("no working TWENTY_API_KEY: run the `twenty` step")
    objs = gql("/metadata", """{ objects(paging: {first: 200}) { edges { node { id nameSingular
              fields(paging: {first: 300}) { edges { node { id name type options } } } } } } }""", token=key)
    objs = {e["node"]["nameSingular"]: e["node"] for e in objs["objects"]["edges"]}
    have = {(o, f["node"]["name"]): f["node"] for o in ("person", "opportunity")
            for f in objs[o]["fields"]["edges"]}
    stage = have[("opportunity", "stage")]
    want = options(STAGES)
    if [o["value"] for o in stage["options"]] != [o["value"] for o in want]:
        gql("/metadata", """mutation($input: UpdateOneFieldMetadataInput!) { updateOneField(input: $input) { id } }""",
            {"input": {"id": stage["id"], "update": {"options": want, "defaultValue": "'NEW'"}}}, key)
        print("  twenty: opportunity stages = the e-KNOT funnel")
    for obj, name, label, typ, opts in model_fields():
        cur = have.get((obj, name))
        if cur is None:
            field = {"objectMetadataId": objs[obj]["id"], "type": typ, "name": name, "label": label}
            if opts:
                field["options"] = opts
            gql("/metadata", """mutation($input: CreateOneFieldMetadataInput!) { createOneField(input: $input) { id } }""",
                {"input": {"field": field}}, key)
            print(f"  twenty: {obj}.{name} ({typ})")
        elif opts and [o["value"] for o in cur["options"] or []] != [o["value"] for o in opts]:
            gql("/metadata", """mutation($input: UpdateOneFieldMetadataInput!) { updateOneField(input: $input) { id } }""",
                {"input": {"id": cur["id"], "update": {"options": opts}}}, key)
            print(f"  twenty: {obj}.{name} options updated")
    print("  twenty: data model ok")


STEPS = {"hatchet": step_hatchet, "twenty": step_twenty, "model": step_model}

if __name__ == "__main__":
    names = sys.argv[1:] or sys.exit(__doc__)
    if names == ["all"]:
        names = ["hatchet", "twenty", "model"]
    for n in names:
        (STEPS.get(n) or die(f"unknown step {n}"))()
    note_logins()
