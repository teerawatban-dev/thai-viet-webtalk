"""Unit tests for ``backend/pipeline.py``.

The pipeline is deliberately framework-free, so these tests drive it with fake
STT sessions and a fake translator. Each scenario is a plain coroutine run via
``asyncio.run`` (``pytest-asyncio`` is not installed and must not be added).
"""

from __future__ import annotations

import asyncio

from backend.adapters.stt.base import STTProvider, STTSession
from backend.adapters.translate.base import Translator
from backend.pipeline import TranscriptPipeline


class FakeSession(STTSession):
    """Session whose callbacks can be triggered by the test."""

    def __init__(self, lang: str, on_partial, on_final) -> None:
        self.lang = lang
        self.on_partial = on_partial
        self.on_final = on_final
        self.pushes: list[bytes] = []
        self.close_calls = 0
        self.end_calls = 0
        self.closed = False
        self.final_on_close: str | None = None

    async def push(self, chunk: bytes) -> None:
        self.pushes.append(chunk)

    async def end_utterance(self) -> None:
        self.end_calls += 1

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        # Mimic providers that flush buffered audio when the stream closes.
        pending, self.final_on_close = self.final_on_close, None
        if pending:
            await self.on_final(pending)

    # test helpers ------------------------------------------------------
    async def emit_partial(self, text: str) -> None:
        await self.on_partial(text)

    async def emit_final(self, text: str) -> None:
        await self.on_final(text)


class FakeProvider(STTProvider):
    name = "fake"

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.sessions: list[FakeSession] = []
        self.fail = False

    async def open(self, lang, on_partial, on_final):  # noqa: D401 - protocol method
        if self.fail:
            raise RuntimeError("provider offline")
        session = FakeSession(lang, on_partial, on_final)
        self.opened.append(lang)
        self.sessions.append(session)
        return session


class FakeTranslator(Translator):
    name = "fake"

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.fail_for = set(fail_for or ())
        self.calls: list[tuple[str, str, str]] = []

    async def translate(self, text: str, src: str, tgt: str) -> str:
        self.calls.append((text, src, tgt))
        if tgt in self.fail_for:
            raise RuntimeError("translator exploded")
        return f"{tgt}:{text}"


class SlowFirstTranslator(FakeTranslator):
    """Adds latency to the first utterance to expose ordering bugs."""

    async def translate(self, text: str, src: str, tgt: str) -> str:
        self.calls.append((text, src, tgt))
        if text == "first":
            await asyncio.sleep(0.01)
        return f"{tgt}:{text}"


def make_pipeline(
    *,
    lang: str = "th",
    targets=("th", "vi", "en"),
    translator: Translator | None = None,
    provider: STTProvider | None = None,
):
    provider = provider if provider is not None else FakeProvider()
    translator = translator if translator is not None else FakeTranslator()
    partials: list[str] = []
    finals: list[dict] = []

    async def emit_partial(text: str) -> None:
        partials.append(text)

    async def emit_final(text, translations, latency_ms, utterance_id) -> None:
        finals.append(
            {
                "text": text,
                "translations": translations,
                "latency_ms": latency_ms,
                "id": utterance_id,
            }
        )

    pipeline = TranscriptPipeline(
        client_id="c1",
        lang=lang,
        provider=provider,
        translator=translator,
        targets=list(targets),
        emit_partial=emit_partial,
        emit_final=emit_final,
    )
    return pipeline, provider, translator, partials, finals


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_returns_true_and_opens_a_session():
    async def scenario():
        pipeline, provider, *_ = make_pipeline()
        opened = await pipeline.start()
        return opened, provider

    opened, provider = asyncio.run(scenario())

    assert opened is True
    assert provider.opened == ["th"]
    assert len(provider.sessions) == 1


def test_start_returns_false_when_the_provider_raises():
    async def scenario():
        provider = FakeProvider()
        provider.fail = True
        pipeline, provider, translator, _, finals = make_pipeline(provider=provider)

        opened = await pipeline.start()
        await pipeline.feed(b"\x00\x00")
        await pipeline.end_utterance()
        return opened, provider, translator, finals

    opened, provider, translator, finals = asyncio.run(scenario())

    assert opened is False
    assert provider.sessions == []
    assert translator.calls == []
    assert finals == []


def test_close_lets_the_provider_flush_a_buffered_final():
    async def scenario():
        pipeline, provider, _, _, finals = make_pipeline()
        await pipeline.start()
        session = provider.sessions[0]
        session.final_on_close = "buffered words"

        await pipeline.close()
        return session, finals

    session, finals = asyncio.run(scenario())

    assert session.close_calls == 1
    assert [final["text"] for final in finals] == ["buffered words"]
    assert finals[0]["translations"] == {
        "vi": "vi:buffered words",
        "en": "en:buffered words",
    }


def test_audio_and_finals_are_ignored_after_close():
    async def scenario():
        pipeline, provider, translator, _, finals = make_pipeline()
        await pipeline.start()
        session = provider.sessions[0]
        await pipeline.close()

        await pipeline.feed(b"\x00\x00")
        await pipeline.end_utterance()
        await session.emit_partial("late partial")
        await session.emit_final("late final")

        return session, translator, finals

    session, translator, finals = asyncio.run(scenario())

    assert session.pushes == []
    assert session.end_calls == 0
    assert translator.calls == []
    assert finals == []


# ---------------------------------------------------------------------------
# Translation fan-out
# ---------------------------------------------------------------------------


