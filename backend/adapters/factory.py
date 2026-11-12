"""Provider selection from settings, with graceful degradation.

Unknown providers or missing keys never crash startup: we log loudly and fall
back to the offline implementation so the room still works.
"""

from __future__ import annotations

import logging

from backend.adapters.stt.base import STTProvider
from backend.adapters.stt.browser import BrowserProvider
from backend.adapters.stt.mock import MockProvider
from backend.adapters.stt.soniox import SonioxProvider
from backend.adapters.translate.base import Translator
from backend.adapters.translate.lingva import LingvaTranslator
from backend.adapters.translate.llm import LLMTranslator
from backend.adapters.translate.passthrough import PassthroughTranslator
from backend.config import Settings

log = logging.getLogger(__name__)


def build_stt_provider(settings: Settings) -> STTProvider:
    requested = settings.stt_provider.strip().lower()

    if requested == "browser":
        log.info("stt provider: browser (Web Speech API, client-side)")
        return BrowserProvider()

    if requested == "soniox":
        if settings.key_for("soniox"):
            log.info("stt provider: soniox (%s)", settings.stt_model)
            return SonioxProvider(
                api_key=settings.key_for("soniox"),
                model=settings.stt_model,
                ws_url=settings.stt_ws_url,
            )
        log.warning("STT_PROVIDER=soniox but STT_API_KEY is empty -> using mock")

    elif requested != "mock":
        log.warning("unknown STT_PROVIDER=%r -> using mock", requested)

    log.info("stt provider: mock (offline transcripts)")
    return MockProvider()


def build_translator(settings: Settings) -> Translator:
    requested = settings.translate_provider.strip().lower()

    if requested == "lingva":
        log.info("translator: lingva (key-free, public instances + mymemory fallback)")
        return LingvaTranslator()

    if requested == "llm":
        if settings.key_for("llm"):
            log.info(
                "translator: llm model=%s base_url=%s",
                settings.translate_model,
                settings.translate_base_url,
            )
            return LLMTranslator(
                api_key=settings.key_for("llm"),
                base_url=settings.translate_base_url,
                model=settings.translate_model,
                timeout_s=settings.translate_timeout_s,
            )
        log.warning("TRANSLATE_PROVIDER=llm but TRANSLATE_API_KEY is empty -> passthrough")

    elif requested != "passthrough":
        log.warning("unknown TRANSLATE_PROVIDER=%r -> using passthrough", requested)

    log.info("translator: passthrough (offline)")
    return PassthroughTranslator()
