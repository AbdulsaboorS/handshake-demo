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
from collections.abc import AsyncIterator

import httpx
import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

from backend import dtmf, llm
from backend.llm import post_with_retry
from backend.realtime import RATE, Peer, RealtimeAPI

VOICES = {"airline": "af_bella", "agent": "am_michael"}
TTS_HOST = os.getenv("TTS_HOST", "cloudflare")
SPEECH_RMS = 300
END_OF_TURN_S = 0.35
PREROLL_FRAMES = 15  # 300ms. 60ms clipped the first word, since speech ramps up under the energy threshold

_ai = httpx.AsyncClient(
    base_url=f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/run",
    headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"},
    timeout=30,
)
_kokoro: Kokoro | None = None
_kokoro_loading: asyncio.Task[Kokoro] | None = None
_kokoro_lock = asyncio.Lock()


def _load_kokoro() -> Kokoro:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        "models/kokoro-v1.0.int8.onnx", sess_options=options, providers=["CPUExecutionProvider"]
    )
    return Kokoro.from_session(session, "models/voices-v1.0.bin")


async def tts(text: str, voice: str) -> np.ndarray:
    if TTS_HOST == "local":
        global _kokoro, _kokoro_loading
        if _kokoro is None:
            if _kokoro_loading is None:
                _kokoro_loading = asyncio.create_task(asyncio.to_thread(_load_kokoro))
            _kokoro = await _kokoro_loading
        async with _kokoro_lock:
            samples, sample_rate = await asyncio.to_thread(_kokoro.create, text, voice)
        if sample_rate != 24000:
            raise ValueError(f"Kokoro returned unexpected sample rate: {sample_rate}")
        return (np.repeat(np.clip(samples, -1, 1), 2) * 32767).astype(np.int16)
    r = await post_with_retry(_ai, "/@cf/deepgram/aura-1", json={
        "text": text, "speaker": {"af_bella": "asteria", "am_michael": "orion"}.get(voice, voice),
        "encoding": "linear16", "sample_rate": RATE, "container": "none",
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


async def warm_up() -> None:
    """The first model call after idle can take ~20s. Pay that while the line is still connecting."""
    await asyncio.gather(tts("Hello.", VOICES["agent"]), stt(np.zeros(RATE // 2, np.int16)),
                         llm.chat([{"role": "user", "content": "Say OK."}]), return_exceptions=True)


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
        self._warming = asyncio.create_task(warm_up())  # runs alongside the call; never holds it up
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

    def _clear(self, listener: str) -> Ear:
        ear = self.ears[listener]
        while not ear.utterances.empty():
            ear.utterances.get_nowait()
        return ear

    async def send_code(self, code: str) -> str:
        # A line waking from silence can clip the first ~70ms, which ate whole digits. Lead with a tone that
        # is never part of a code, so the clip lands on it.
        ear = self._clear("airline")
        await self.peers["agent"].mouth.play(dtmf.tones("#" + code))
        return dtmf.decode(await ear.collect(timeout=END_OF_TURN_S + 5)).lstrip("#")

    async def speak(self, speaker: str, sentences: AsyncIterator[str]) -> tuple[str, dict]:
        """Synthesize each sentence the moment it's written and play them in order, so the first one is on
        the line while the model is still writing. Returns what the other side heard, and where the time
        went (wall clock, for evals)."""
        ear = self._clear(self._other(speaker))
        timing: dict = {}
        audio: asyncio.Queue[asyncio.Task | None] = asyncio.Queue()

        async def synthesize():
            try:
                async for text in sentences:
                    timing.setdefault("first_text_t", time.time())
                    audio.put_nowait(asyncio.create_task(tts(text, VOICES[speaker])))
            finally:
                audio.put_nowait(None)

        writing = asyncio.create_task(synthesize())
        while (clip := await audio.get()) is not None:
            pcm = await clip
            if "play_start" not in timing:
                timing["play_start"] = time.time()
                timing["tts_s"] = timing["play_start"] - timing["first_text_t"]
            await self.peers[speaker].mouth.play(pcm)
        await writing
        if "play_start" not in timing:  # a model can end a turn without saying anything
            return "", {}
        timing["play_end"] = time.time()
        heard = await ear.collect(timeout=END_OF_TURN_S + 5)
        timing["endpoint_s"] = time.time() - timing["play_end"]
        t = time.time()
        text = await stt(heard)
        timing["stt_s"] = time.time() - t
        return text, timing

    async def close(self) -> None:
        await asyncio.gather(*(p.leave() for p in self.peers.values()))
        await self.api.close()


async def listen_in(api: RealtimeAPI, tracks: list[tuple[str, str]]) -> tuple[str, dict]:
    """Start a listener session for the browser. Returns the session and the SFU's offer to answer.
    The app token never leaves the server."""
    session = await api.new_session()
    data = await api.subscribe(session, tracks)
    return session, data["sessionDescription"]

