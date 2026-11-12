#!/usr/bin/env python3
"""Scenario run against the REAL pipeline: the agents playground (the agent's own model, prompt, tools, knowledge
base and factual-grounding guardrail). Nothing is re-implemented here: a scenario only states what the pipeline
must DO (which tools it calls or must not call) and that the reply survived the agent's own grounding check.

    python3 -I tests/scenarios.py [name ...]        # inside the VM, from ~/work/stack

scenarios.json fields:
  say                 resident message, or a list of messages sent in turn to the same playground thread
  calls[], not_calls[] tools that must / must not be called (over the whole thread)
  no_numbers          the last reply (after a handoff: its customerMessage, as in production) states no figure except phones and figures the resident wrote
  reply_has_from_tool tool name or list: the last reply repeats at least one figure each of these tools returned
Run it after every change of model, prompt or tools. Playground has no Chatwoot conversation, so tickets it
creates carry no phone (test data).
"""
import json, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import bootstrap as b  # noqa: E402

FIGURE = re.compile(r"\d[\d\s:.,-]*\d|\d")
PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d|\b1\d\d\b")


def figures(text):
    return {re.sub(r"\D", "", f) for f in FIGURE.findall(text or "") if len(re.sub(r"\D", "", f)) >= 2}


def run(a, agent_id, sc):
    says = sc["say"] if isinstance(sc["say"], list) else [sc["say"]]
    trace, reply, thread = [], "", None
    for msg in says:
        st, r = a.req("POST", f"/v1/agents/{agent_id}/playground",
                      {"message": msg, **({"threadId": thread} if thread else {})})
        if st != 200:
            return [f"HTTP {st}: {str(r)[:200]}"], "", [t["name"] for t in trace if t.get("type") == "tool_call"], []
        turn = r.get("trace") or []
        trace += turn
        reply, thread = r.get("reply") or "", r.get("threadId")
        # The playground only simulates handoff_to_human, so the model writes one more line after it and that line
        # becomes the playground reply. In production the handoff's customerMessage is the turn's last message
        # (graph/tools/native.ts: "a successful handoff customerMessage is terminal"), so judge what the resident
        # would actually read.
        handoff = [t for t in turn if t.get("type") == "tool_call" and t.get("name") == "handoff_to_human"]
        if handoff and (handoff[-1].get("args") or {}).get("customerMessage"):
            reply = handoff[-1]["args"]["customerMessage"]
    called = [t["name"] for t in trace if t.get("type") == "tool_call"]
    errs = [f"no {t} call" for t in sc.get("calls", []) if t not in called]
    errs += [f"called {t}" for t in sc.get("not_calls", []) if t in called]
    # clean: nothing unsupported. revised: the guardrail caught claims and the agent rewrote the reply (the pipeline
    # working, reported as a note). unavailable: the reviewer model did not answer (free-tier 429 or the 30 s review
    # timeout) and the reply went out unchecked (onFailure: pass): reported as UNCHECKED, not as the bot's failure.
    # replaced / handed-off: no grounded reply went out.
    notes = []
    for g in (t for t in trace if t.get("type") == "guardrail"):
        if g.get("outcome") == "revised":
            notes += [c.get("claim", "") for c in g.get("claims") or []] or ["revised"]
        elif g.get("outcome") == "unavailable":
            notes.append("UNCHECKED: grounding reviewer unavailable")
        elif g.get("outcome") != "clean":
            errs.append(f"guardrail {g.get('outcome')}")
    if sc.get("no_numbers") and figures(PHONE.sub(" ", reply)) - figures(" ".join(says)):
        errs.append("reply states a figure")
    srcs = sc.get("reply_has_from_tool") or []
    for src in [srcs] if isinstance(srcs, str) else srcs:
        out = " ".join(str(t.get("output")) for t in trace if t.get("type") == "tool_result" and t.get("name") == src)
        if not figures(reply) & figures(out):
            errs.append(f"reply repeats no figure from {src}")
    return errs, reply, called, notes


def main():
    only = set(sys.argv[1:])
    cases = json.loads((pathlib.Path(__file__).parent / "scenarios.json").read_text())
    cases = [c for c in cases if not only or c["name"] in only]
    a = b.Agents().login()
    ag = b.find_agent(a) or b.die("agent missing")
    failed = unchecked = 0
    for sc in cases:
        errs, reply, called, notes = run(a, ag["id"], sc)
        failed += bool(errs)
        print(f"  {sc['name']:16} {'PASS' if not errs else 'FAIL: ' + '; '.join(errs)} | {called} | "
              f"{reply[:90].replace(chr(10), ' ')}", flush=True)
        unchecked += any(n.startswith("UNCHECKED") for n in notes)
        for n in dict.fromkeys(notes):
            print(f"  {'':16} {n if n.startswith('UNCHECKED') else 'revised by guardrail: ' + n[:150]}", flush=True)
    print("RESULT:", "OK" if not failed else f"{failed} failure(s)",
          f"({unchecked} not grounding-checked: reviewer unavailable)" if unchecked else "")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
