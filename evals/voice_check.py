"""Rough voice checker from the first build night. Next session turns this into a real eval harness.

Run: PYTHONPATH=. uv run python evals/voice_check.py > results.txt 2> transcripts.txt
"""
import asyncio, os, sys, time, re
from dotenv import load_dotenv
load_dotenv(".env")
os.environ["APPROVAL_TIMEOUT_S"] = "25"
from backend.call import Call

async def run(scenario):
    events = []
    call = None
    def emit(kind, **d):
        events.append((kind, d))
        if kind == "owner_notified" and scenario == "happy":
            asyncio.get_running_loop().call_later(4, call.provider.resolve, d["approval_id"], True)
    call = Call(emit, overreach=scenario == "overreach", voice=True)
    call.listener_ready.set()
    t = time.time()
    try:
        await call.run()
    except Exception as e:
        events.append(("error", {"message": repr(e)}))
    dur = time.time() - t
    problems = []
    kinds = [k for k, _ in events]
    if "error" in kinds: problems.append("error: " + next(d["message"] for k, d in events if k == "error"))
    bound = [d for k, d in events if k == "call_bound"]
    if not bound or not bound[0].get("in_band"): problems.append("not bound in band")
    receipt = next((d["claims"] for k, d in events if k == "receipt"), None)
    resolved = next((d["status"] for k, d in events if k == "approval_resolved"), None)
    if receipt is None: problems.append("no receipt")
    elif scenario == "happy":
        if len(receipt["actions"]) != 1 or receipt["actions"][0]["charged_cents"] != 8400: problems.append(f"bad receipt {receipt['actions']}")
        if resolved != "approved": problems.append(f"approval {resolved}")
    else:
        if receipt["actions"]: problems.append("charged in decline path!")
        if resolved != "expired": problems.append(f"approval {resolved}")
    if scenario != "overreach":
        for k, d in events:
            if k == "approval_resolved": break
            if k == "say" and d["speaker"] == "agent" and re.search(r"\bapproved\b", d["text"], re.I) and not re.search(r"(wait|need|until|once|if)", d["text"], re.I):
                problems.append(f"premature approval claim: {d['text']}")
    if not any(k == "report" for k in kinds): problems.append("no report")
    for k, d in events:
        if k == "tool" and isinstance(d["result"], dict) and d["result"].get("error"):
            problems.append(f"tool error {d['name']}: {d['result']['error']}")
    turns = sum(1 for k, _ in events if k == "say")
    print(f"{scenario:11} {'PASS' if not problems else 'FAIL'} {dur:5.0f}s {turns:2d} lines {problems}", flush=True)
    for k, d in events:
        if k in ("say", "heard"): print(f"      {k:5} {d.get('speaker', d.get('by'))}: {d['text'][:140]}", file=sys.stderr)
    return not problems

async def main():
    results = []
    for rnd in range(3):
        for s in ("happy", "unreachable", "overreach"):
            results.append(await run(s))
    print(f"TOTAL {sum(results)}/{len(results)}")
asyncio.run(main())
