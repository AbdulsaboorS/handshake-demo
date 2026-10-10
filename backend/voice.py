"""The call as audio. Both parties sit on Cloudflare Realtime. A line is synthesized into one party's
track, travels through the SFU, and the other party transcribes what actually arrived. Nobody gets the
other side's text for free.
"""

import asyncio
import io
import os
import time
import wave
from collections import deque

import httpx
import numpy as np

from backend import dtmf
from backend.llm import post_with_retry
from backend.realtime import RATE, Peer, RealtimeAPI

VOICES = {"airline": "asteria", "agent": "orion"}
SPEECH_RMS = 300
END_OF_TURN_S = 0.55
PREROLL_FRAMES = 3

_ai = httpx.AsyncClient(
    base_url=f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run",
    headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"},
    timeout=30,
)


async def tts(text: str, voice: str) -> np.ndarray:
    r = await post_with_retry(_ai, "/@cf/deepgram/aura-1", json={
        "text": text, "speaker": voice, "encoding": "linear16", "sample_rate": RATE, "container": "none",
    })
    return np.frombuffer(r.content, dtype=np.int16)


async def stt(pcm: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm[::3].tobytes())  # 48k -> 16k is plenty for speech
    r = await post_with_retry(_ai, "/@cf/deepgram/nova-3?smart_format=true", content=buf.getvalue(),
                              headers={"Content-Type": "audio/wav"})
    return r.json()["result"]["results"]["channels"][0]["alternatives"][0]["transcript"]


class Ear:
    """Energy endpointing on incoming audio: an utterance starts at the first loud frame and ends after
    END_OF_TURN_S of quiet."""

    def __init__(self):
        self.utterances: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.hearing = asyncio.Event()
        self._buf: list[np.ndarray] = []
        self._pre: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self._quiet = 0

    def feed(self, pcm: np.ndarray) -> None:
        self.hearing.set()
        loud = np.sqrt(np.mean(pcm.astype(np.float64) ** 2)) > SPEECH_RMS
        if not self._buf:
            if loud:
                self._buf = [*self._pre, pcm]
                self._quiet = 0
            else:
                self._pre.append(pcm)
            return
        self._buf.append(pcm)
        self._quiet = 0 if loud else self._quiet + len(pcm)
        if self._quiet >= END_OF_TURN_S * RATE:
            self.utterances.put_nowait(np.concatenate(self._buf))
            self._buf = []
            self._pre.clear()

    @property
    def mid_utterance(self) -> bool:
        return bool(self._buf)

    async def collect(self, timeout: float) -> np.ndarray:
        """Everything heard since the last collect, once the speaker has gone quiet."""
        parts = [await asyncio.wait_for(self.utterances.get(), timeout)]
        while self.mid_utterance or not self.utterances.empty():
            parts.append(await asyncio.wait_for(self.utterances.get(), timeout))
        return np.concatenate(parts)


class VoiceLine:
    def __init__(self):
        self.api = RealtimeAPI()
        self.peers = {"agent": Peer(self.api, "pocket"), "airline": Peer(self.api, "northwind")}
        self.ears = {"agent": Ear(), "airline": Ear()}

    async def open(self) -> None:
        agent, airline = self.peers["agent"], self.peers["airline"]
        await asyncio.gather(agent.join(), airline.join())
        await asyncio.gather(
            airline.listen(agent, self.ears["airline"].feed),
            agent.listen(airline, self.ears["agent"].feed),
        )
        # Media flows a beat after negotiation. Speaking before both ears hear anything clips the first words.
        await asyncio.wait_for(asyncio.gather(*(e.hearing.wait() for e in self.ears.values())), 10)

    def tracks(self) -> list[tuple[str, str]]:
        return [(p.session, p.track_name) for p in self.peers.values()]

    def _other(self, speaker: str) -> str:
        return "airline" if speaker == "agent" else "agent"

    async def _transmit(self, speaker: str, pcm: np.ndarray, timing: dict) -> np.ndarray:
        ear = self.ears[self._other(speaker)]
        while not ear.utterances.empty():
            ear.utterances.get_nowait()
        timing["play_start"] = time.time()
        await self.peers[speaker].mouth.play(pcm)
        timing["play_end"] = time.time()
        heard = await ear.collect(timeout=END_OF_TURN_S + 5)
        timing["endpoint_s"] = time.time() - timing["play_end"]
        return heard

    async def send_code(self, code: str) -> str:
        return dtmf.decode(await self._transmit("agent", dtmf.tones(code), {}))

    async def speak(self, speaker: str, text: str) -> tuple[str, dict]:
        """Returns what the other side heard, and where the time went (wall clock, for evals)."""
        if not text.strip():  # a model can end a turn without saying anything; aura-1 rejects empty text
            return "", {}
        timing = {}
        t = time.time()
        pcm = await tts(text, VOICES[speaker])
        timing["tts_s"] = time.time() - t
        audio = await self._transmit(speaker, pcm, timing)
        t = time.time()
        heard = await stt(audio)
        timing["stt_s"] = time.time() - t
        return heard, timing

    async def close(self) -> None:
        await asyncio.gather(*(p.leave() for p in self.peers.values()))
        await self.api.close()


async def listen_in(api: RealtimeAPI, tracks: list[tuple[str, str]]) -> tuple[str, dict]:
    """Start a listener session for the browser. Returns the session and the SFU's offer to answer.
    The app token never leaves the server."""
    session = await api.new_session()
    data = await api.subscribe(session, tracks)
    return session, data["sessionDescription"]

