"""Regression: emitting to a dead peer must never deadlock.

Two invariants are locked in here:

1. a broadcast failure is cleaned up via ``schedule_purge`` (background task),
   never awaited from inside the emit callback;
2. ``pipeline.close()`` called *from* an emit callback defers the provider
   flush instead of waiting on the emit lock it already holds.
"""

from __future__ import annotations

import asyncio

from backend.adapters.stt.base import STTProvider, STTSession
from backend.adapters.translate.passthrough import PassthroughTranslator
from backend.pipeline import TranscriptPipeline
from backend.rooms import Participant, Room


class DeadSocket:
    async def send_text(self, data: str) -> None:
        raise RuntimeError("socket is gone")

    async def close(self) -> None:
        return None


class LiveSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        return None


class FlushingSession(STTSession):
    """Emits a buffered final from ``close()`` — the dangerous part."""

    def __init__(self, on_final) -> None:
        self._on_final = on_final
        self._had_audio = False
        self.closed = False

    async def push(self, chunk: bytes) -> None:
        self._had_audio = True

    async def end_utterance(self) -> None:
        return None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._had_audio:
            await self._on_final("buffered final")


class FlushingProvider(STTProvider):
    name = "flushing"

    def __init__(self) -> None:
        self.last_on_final = None

    async def open(self, lang, on_partial, on_final):
        self.last_on_final = on_final
        return FlushingSession(on_final)


def test_failed_peer_during_broadcast_does_not_deadlock() -> None:
    async def scenario() -> None:
        room = Room("room", 4)
        speaker = Participant(client_id="speaker", websocket=DeadSocket(), lang="th")
        peer = Participant(client_id="peer", websocket=LiveSocket(), lang="vi")
        room.add(speaker)
        room.add(peer)

        async def emit_final(text, translations, latency_ms, utterance_id):
            dead = await room.broadcast(
                {"type": "final", "from": speaker.client_id, "text": text}
            )
            room.schedule_purge(dead)

        pipeline = TranscriptPipeline(
            client_id=speaker.client_id,
            lang="th",
            provider=FlushingProvider(),
            translator=PassthroughTranslator(),
            targets=["th", "vi"],
            emit_partial=lambda text: asyncio.sleep(0),
            emit_final=emit_final,
        )
        speaker.pipeline = pipeline
        assert await pipeline.start()

        await pipeline.feed(b"\x00\x00" * 40_000)
        await pipeline.end_utterance()
        await pipeline._session._on_final("hello")  # type: ignore[union-attr]

        await asyncio.sleep(0.1)
        assert room.get("speaker") is None, "dead speaker should be purged"
        assert room.get("peer") is not None
        assert peer.websocket.sent, "surviving peer should receive the final"

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_close_from_inside_emit_callback_does_not_deadlock() -> None:
    async def scenario() -> None:
        provider = FlushingProvider()
        state = {"closed": 0}

        async def emit_final(text, translations, latency_ms, utterance_id):
            state["closed"] += 1
            await pipeline.close()

        pipeline = TranscriptPipeline(
            client_id="c1",
            lang="th",
            provider=provider,
            translator=PassthroughTranslator(),
            targets=["th", "vi"],
            emit_partial=lambda text: asyncio.sleep(0),
            emit_final=emit_final,
        )
        assert await pipeline.start()
        await pipeline.feed(b"\x00\x00" * 1_000)

        # Re-entrant close: the emit callback closes its own pipeline.
        await provider.last_on_final("hello")
        assert state["closed"] == 1
        assert pipeline._closed is True

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
