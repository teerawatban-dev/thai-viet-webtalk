"""Speech-to-text adapter interface.

A provider is a factory: ``provider.open(lang, on_partial, on_final)`` returns a
live session. The session is fed raw mono PCM16LE audio chunks and reports
transcripts through the two async callbacks. Providers own their own
endpointing / partial logic; the pipeline never inspects audio.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable

PartialCB = Callable[[str], Awaitable[None]]
FinalCB = Callable[[str], Awaitable[None]]

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono, 16-bit


class STTSession(abc.ABC):
    """One recognition stream for one speaker."""

    @abc.abstractmethod
    async def push(self, chunk: bytes) -> None:
        """Feed audio. Never blocks the caller for long."""

    @abc.abstractmethod
    async def end_utterance(self) -> None:
        """Signal that the speaker stopped; flush any buffered audio."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources. Must be safe to call twice."""


class STTProvider(abc.ABC):
    """Factory for :class:`STTSession` objects."""

    name: str = "base"

    @abc.abstractmethod
    async def open(
        self,
        lang: str,
        on_partial: PartialCB,
        on_final: FinalCB,
    ) -> STTSession:
        """Open a session for ``lang``. Callbacks may fire from any task."""

    async def aclose(self) -> None:
        """Release provider-wide resources (shared HTTP/WS clients)."""
        return None
