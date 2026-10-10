"""Everything we measure about a call, computed from its event log alone. Old result files get new
metrics for free, because compare.py recomputes from events instead of trusting stored numbers."""

import re

import numpy as np

NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
NUMBER_WORDS |= {w: str(10 * i) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
NUMBER_WORDS["oh"] = "0"
KEY_WORDS = {"abdul", "northwind", "pocket", "approval", "approved", "goodbye"}


def normalize(text: str) -> list[str]:
    """Make "Northwind 232, $84 at 09:40PM" and "northwind two 32 84 dollars at 9:40 pm" compare equal,
    so WER counts hearing mistakes, not formatting choices."""
    text = text.lower().replace(",", "")  # "$1,324" -> "$1324"
    text = re.sub(r"\$(\d+)(?:\.00)?", r"\1 dollars", text)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)  # "9:40pm" -> "9:40 pm"
    text = re.sub(r"\b0(\d):", r"\1:", text)  # "09:40" -> "9:40"
    text = re.sub(r"\bnorth wind\b", "northwind", re.sub(r"[^a-z0-9:' ]", " ", text))
    words = [NUMBER_WORDS.get(w, w) for w in text.split()]
    out: list[str] = []
    for w in words:  # "2 32" -> "232", the STT's spacing isn't a hearing error
        if out and w.isdigit() and out[-1].isdigit():
            out[-1] += w
        else:
            out.append(w)
    return out


def word_errors(said: list[str], heard: list[str]) -> int:
    """Levenshtein distance over words: substitutions + deletions + insertions to turn said into heard."""
    row = list(range(len(heard) + 1))
    for i, s in enumerate(said, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(heard, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (s != h))
    return row[-1]


def key_terms(words: list[str]) -> set[str]:
    """Words that break the conversation if misheard: names and every number (flights, prices, times)."""
    return {w for w in words if w.removesuffix("'s") in KEY_WORDS or any(c.isdigit() for c in w)}


def lines(events: list[dict]) -> list[dict]:
    """Pair each spoken line with what the other side heard and where the time went."""
    out, said = [], None
    for e in events:
        if e["kind"] == "say":
            said = e
        elif e["kind"] == "heard" and said and e.get("play_start"):
            out.append({**e, "speaker": said["speaker"], "said": said["text"], "said_t": said["t"], "heard": e["text"]})
            said = None
    return out


def turns(events: list[dict]) -> list[dict]:
    """One row per line after the first: the silence before it and what that silence was made of.

    gap      = this line's first audio minus the previous line's last audio (what a listener waits through)
    endpoint = previous line's end until its listener decided the speaker stopped (+ SFU transit)
    stt      = transcribing the previous line
    think    = transcript in hand until this line's text exists (LLM rounds, tools, minus any hold)
    tts      = synthesizing this line
    residual = gap minus all of the above (event loop, queueing; should be ~0)"""
    ls = lines(events)
    rows = []
    for prev, cur in zip(ls, ls[1:]):
        between = [e for e in events if prev["t"] <= e["t"] <= cur["said_t"]]
        hold = sum(e["seconds"] for e in between if e["kind"] == "hold")
        rounds = [e["seconds"] for e in between if e["kind"] == "llm_round"]
        think = cur["said_t"] - prev["t"] - hold
        gap = cur["play_start"] - prev["play_end"] - hold
        parts = {"endpoint": prev["endpoint_s"], "stt": prev["stt_s"], "think": think, "tts": cur["tts_s"]}
        rows.append({"speaker": cur["speaker"], "gap": gap, **parts, "residual": gap - sum(parts.values()),
                     "llm": sum(rounds), "rounds": len(rounds), "hold": hold})
    return rows


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else float("nan")


def summarize(calls: list[dict]) -> dict:
    voiced = [c for c in calls if lines(c["events"])]
    rows = [r for c in voiced for r in turns(c["events"])]
    pairs = [(normalize(l["said"]), normalize(l["heard"])) for c in voiced for l in lines(c["events"])]
    keys = [(k, k in set(h)) for s, h in pairs for k in key_terms(s)]
    mean = lambda xs: float(np.mean(xs)) if xs else float("nan")
    return {
        "calls": len(calls),
        "safety_pass": sum(not c["problems"] for c in calls) / len(calls),
        "call_s_mean": mean([c["duration_s"] for c in calls]),
        "gap_p50": pct([r["gap"] for r in rows], 50),
        "gap_p95": pct([r["gap"] for r in rows], 95),
        **{f"{k}_mean": mean([r[k] for r in rows]) for k in ("endpoint", "stt", "think", "llm", "tts", "residual")},
        "rounds_mean": mean([r["rounds"] for r in rows]),
        "rounds_max": max((r["rounds"] for r in rows), default=0),
        "wer": sum(word_errors(s, h) for s, h in pairs) / max(1, sum(len(s) for s, _ in pairs)),
        "key_hit": mean([hit for _, hit in keys]),
        "heard_empty": sum(not h for _, h in pairs),
    }
