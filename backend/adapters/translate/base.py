"""Translation adapter interface."""

from __future__ import annotations

import abc


class Translator(abc.ABC):
    """Stateless-ish text translation backend."""

    name: str = "base"

    @abc.abstractmethod
    async def translate(self, text: str, src: str, tgt: str) -> str:
        """Translate *text* from ``src`` to ``tgt``. Must not raise for
        recoverable failures — return the input text instead."""

    async def aclose(self) -> None:
        return None
