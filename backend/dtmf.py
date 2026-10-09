"""In-band DTMF: the same keypad tones a phone sends, generated as audio and decoded with Goertzel.

In-band on purpose. It survives any audio path, including the PSTN, which is the point of the binding.
"""

import numpy as np

RATE = 48000
ROWS = (697, 770, 852, 941)
COLS = (1209, 1336, 1477)
KEYS = ("123", "456", "789", "*0#")
TONE_S, GAP_S = 0.12, 0.08


def tones(digits: str) -> np.ndarray:
    t = np.arange(int(RATE * TONE_S)) / RATE
    gap = np.zeros(int(RATE * GAP_S))
    out = []
    for d in digits:
        r = next(i for i, row in enumerate(KEYS) if d in row)
        c = KEYS[r].index(d)
        out += [0.25 * (np.sin(2 * np.pi * ROWS[r] * t) + np.sin(2 * np.pi * COLS[c] * t)), gap]
    return (np.concatenate(out) * 32767).astype(np.int16)


def _power(x: np.ndarray, freq: float) -> float:
    k = 2 * np.cos(2 * np.pi * freq / RATE)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + k * s1 - s2, s1
    return s1 * s1 + s2 * s2 - k * s1 * s2


def decode(pcm: np.ndarray) -> str:
    """Find tone bursts by energy, then name each one by its strongest row and column frequency."""
    x = pcm.astype(np.float64) / 32768
    win = int(RATE * 0.01)
    energy = np.array([np.sqrt(np.mean(x[i:i + win] ** 2)) for i in range(0, len(x) - win, win)])
    loud = energy > 0.05
    digits, i = [], 0
    while i < len(loud):
        if not loud[i]:
            i += 1
            continue
        j = i
        while j < len(loud) and loud[j]:
            j += 1
        if j - i >= 5:  # at least 50ms of tone
            mid = x[(i + 1) * win:(j - 1) * win][: int(RATE * 0.04)]  # 40ms from the steady part
            r = int(np.argmax([_power(mid, f) for f in ROWS]))
            c = int(np.argmax([_power(mid, f) for f in COLS]))
            digits.append(KEYS[r][c])
        i = j
    return "".join(digits)
