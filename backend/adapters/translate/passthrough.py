"""Offline translator: tags the text with its target language.

Good enough to validate routing (does the Vietnamese client see a ``th``
translation?) without spending money or needing a key.
"""

from __future__ import annotations

from backend.adapters.translate.base import Translator


class PassthroughTranslator(Translator):
    name = "passthrough"

    async def translate(self, text: str, src: str, tgt: str) -> str:
        if not text.strip() or src == tgt:
            return text
        return f"[{tgt}] {text}"
