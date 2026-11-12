"""Audio -> transcript -> translation orchestration for a single speaker.

Framework-agnostic on purpose: it takes plain async callbacks, so the room /
WebSocket layer can be swapped without touching this file (and it is trivially
unit-testable with fakes).

Ordering guarantee: finals are translated and emitted one at a time per
participant (``_emit_lock``), so utterance N+1 can never overtake N even when
utterance N is waiting on a slow translation call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from backend.adapters.stt.base import STTProvider, STTSession
from backend.adapters.translate.base import Translator

log = logging.getLogger(__name__)

PartialEmitter = Callable[[str], Awaitable[None]]
FinalEmitter = Callable[[str, dict[str, str], int, str], Awaitable[None]]


class TranscriptPipeline:
    """Owns one STT session plus the translation fan-out for one participant."""

    def __init__(
        self,
        *,
        client_id: str,
        lang: str,
        provider: STTProvider,
        translator: Translator,
        targets: list[str],
        emit_partial: PartialEmitter,
        emit_final: FinalEmitter,
    ) -> None:
        self.client_id = client_id
        self.lang = lang
        self._provider = provider
        self._translator = translator
        self._targets = targets
        self._emit_partial = emit_partial
        self._emit_final = emit_final

        self._session: STTSession | None = None
        self._utterance_started_at: float | None = None
        self._utterance_seq = 0
        self._emit_lock = asyncio.Lock()
        self._emit_tasks: set[asyncio.Task] = set()
        self._emit_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._closed = False

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> bool:
        """Open the provider session. Returns False when it cannot be opened."""
        if self._session is not None:
            return True
        try:
            self._session = await self._provider.open(
                self.lang,
                on_partial=self._on_partial,
                on_final=self._on_final,
            )
        except Exception:
            log.exception("could not open stt session for %s", self.client_id)
            self._session = None
            return False
        self._closed = False
        return True

    async def close(self) -> None:
        """Close the session, letting any buffered final transcript through."""
        session, self._session = self._session, None
        if session is not None and asyncio.current_task() in self._emit_tasks:
            # Re-entrant close from one of our own emit callbacks (a peer was
            # purged while we were broadcasting). Waiting for the provider
            # flush here would deadlock on the emit lock we already hold, so
            # drop it. The buffered final is intentionally discarded.
            self._closed = True
            task = asyncio.create_task(self._quiet_close(session))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return

        if session is not None:
            try:
                # Providers may still emit a final from their buffered audio
                # here; that is intentional, so ``_closed`` stays False.
                await session.close()
            except Exception:
                log.exception("failed to close stt session for %s", self.client_id)
        self._closed = True

    @staticmethod
    async def _quiet_close(session: STTSession) -> None:
        try:
            await session.close()
        except Exception:
            log.exception("deferred stt close failed")

    async def set_lang(self, lang: str) -> bool:
        """Switch recognition language by recycling the provider session."""
        if lang == self.lang and self._session is not None:
            return True
        await self._shutdown_session()
        self.lang = lang
        return await self.start()

    async def _shutdown_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            # Providers may still emit a final from their buffered audio here;
            # that is intentional, so ``_closed`` stays False until afterwards.
            await session.close()
        except Exception:
            log.exception("failed to close stt session for %s", self.client_id)

    # -- audio ------------------------------------------------------------
    async def feed(self, chunk: bytes) -> None:
        if self._closed or not chunk:
            return
        if self._utterance_started_at is None:
            self._utterance_started_at = time.monotonic()
        session = self._session
        if session is None:
            return
        try:
            await session.push(chunk)
        except Exception:
            log.exception("stt push failed for %s", self.client_id)

    async def end_utterance(self) -> None:
        session = self._session
        if self._closed or session is None:
            return
        try:
            await session.end_utterance()
        except Exception:
            log.exception("stt end_utterance failed for %s", self.client_id)

    # -- callbacks --------------------------------------------------------
    async def _on_partial(self, text: str) -> None:
        clean = text.strip()
        if not clean or self._closed:
            return
        await self._safe(self._emit_partial, clean)

    async def _on_final(self, text: str) -> None:
        clean = text.strip()
        if not clean or self._closed:
            return

        self._emit_task = asyncio.current_task()
        self._emit_tasks.add(self._emit_task)
        try:
            async with self._emit_lock:
                if self._closed:
                    return
                latency_ms = 0
                if self._utterance_started_at is not None:
                    latency_ms = int(
                        (time.monotonic() - self._utterance_started_at) * 1000
                    )
                self._utterance_started_at = None
                self._utterance_seq += 1
                utterance_id = f"{self.client_id}-{self._utterance_seq}"

                targets = [code for code in self._targets if code != self.lang]
                if not targets:
                    await self._safe(
                        self._emit_final, clean, {}, latency_ms, utterance_id
                    )
                    return

                results = await asyncio.gather(
                    *(
                        self._translator.translate(clean, self.lang, code)
                        for code in targets
                    ),
                    return_exceptions=True,
                )
                translations: dict[str, str] = {}
                for code, result in zip(targets, results, strict=False):
                    if isinstance(result, BaseException):
                        log.warning(
                            "translate %s->%s failed: %s", self.lang, code, result
                        )
                        translations[code] = clean
                    else:
                        translations[code] = result

                await self._safe(
                    self._emit_final, clean, translations, latency_ms, utterance_id
                )
        finally:
            self._emit_tasks.discard(self._emit_task)
            self._emit_task = None

    @staticmethod
    async def _safe(cb, *args) -> None:
        try:
            await cb(*args)
        except Exception:
            log.exception("emit callback failed")
