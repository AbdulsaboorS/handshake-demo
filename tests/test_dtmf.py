import numpy as np

from backend import dtmf


def test_round_trip_with_noise():
    clean = dtmf.tones("607698")
    noise = np.random.default_rng(0).normal(0, 400, len(clean))
    noisy = np.clip(clean.astype(np.float64) + noise, -32768, 32767).astype(np.int16)
    assert dtmf.decode(noisy) == "607698"
