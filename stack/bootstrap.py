#!/usr/bin/env python3
"""First-run configuration of the stack. Runs INSIDE the VM (python3 -I stack/bootstrap.py <step>).

Steps (idempotent where possible; secrets go to ~/secrets/*.env with mode 600 and are never printed):
  chatwoot   create test account + admin user in Chatwoot, mint its API token
  agents     first-run /setup of agents (token read from container logs), store admin credentials
  vault      store the OpenRouter key (from ~/secrets/openrouter.env) in the agents vault
  agent      create/update the "ЖКХ ассистент" agent (OpenRouter model, system prompt, test mode)
  connect    connect agents to Chatwoot (private docker network) and select the account
  bind INBOX_ID   bind a Chatwoot inbox (mirror id from `inboxes`) to the agent
  inboxes    list inboxes known to agents
  playground smoke test of the agent through the playground (no WhatsApp)
  all        chatwoot agents vault agent connect
"""
import json, os, pathlib, re, secrets, string, subprocess, sys, urllib.error, urllib.request

HOME = pathlib.Path.home()
STACK = pathlib.Path(__file__).resolve().parent
SECRETS = HOME / "secrets"
AGENTS = "http://127.0.0.1:3100/api"
MODEL = os.environ.get("AGENT_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
AGENT_NAME = "ЖКХ ассистент (тест)"
VAULT_NAME = "OpenRouter"
CW_ACCOUNT = "УК Тестовая (ЖКХ)"
CW_EMAIL = "admin@salesbot.test"
AG_EMAIL = "admin@salesbot.test"


def die(msg):
    sys.exit(f"bootstrap: {msg}")


def read_env(name):
    p = SECRETS / name
    out = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(name, data):
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    p = SECRETS / name
    old = os.umask(0o077)
    try:
        p.write_text("".join(f"{k}={v}\n" for k, v in data.items()))
    finally:
        os.umask(old)
    p.chmod(0o600)
    print(f"  wrote {p} (600): {', '.join(data)}")


def gen_password():
    # Chatwoot policy: upper, lower, digit, special.
    core = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    return core + "Aa1!"


def compose(*args, stdin=None):
    return subprocess.run(["docker", "compose", *args], cwd=STACK, input=stdin, capture_output=True, text=True)


# ---------------------------------------------------------------- Chatwoot
CW_RUBY = r'''
require 'json'
email = ENV.fetch('BS_EMAIL'); pass = ENV.fetch('BS_PASS'); acc_name = ENV.fetch('BS_ACCOUNT')
acc = Account.find_by(name: acc_name) || Account.create!(name: acc_name, locale: 'ru')
u = User.find_by(email: email)
created = false
unless u
  u = SuperAdmin.new(name: 'Test Admin', email: email, password: pass, password_confirmation: pass)
  u.skip_confirmation!
  u.save!
  created = true
end
au = AccountUser.find_by(account: acc, user: u) || AccountUser.create!(account: acc, user: u, role: :administrator)
::Redis::Alfred.delete(::Redis::Alfred::CHATWOOT_INSTALLATION_ONBOARDING) rescue nil
at = AccessToken.find_or_create_by!(owner: u)
puts 'BS_JSON:' + JSON.generate({ account_id: acc.id, user_id: u.id, created: created, token: at.token })
'''


def step_chatwoot():
    cur = read_env("chatwoot-admin.env")
    pw = cur.get("CW_ADMIN_PASSWORD") or gen_password()
    # Secrets travel via stdin into a file inside the container, never via argv.
    script = "ENV['BS_EMAIL']=%s\nENV['BS_PASS']=%s\nENV['BS_ACCOUNT']=%s\n" % (
        json.dumps(CW_EMAIL), json.dumps(pw), json.dumps(CW_ACCOUNT)) + CW_RUBY
    r = compose("exec", "-T", "cw-rails", "sh", "-c",
                "umask 077; cat > /tmp/bs.rb && bundle exec rails runner /tmp/bs.rb; rc=$?; rm -f /tmp/bs.rb; exit $rc",
                stdin=script)
    m = re.search(r"^BS_JSON:(.*)$", r.stdout, re.M)
    if r.returncode != 0 or not m:
        die("chatwoot bootstrap failed:\n" + (r.stderr or r.stdout)[-1500:])
    res = json.loads(m.group(1))
    write_env("chatwoot-admin.env", {"CW_URL": "http://localhost:3000", "CW_ADMIN_EMAIL": CW_EMAIL,
                                     "CW_ADMIN_PASSWORD": pw, "CW_ACCOUNT_ID": str(res["account_id"]),
                                     "CW_API_TOKEN": res["token"]})
    print(f"  chatwoot: account {res['account_id']} '{CW_ACCOUNT}', admin {CW_EMAIL} (created={res['created']})")


# ---------------------------------------------------------------- agents HTTP
class Agents:
    def __init__(self):
        self.cookie = None

    def req(self, method, path, body=None, ok=(200, 201, 204)):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        r = urllib.request.Request(AGENTS + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=180) as resp:
                self._grab(resp)
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw[:400].decode(errors="replace")
            if e.code in ok:
                return e.code, payload
            return e.code, payload

    def _grab(self, resp):
        # Secure cookie over http://127.0.0.1: keep it manually (cookiejar would refuse to send it).
        cookies = [c.split(";", 1)[0] for c in resp.headers.get_all("Set-Cookie") or []]
        if cookies:
            self.cookie = "; ".join(cookies)

    def login(self):
        cred = read_env("agents-admin.env")
        if not cred:
            die("no ~/secrets/agents-admin.env; run the 'agents' step first")
        st, b = self.req("POST", "/auth/login", {"email": cred["AGENTS_ADMIN_EMAIL"], "password": cred["AGENTS_ADMIN_PASSWORD"]})
        if st != 200 or not self.cookie:
            die(f"agents login failed: {st} {str(b)[:200]}")
        return self


def setup_token_from_logs():
    r = compose("logs", "--no-color", "agents")
    m = re.findall(r"/setup\?token=([A-Za-z0-9_\-.~%]+)", r.stdout + r.stderr)
    return m[-1] if m else None


def step_agents():
    if read_env("agents-admin.env"):
        Agents().login()
        print("  agents: admin already set up, login OK")
        return
    tok = setup_token_from_logs()
    if not tok:
        die("setup token not found in agents logs (is the container up? already set up?)")
    pw = gen_password()
    a = Agents()
    st, b = a.req("POST", "/auth/setup", {"email": AG_EMAIL, "password": pw, "name": "Test Admin",
                                         "companyName": "УК Тестовая", "token": tok})
    if st not in (200, 201):
        die(f"agents /auth/setup failed: {st} {str(b)[:300]}")
    write_env("agents-admin.env", {"AGENTS_URL": "http://localhost:3100", "AGENTS_ADMIN_EMAIL": AG_EMAIL,
                                   "AGENTS_ADMIN_PASSWORD": pw})
    print("  agents: first admin created")


def find_vault(a):
    st, b = a.req("GET", "/v1/vault")
    items = b if isinstance(b, list) else (b or {}).get("items") or (b or {}).get("data") or []
    for it in items:
        if it.get("name") == VAULT_NAME:
            return it
    return None


def step_vault():
    a = Agents().login()
    if find_vault(a):
        print(f"  vault: '{VAULT_NAME}' already exists")
        return
    key = read_env("openrouter.env").get("OPENROUTER_API_KEY")
    if not key:
        die("no OPENROUTER_API_KEY in ~/secrets/openrouter.env")
    st, b = a.req("POST", "/v1/vault/test", {"kind": "openrouter", "value": key})
    print(f"  vault test: HTTP {st} {json.dumps(b, ensure_ascii=False)[:160] if b else ''}")
    st, b = a.req("POST", "/v1/vault", {"name": VAULT_NAME, "kind": "openrouter", "value": key})
    if st not in (200, 201):
        die(f"vault create failed: {st} {str(b)[:300]}")
    print(f"  vault: '{VAULT_NAME}' stored (kind=openrouter)")


def find_agent(a):
    st, b = a.req("GET", "/v1/agents")
    items = b if isinstance(b, list) else (b or {}).get("items") or (b or {}).get("data") or []
    return next((x for x in items if x.get("name") == AGENT_NAME), None)


def step_agent():
    a = Agents().login()
    v = find_vault(a) or die("vault entry missing; run 'vault'")
    prompt = (STACK / "agent" / "system-prompt.md").read_text(encoding="utf-8")
    body = {
        "name": AGENT_NAME,
        "systemPrompt": prompt,
        "enabled": True,
        "mode": "test",  # silent in every conversation until someone sends /teste in it
        "kind": "text",
        "modelConfig": {"provider": "openrouter", "model": MODEL, "credentialRef": f"vault:{v['id']}",
                        "temperature": 0.3},
        "settings": {
            "handoff": {"mode": "route", "instructions":
                        "Передай оператору, если клиент просит человека, жалуется, нужна точная сумма/тариф/дата/"
                        "юридическая формулировка, которой нет в инструментах, или аварийная ситуация."},
            "debounce": {"enabled": True, "windowSeconds": 4, "maxWindowSeconds": 30, "maxMessagesPerBurst": 10},
        },
    }
    cur = find_agent(a)
    if cur:
        st, b = a.req("PATCH", f"/v1/agents/{cur['id']}", body)
        print(f"  agent: updated id={cur['id']} HTTP {st} {'' if st < 300 else str(b)[:300]}")
    else:
        st, b = a.req("POST", "/v1/agents", body)
        if st not in (200, 201):
            die(f"agent create failed: {st} {str(b)[:400]}")
        print(f"  agent: created id={(b or {}).get('id')} model={MODEL} mode=test")


def step_connect():
    a = Agents().login()
    cw = read_env("chatwoot-admin.env") or die("run 'chatwoot' first")
    st, b = a.req("GET", "/v1/chatwoot/deployment")
    body = {"baseUrl": "http://cw-rails:3000", "adminToken": cw["CW_API_TOKEN"],
            "webhookBaseUrl": "http://agents:3000", "publicUrl": "http://localhost:3000"}
    if st == 200 and b:
        st, b = a.req("PATCH", "/v1/chatwoot/deployment", body)
    else:
        st, b = a.req("POST", "/v1/chatwoot/deployment", body)
    print(f"  deployment: HTTP {st} {'' if st < 300 else str(b)[:400]}")
    st, b = a.req("PUT", "/v1/chatwoot/deployment/accounts", {"accountIds": [int(cw["CW_ACCOUNT_ID"])]})
    print(f"  accounts: HTTP {st} {'' if st < 300 else str(b)[:400]}")


def step_inboxes():
    a = Agents().login()
    st, b = a.req("GET", "/v1/chatwoot/inboxes")
    print(json.dumps(b, ensure_ascii=False, indent=1)[:3000])


def step_bind(inbox_id):
    a = Agents().login()
    ag = find_agent(a) or die("agent missing")
    st, b = a.req("PATCH", f"/v1/chatwoot/inboxes/{inbox_id}", {"agentId": str(ag["id"])})
    print(f"  bind inbox {inbox_id} -> agent {ag['id']}: HTTP {st} {'' if st < 300 else str(b)[:400]}")


def step_playground():
    a = Agents().login()
    ag = find_agent(a) or die("agent missing")
    thread = None
    for msg in ["Здравствуйте!", "У нас в подъезде не работает домофон, что делать?",
                "Сколько стоит подключить домофон?", "Кто выиграл чемпионат мира по футболу?",
                "Позовите, пожалуйста, живого оператора."]:
        body = {"message": msg, "guardrails": False}
        if thread:
            body["threadId"] = thread
        st, b = a.req("POST", f"/v1/agents/{ag['id']}/playground", body)
        if st != 200:
            print(f"  [{st}] {msg} -> {str(b)[:300]}")
            continue
        thread = b.get("threadId", thread)
        tools = [t.get("name") or t.get("tool") for t in (b.get("trace") or []) if isinstance(t, dict)
                 and (t.get("type") in ("tool", "tool_call") or t.get("name"))]
        print(f"  > {msg}\n  < {(b.get('reply') or '').strip()[:300]}\n    trace: {json.dumps(b.get('trace'), ensure_ascii=False)[:300]}")


STEPS = {"chatwoot": step_chatwoot, "agents": step_agents, "vault": step_vault, "agent": step_agent,
         "connect": step_connect, "inboxes": step_inboxes, "playground": step_playground}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        die(__doc__)
    cmd = sys.argv[1]
    if cmd == "all":
        for s in ("chatwoot", "agents", "vault", "agent", "connect"):
            print(f"== {s}"); STEPS[s]()
    elif cmd == "bind":
        step_bind(sys.argv[2])
    elif cmd in STEPS:
        STEPS[cmd]()
    else:
        die(__doc__)
