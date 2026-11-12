"""Client-side STT mode.

When ``STT_PROVIDER=browser`` the browser runs the recogniser (Web Speech API)
and posts text over the WebSocket, so the server never touches audio. This
provider exists purely to keep the pipeline shape identical: its sessions
accept audio and do nothing.
"""

from __future__ import annotations

from backend.adapters.stt.base import (
    FinalCB,
    PartialCB,
    STTProvider,
    STTSession,
)


class NullSession(STTSession):
    async def push(self, chunk: bytes) -> None:
        return None

    async def end_utterance(self) -> None:
        return None

    async def close(self) -> None:
        return None


class BrowserProvider(STTProvider):
    name = "browser"

    async def open(
        self,
        lang: str,
        on_partial: PartialCB,
        on_final: FinalCB,
    ) -> STTSession:
        return NullSession()
