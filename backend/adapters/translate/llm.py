"""Translation through any OpenAI-compatible ``/chat/completions`` endpoint.

Works with OpenAI, OpenRouter, Groq, vLLM, LM Studio, and the in-house
``ai-pass`` gateway (``http://192.168.9.218:8181/v1``) by changing
``TRANSLATE_BASE_URL``. Subtitle prompts are short and deterministic: no
chat history, no explanation, output only the translation.
"""

from __future__ import annotations

import logging

import httpx

from backend.adapters.translate.base import Translator
from backend.config import LANG_NAMES

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a translation engine for live meeting subtitles between Thai and "
    "Vietnamese speakers. Translate the user's message from {src} to {tgt}. "
    "Keep the original tone and do not summarise. Reply with the translation "
    "only: no quotes, no romanisation, no notes, no language tag."
)

MAX_CACHE_ENTRIES = 2_000


class LLMTranslator(Translator):
    name = "llm"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = 12.0,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        self._cache: dict[tuple[str, str, str], str] = {}

    async def translate(self, text: str, src: str, tgt: str) -> str:
        clean = text.strip()
        if not clean:
            return text
        if src == tgt:
            return clean

        cache_key = (clean, src, tgt)
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit

        body = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 512,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        src=LANG_NAMES.get(src, src),
                        tgt=LANG_NAMES.get(tgt, tgt),
                    ),
                },
                {"role": "user", "content": clean},
            ],
        }

        try:
            response = await self._client.post("/chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
            raw = payload["choices"][0]["message"]["content"]
            if not isinstance(raw, str):
                # Some gateways return content as a list of parts.
                raw = "".join(
                    part.get("text", "")
                    for part in raw
                    if isinstance(part, dict)
                ) if isinstance(raw, list) else str(raw)
            result = _clean_output(raw) or clean
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError):
            log.exception("translation failed for %s->%s", src, tgt)
            return clean

        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[cache_key] = result
        return result

    async def aclose(self) -> None:
        await self._client.aclose()


def _clean_output(raw: str) -> str:
    text = (raw or "").strip()
    for prefix in ('"', "'", "Translation:", "translation:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text
