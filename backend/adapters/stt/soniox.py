"""Soniox realtime speech-to-text adapter (upstream WebSocket).

Protocol notes (verify against https://soniox.com/docs before production use):

* The upstream socket is opened first, then a single JSON config frame is sent.
* Client audio is raw little-endian PCM16. The app sends mono 16 kHz, so the
  config declares ``pcm_s16le`` / 16000 / 1 channel.
* Each upstream frame is JSON: ``{"tokens": [{"text": "...", "is_final": true}]}``.
  Token text ``"<fin>"`` marks the end of an utterance.

The adapter is intentionally thin: connection failures propagate to the
pipeline, which reports ``stt_unavailable`` to the client and simply ignores
audio, so a provider outage never takes a room down.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed

from backend.adapters.stt.base import (
    SAMPLE_RATE,
    FinalCB,
    PartialCB,
    STTProvider,
    STTSession,
)

log = logging.getLogger(__name__)

FIN_TOKEN = "<fin>"


class SonioxSession(STTSession):
    def __init__(
        self,
        ws,
        lang: str,
        on_partial: PartialCB,
        on_final: FinalCB,
    ) -> None:
        self._ws = ws
        self._lang = lang
        self._on_partial = on_partial
        self._on_final = on_final
        self._final_parts: list[str] = []
        self._partial_parts: list[str] = []
        self._last_partial = ""
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                self._handle_raw(raw)
        except ConnectionClosed:
            log.info("soniox socket closed")
        except Exception:
            log.exception("soniox read loop failed")
        finally:
            await self._flush_final()

    def _handle_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("soniox sent non-JSON frame")
            return

        error = payload.get("error_message") or payload.get("error_code")
        if error:
            log.warning("soniox error: %s", error)
            return

        ended = False
        for token in payload.get("tokens", []):
            text = token.get("text") or ""
            if text == FIN_TOKEN:
                ended = True
                continue
            if token.get("is_final"):
                self._final_parts.append(text)
            else:
                self._partial_parts.append(text)

        if ended:
            self._schedule_final()
            return

        current = "".join(self._partial_parts).strip()
        if current and current != self._last_partial:
            self._last_partial = current
            self._schedule_partial(current)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_partial(self, text: str) -> None:
        self._spawn(self._safe(self._on_partial, text))

    def _schedule_final(self) -> None:
        text = "".join(self._final_parts).strip()
        self._final_parts.clear()
        self._partial_parts.clear()
        self._last_partial = ""
        if text:
            self._spawn(self._safe(self._on_final, text))

    async def _flush_final(self) -> None:
        text = "".join(self._final_parts).strip()
        self._final_parts.clear()
        if text:
            await self._safe(self._on_final, text)

    @staticmethod
    async def _safe(cb, text: str) -> None:
        try:
            await cb(text)
        except Exception:
            log.exception("stt callback failed")

    async def push(self, chunk: bytes) -> None:
        if self._closed or not chunk:
            return
        try:
            await self._ws.send(chunk)
        except ConnectionClosed:
            self._closed = True
            log.warning("soniox socket closed while sending audio")

    async def end_utterance(self) -> None:
        # Soniox endpointing marks finals itself; an empty frame nudges it.
        if self._closed:
            return
        try:
            await self._ws.send(b"")
        except ConnectionClosed:
            self._closed = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            await self._ws.close()
        except Exception:
            pass

        # Give the reader a chance to emit its buffered final transcript.
        try:
            await asyncio.wait_for(self._reader, timeout=2.0)
        except Exception:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

        if self._tasks:
            others = [t for t in self._tasks if t is not asyncio.current_task()]
            if others:
                await asyncio.gather(*others, return_exceptions=True)


class SonioxProvider(STTProvider):
    name = "soniox"

    def __init__(self, api_key: str, model: str, ws_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._ws_url = ws_url

    async def open(
        self,
        lang: str,
        on_partial: PartialCB,
        on_final: FinalCB,
    ) -> STTSession:
        ws = await websockets.connect(
            self._ws_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
        )
        await ws.send(
            json.dumps(
                {
                    "api_key": self._api_key,
                    "model": self._model,
                    "audio_format": "pcm_s16le",
                    "sample_rate": SAMPLE_RATE,
                    "num_channels": 1,
                    "language_hints": [lang],
                    "enable_partial_results": True,
                    "enable_endpoint_detection": True,
                }
            )
        )
        return SonioxSession(ws, lang, on_partial, on_final)
