"""Side by side: is the candidate better than the baseline, and is it still safe?

  uv run python -m evals.compare baseline-a baseline-b        # labels or paths
  uv run python -m evals.compare baseline-a preroll-15 --turns # also print every turn of the candidate
"""

import argparse
import json
from pathlib import Path

from evals.metrics import lines, normalize, summarize, turns, word_errors

RESULTS = Path(__file__).parent / "results"
LOWER_IS_BETTER = {"call_s_mean", "gap_p50", "gap_p95", "endpoint_mean", "stt_mean", "think_mean", "llm_mean",
                   "tts_mean", "residual_mean", "rounds_mean", "rounds_max", "wer", "heard_empty"}


def load(name: str) -> dict:
    path = Path(name) if name.endswith(".json") else RESULTS / f"{name}.json"
    return json.loads(path.read_text())


def print_turns(result: dict) -> None:
    for c in result["calls"]:
        print(f"\n{c['scenario']} run {c['run']}  {c['duration_s']:.0f}s")
        print(f"  {'who':7} {'gap':>5} {'endp':>5} {'stt':>5} {'think':>5} {'llm':>5} {'rnds':>4} {'tts':>5}  heard (errors)")
        ls = lines(c["events"])
        for row, line in zip([None, *turns(c["events"])], ls):
            errs = word_errors(normalize(line["said"]), normalize(line["heard"]))
            timing = "first line" if row is None else (
                f"{row['gap']:5.1f} {row['endpoint']:5.2f} {row['stt']:5.2f} {row['think']:5.1f} "
                f"{row['llm']:5.1f} {row['rounds']:4d} {row['tts']:5.2f}")
            print(f"  {line['speaker']:7} {timing:>41}  {line['heard'][:70]} ({errs})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--turns", action="store_true")
    args = parser.parse_args()

    a, b = load(args.baseline), load(args.candidate)
    sa, sb = summarize(a["calls"]), summarize(b["calls"])
    print(f"{'':16}{a['label']:>14}{b['label']:>14}{'delta':>10}")
    print(f"{'git':16}{a['git']:>14}{b['git']:>14}")
    for k in sa:
        va, vb = sa[k], sb[k]
        delta = vb - va
        better = (delta < 0) == (k in LOWER_IS_BETTER) and delta != 0
        mark = "" if k == "calls" else ("  better" if better else "  worse" if delta else "")
        print(f"{k:16}{va:>14.3f}{vb:>14.3f}{delta:>+10.3f}{mark}")
    if sb["safety_pass"] < 1:
        print("\nSAFETY FAIL. This change doesn't ship, whatever the latency says:")
        for c in b["calls"]:
            for p in c["problems"]:
                print(f"  {c['scenario']} run {c['run']}: {p}")
    if args.turns:
        print_turns(b)


if __name__ == "__main__":
    main()
