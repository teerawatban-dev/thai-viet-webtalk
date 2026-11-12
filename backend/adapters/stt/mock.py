"""Offline STT provider.

Emits deterministic placeholder text so the whole pipeline (audio -> stt ->
translate -> broadcast) can be exercised with no API key and no network. It is
the default provider and the one used by the test-suite.
"""

from __future__ import annotations

from backend.adapters.stt.base import (
    BYTES_PER_SECOND,
    FinalCB,
    PartialCB,
    STTProvider,
    STTSession,
)

PARTIAL_EVERY_BYTES = BYTES_PER_SECOND  # ~1 s of 16 kHz mono s16le
MAX_UTTERANCE_BYTES = 6 * BYTES_PER_SECOND


class MockSession(STTSession):
    def __init__(self, lang: str, on_partial: PartialCB, on_final: FinalCB) -> None:
        self._lang = lang
        self._on_partial = on_partial
        self._on_final = on_final
        self._total_bytes = 0
        self._utterance_bytes = 0
        self._bytes_at_last_partial = 0
        self._closed = False

    async def push(self, chunk: bytes) -> None:
        if self._closed or not chunk:
            return

        self._total_bytes += len(chunk)
        self._utterance_bytes += len(chunk)

        if self._utterance_bytes - self._bytes_at_last_partial >= PARTIAL_EVERY_BYTES:
            self._bytes_at_last_partial = self._utterance_bytes
            await self._on_partial(self._text("partial"))

        if self._utterance_bytes >= MAX_UTTERANCE_BYTES:
            await self._finish()

    async def end_utterance(self) -> None:
        await self._finish()

    async def close(self) -> None:
        if self._closed:
            return
        await self._finish()
        self._closed = True

    async def _finish(self) -> None:
        if self._utterance_bytes <= 0:
            return
        seconds = self._utterance_bytes / BYTES_PER_SECOND
        self._utterance_bytes = 0
        self._bytes_at_last_partial = self._total_bytes
        await self._on_final(self._text(f"final · {seconds:.1f}s"))

    def _text(self, kind: str) -> str:
        return f"[mock {self._lang} {kind}]"


class MockProvider(STTProvider):
    name = "mock"

    async def open(
        self,
        lang: str,
        on_partial: PartialCB,
        on_final: FinalCB,
    ) -> STTSession:
        return MockSession(lang, on_partial, on_final)
