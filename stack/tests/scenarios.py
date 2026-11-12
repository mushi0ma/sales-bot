#!/usr/bin/env python3
"""Scenario run against the REAL pipeline: the agents playground (the agent's own model, prompt, tools, knowledge
base and factual-grounding guardrail). Nothing is re-implemented here: a scenario only states what the pipeline
must DO (which tools it calls or must not call) and that the reply survived the agent's own grounding check.

    python3 -I tests/scenarios.py [name ...]        # inside the VM, from ~/work/stack

scenarios.json fields: say, calls[], not_calls[], no_numbers (no figure that is not a phone in the reply;
figures the resident wrote are fine), reply_has_from_tool (the reply repeats at least one figure that this tool returned). Run it after every change
of model, prompt or tools. Playground has no conversation, so tickets it creates carry no phone (test data).
"""
import json, pathlib, re, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import bootstrap as b  # noqa: E402

FIGURE = re.compile(r"\d[\d\s:.,-]*\d|\d")
PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d|\b1\d\d\b")


def figures(text):
    return {re.sub(r"\D", "", f) for f in FIGURE.findall(text or "") if len(re.sub(r"\D", "", f)) >= 2}


def run(a, agent_id, sc):
    st, r = a.req("POST", f"/v1/agents/{agent_id}/playground", {"message": sc["say"]})
    if st != 200:
        return [f"HTTP {st}: {str(r)[:200]}"], "", []
    trace, reply = r.get("trace") or [], r.get("reply") or ""
    called = [t["name"] for t in trace if t.get("type") == "tool_call"]
    errs = [f"no {t} call" for t in sc.get("calls", []) if t not in called]
    errs += [f"called {t}" for t in sc.get("not_calls", []) if t in called]
    if any(t.get("type") == "guardrail" and t.get("outcome") not in ("clean", None) for t in trace):
        errs.append("grounding guardrail flagged the reply")
    if sc.get("no_numbers") and figures(PHONE.sub(" ", reply)) - figures(sc["say"]):
        errs.append("reply states a figure")
    src = sc.get("reply_has_from_tool")
    if src:
        out = " ".join(str(t.get("output")) for t in trace if t.get("type") == "tool_result" and t.get("name") == src)
        if not figures(reply) & figures(out):
            errs.append(f"reply repeats no figure from {src}")
    return errs, reply, called


def main():
    only = set(sys.argv[1:])
    cases = json.loads((pathlib.Path(__file__).parent / "scenarios.json").read_text())
    cases = [c for c in cases if not only or c["name"] in only]
    a = b.Agents().login()
    ag = b.find_agent(a) or b.die("agent missing")
    failed = 0
    for sc in cases:
        errs, reply, called = run(a, ag["id"], sc)
        failed += bool(errs)
        print(f"  {sc['name']:16} {'PASS' if not errs else 'FAIL: ' + '; '.join(errs)} | {called} | "
              f"{reply[:90].replace(chr(10), ' ')}", flush=True)
    print("RESULT:", "OK" if not failed else f"{failed} failure(s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