def test_final_is_translated_once_per_target_excluding_source():
    async def scenario():
        pipeline, provider, translator, _, finals = make_pipeline()
        await pipeline.start()
        await provider.sessions[0].emit_final("  hello world  ")
        return translator, finals

    translator, finals = asyncio.run(scenario())

    assert translator.calls == [
        ("hello world", "th", "vi"),
        ("hello world", "th", "en"),
    ]
    assert len(finals) == 1
    final = finals[0]
    assert final["text"] == "hello world"
    assert final["translations"] == {
        "vi": "vi:hello world",
        "en": "en:hello world",
    }
    assert final["id"] == "c1-1"
    assert final["latency_ms"] >= 0


def test_final_with_no_remaining_targets_has_empty_translations():
    async def scenario():
        pipeline, provider, translator, _, finals = make_pipeline(targets=["th"])
        await pipeline.start()
        await provider.sessions[0].emit_final("hello")
        return translator, finals

    translator, finals = asyncio.run(scenario())

    assert translator.calls == []
    assert len(finals) == 1
    assert finals[0]["text"] == "hello"
    assert finals[0]["translations"] == {}


def test_failing_translation_falls_back_to_the_original_text():
    async def scenario():
        translator = FakeTranslator(fail_for={"en"})
        pipeline, provider, translator, _, finals = make_pipeline(
            lang="vi", translator=translator
        )
        await pipeline.start()
        await provider.sessions[0].emit_final("xin chao")
        return finals

    finals = asyncio.run(scenario())

    # The failing target keeps the source text, the healthy one is translated.
    assert finals[0]["translations"] == {"th": "th:xin chao", "en": "xin chao"}


def test_utterance_ids_increment_per_final():
    async def scenario():
        pipeline, provider, _, _, finals = make_pipeline()
        await pipeline.start()
        session = provider.sessions[0]
        await session.emit_final("one")
        await session.emit_final("two")
        return finals

    finals = asyncio.run(scenario())

    assert [final["id"] for final in finals] == ["c1-1", "c1-2"]
    assert [final["text"] for final in finals] == ["one", "two"]


def test_concurrent_finals_keep_arrival_order():
    async def scenario():
        pipeline, provider, _, _, finals = make_pipeline(
            translator=SlowFirstTranslator()
        )
        await pipeline.start()
        session = provider.sessions[0]

        # The first utterance is slow; without the internal emit lock the
        # second (fast) final would overtake it.
        await asyncio.gather(
            session.emit_final("first"),
            session.emit_final("last"),
        )
        return finals

    finals = asyncio.run(scenario())

    assert [final["text"] for final in finals] == ["first", "last"]
    assert [final["id"] for final in finals] == ["c1-1", "c1-2"]


def test_latency_is_non_negative_after_audio():
    async def scenario():
        pipeline, provider, _, _, finals = make_pipeline()
        await pipeline.start()
        await pipeline.feed(b"\x00\x00" * 1_000)
        await provider.sessions[0].emit_final("done")
        return finals

    finals = asyncio.run(scenario())

    assert len(finals) == 1
    assert finals[0]["latency_ms"] >= 0


def test_blank_transcripts_are_ignored():
    async def scenario():
        pipeline, provider, translator, partials, finals = make_pipeline()
        await pipeline.start()
        session = provider.sessions[0]
        await session.emit_partial("   ")
        await session.emit_final("  \n ")
        return translator, partials, finals

    translator, partials, finals = asyncio.run(scenario())

    assert partials == []
    assert finals == []
    assert translator.calls == []


def test_partial_is_trimmed_before_emit():
    async def scenario():
        pipeline, provider, _, partials, _ = make_pipeline()
        await pipeline.start()
        await provider.sessions[0].emit_partial("  hi  ")
        return partials

    assert asyncio.run(scenario()) == ["hi"]


# ---------------------------------------------------------------------------
# Language switching
# ---------------------------------------------------------------------------


def test_set_lang_recycles_the_provider_session():
    async def scenario():
        pipeline, provider, _, _, _ = make_pipeline()
        await pipeline.start()
        first = provider.sessions[0]

        switched = await pipeline.set_lang("vi")
        second = provider.sessions[-1]
        await pipeline.feed(b"\x00\x00")
        return switched, pipeline, provider, first, second

    switched, pipeline, provider, first, second = asyncio.run(scenario())

    assert switched is True
    assert provider.opened == ["th", "vi"]
    assert first is not second
    assert first.close_calls == 1
    assert first.pushes == []
    assert second.pushes == [b"\x00\x00"]
    assert pipeline.lang == "vi"


def test_set_lang_to_the_current_language_keeps_the_session():
    async def scenario():
        pipeline, provider, _, _, _ = make_pipeline()
        await pipeline.start()
        switched = await pipeline.set_lang("th")
        return switched, provider

    switched, provider = asyncio.run(scenario())

    assert switched is True
    assert provider.opened == ["th"]
    assert provider.sessions[0].close_calls == 0


def test_set_lang_returns_false_when_the_provider_cannot_reopen():
    async def scenario():
        pipeline, provider, _, _, _ = make_pipeline()
        await pipeline.start()
        first = provider.sessions[0]

        provider.fail = True
        switched = await pipeline.set_lang("vi")
        await pipeline.feed(b"\x00\x00")
        return switched, pipeline, first

    switched, pipeline, first = asyncio.run(scenario())

    assert switched is False
    assert first.close_calls == 1
    assert pipeline.lang == "vi"


def test_set_lang_after_close_restarts_with_the_same_language():
    async def scenario():
        pipeline, provider, _, _, _ = make_pipeline()
        await pipeline.start()
        await pipeline.close()

        restarted = await pipeline.set_lang("th")
        await pipeline.feed(b"\x00\x00")
        return restarted, provider

    restarted, provider = asyncio.run(scenario())

    assert restarted is True
    assert provider.opened == ["th", "th"]
    assert provider.sessions[-1].pushes == [b"\x00\x00"]
