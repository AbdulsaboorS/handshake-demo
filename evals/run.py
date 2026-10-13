"""Run the call scenarios, check the safety invariants, and save every event to evals/results/<label>.json.

  uv run python -m evals.run --label baseline-a                  # 3 runs x 3 scenarios over voice
  uv run python -m evals.run --label quick --scenarios happy     # latency iteration, ~5 min
  uv run python -m evals.run --label harness --text --runs 1     # no voice, checks the harness itself
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")
os.environ["APPROVAL_TIMEOUT_S"] = "25"

from backend.call import Call  # noqa: E402
from evals.metrics import check, summarize  # noqa: E402

APPROVE_AFTER_S = 4
RESULTS = Path(__file__).parent / "results"


async def run_call(scenario: str, voice: bool) -> dict:
    events: list[dict] = []
    call = None

    def emit(kind: str, **data) -> None:
        events.append({"kind": kind, "t": time.time(), **data})
        if kind == "owner_notified" and scenario == "happy":
            asyncio.get_running_loop().call_later(APPROVE_AFTER_S, call.provider.resolve, data["approval_id"], True)

    call = Call(emit, overreach=scenario == "overreach", voice=voice)
    call.listener_ready.set()
    started = time.time()
    try:
        await call.run()
    except Exception as e:
        emit("error", message=repr(e))
    return {"scenario": scenario, "duration_s": time.time() - started, "events": events}


def git_state() -> str:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "backend"], capture_output=True, text=True).stdout.strip()
    return sha + ("+dirty" if dirty else "")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="results file name, e.g. baseline-a or preroll-15")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenarios", default="happy,unreachable,overreach")
    parser.add_argument("--text", action="store_true", help="text line instead of voice")
    parser.add_argument("--note", default="", help="what changed in this run, in a few words")
    args = parser.parse_args()

    out = RESULTS / f"{args.label}.json"
    if out.exists():
        raise SystemExit(f"{out} exists. Pick a new label; results are a record, not a scratch file.")

    git = git_state()  # at start: code edited mid-run isn't what's running
    calls = []
    for run in range(args.runs):
        for scenario in args.scenarios.split(","):
            c = await run_call(scenario, voice=not args.text)
            c["run"] = run
            calls.append(c)
            problems = check(scenario, c["events"])
            status = "; ".join(f"{k.upper()}: {p}" for k, ps in problems.items() for p in ps) or "PASS"
            print(f"{run} {scenario:11} {c['duration_s']:5.0f}s  {status}", flush=True)

    RESULTS.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "label": args.label, "note": args.note, "git": git, "voice": not args.text,
        "model": os.environ["LLM_MODEL"], "started": calls[0]["events"][0]["t"], "calls": calls,
    }, indent=1, default=str))
    s = summarize(calls)
    print(f"\nsaved {out}")
    print("  ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in s.items()))


if __name__ == "__main__":
    asyncio.run(main())
