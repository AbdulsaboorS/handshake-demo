"""Cloudflare Realtime SFU, from Python. Each party is an aiortc peer with its own SFU session: it
publishes one audio track and subscribes to the other party's. The browser joins as a listener.

API: https://developers.cloudflare.com/realtime/sfu/https-api/
"""

import asyncio
import fractions
import os

import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

RATE = 48000
FRAME = 960  # 20ms at 48kHz, what Opus wants


class RealtimeAPI:
    def __init__(self):
        app = os.environ["CF_REALTIME_APP_ID"]
        self._http = httpx.AsyncClient(
            base_url=f"https://rtc.live.cloudflare.com/v1/apps/{app}",
            headers={"Authorization": f"Bearer {os.environ['CF_REALTIME_APP_TOKEN']}"},
            timeout=20,
        )

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        r = await self._http.request(method, path, json=body)
        r.raise_for_status()
        data = r.json()
        if data.get("errorCode"):
            raise RuntimeError(f"Realtime {path}: {data['errorCode']} {data.get('errorDescription')}")
        return data

    async def new_session(self) -> str:
        return (await self._call("POST", "/sessions/new"))["sessionId"]

    async def publish(self, session: str, offer: RTCSessionDescription, mid: str, name: str) -> RTCSessionDescription:
        data = await self._call("POST", f"/sessions/{session}/tracks/new", {
            "sessionDescription": {"type": offer.type, "sdp": offer.sdp},
            "tracks": [{"location": "local", "mid": mid, "trackName": name}],
        })
        return RTCSessionDescription(**data["sessionDescription"])

    async def subscribe(self, session: str, tracks: list[tuple[str, str]]) -> dict:
        """tracks: (publisher session id, track name). Returns the SFU's offer when renegotiation is needed."""
        return await self._call("POST", f"/sessions/{session}/tracks/new", {
            "tracks": [{"location": "remote", "sessionId": s, "trackName": n} for s, n in tracks],
        })

    async def renegotiate(self, session: str, answer: RTCSessionDescription) -> None:
        await self._call("PUT", f"/sessions/{session}/renegotiate", {
            "sessionDescription": {"type": answer.type, "sdp": answer.sdp},
        })

    async def close(self) -> None:
        await self._http.aclose()


class Mouth(MediaStreamTrack):
    """Outbound audio. Plays queued PCM in real time and sends silence in between, like an open mic."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue: asyncio.Queue[np.ndarray | asyncio.Event] = asyncio.Queue()
        self._pts = 0
        self._start: float | None = None

    async def play(self, pcm: np.ndarray) -> None:
        """Queue mono int16 48kHz audio and wait until the last frame has gone out."""
        pad = (-len(pcm)) % FRAME
        pcm = np.concatenate([pcm, np.zeros(pad, np.int16)])
        for i in range(0, len(pcm), FRAME):
            self._queue.put_nowait(pcm[i:i + FRAME])
        done = asyncio.Event()
        self._queue.put_nowait(done)
        await done.wait()

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        wait = self._start + self._pts / RATE - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)

        samples = np.zeros(FRAME, np.int16)
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, asyncio.Event):
                item.set()
                continue
            samples = item
            break

        frame = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        self._pts += FRAME
        return frame


def to_mono(frame: AudioFrame) -> np.ndarray:
    pcm = frame.to_ndarray()
    if frame.format.is_planar:
        pcm = pcm.mean(axis=0)
    else:
        pcm = pcm.reshape(-1, len(frame.layout.channels)).mean(axis=1)
    return pcm.astype(np.int16)


class Peer:
    """One party on the call: publishes `mouth`, and hands every received audio frame to `on_audio`."""

    def __init__(self, api: RealtimeAPI, track_name: str):
        self.api = api
        self.track_name = track_name
        self.mouth = Mouth()
        self.pc = RTCPeerConnection()
        self.session = ""
        self._readers: list[asyncio.Task] = []

    async def join(self) -> None:
        self.session = await self.api.new_session()
        transceiver = self.pc.addTransceiver(self.mouth, direction="sendonly")
        await self.pc.setLocalDescription(await self.pc.createOffer())
        connected = asyncio.Event()

        @self.pc.on("connectionstatechange")
        def _():
            if self.pc.connectionState == "connected":
                connected.set()

        answer = await self.api.publish(self.session, self.pc.localDescription, transceiver.mid, self.track_name)
        await self.pc.setRemoteDescription(answer)
        # The SFU can only forward a track once its publisher is actually connected.
        await asyncio.wait_for(connected.wait(), 10)

    async def listen(self, publisher: "Peer", on_audio) -> None:
        got_track: asyncio.Future = asyncio.get_running_loop().create_future()

        @self.pc.on("track")
        def _(track):
            if not got_track.done():
                got_track.set_result(track)

        data = await self.api.subscribe(self.session, [(publisher.session, publisher.track_name)])
        if data.get("requiresImmediateRenegotiation"):
            await self.pc.setRemoteDescription(RTCSessionDescription(**data["sessionDescription"]))
            await self.pc.setLocalDescription(await self.pc.createAnswer())
            await self.api.renegotiate(self.session, self.pc.localDescription)
        track = await asyncio.wait_for(got_track, 10)
        self._readers.append(asyncio.create_task(self._read(track, on_audio)))

    async def _read(self, track, on_audio) -> None:
        try:
            while True:
                on_audio(to_mono(await track.recv()))
        except MediaStreamError:
            pass

    async def leave(self) -> None:
        for t in self._readers:
            t.cancel()
        await self.pc.close()
