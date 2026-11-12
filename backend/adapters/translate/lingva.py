"""Key-free translation.

Primary path: **Lingva** (https://github.com/thedaviddelta/lingva-translate), a
public read-only proxy over Google Translate. No API key, no account. Public
instances come and go, so we try a list of hosts in order.

Fallback path: **MyMemory**, also key-free (anonymous quota ~5k chars/day).

Neither is an SLA-backed service: if everything fails, the original text is
returned so the room degrades to "same language" instead of breaking.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from backend.adapters.translate.base import Translator

log = logging.getLogger(__name__)

MAX_CACHE_ENTRIES = 2_000
MAX_TEXT_CHARS = 600  # public endpoints choke on long inputs


class LingvaTranslator(Translator):
    name = "lingva"

    def __init__(
        self,
        hosts: list[str] | None = None,
        timeout_s: float = 6.0,
        mymemory: bool = True,
    ) -> None:
        self._hosts = hosts or [
            "https://lingva.ml",
            "https://lingva.garudalinux.org",
            "https://translate.plausibility.cloud",
        ]
        self._mymemory = mymemory
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "thai-viet-webtalk/0.2"},
        )
        self._cache: dict[tuple[str, str, str], str] = {}

    async def translate(self, text: str, src: str, tgt: str) -> str:
        clean = text.strip()
        if not clean or src == tgt:
            return text

        cache_key = (clean, src, tgt)
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit

        trimmed = clean[:MAX_TEXT_CHARS]
        result = await self._via_lingva(trimmed, src, tgt)
        if not result and self._mymemory:
            result = await self._via_mymemory(trimmed, src, tgt)

        if not result:
            log.warning("no free translator answered for %s->%s", src, tgt)
            return clean

        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[cache_key] = result
        return result

    async def _via_lingva(self, text: str, src: str, tgt: str) -> str:
        encoded = urllib.parse.quote(text, safe="")
        for host in self._hosts:
            try:
                response = await self._client.get(f"{host}/api/v1/{src}/{tgt}/{encoded}")
                if response.status_code != 200:
                    continue
                payload = response.json()
                out = str(payload.get("translation") or "").strip()
                if out:
                    return out
            except Exception:
                log.debug("lingva host failed: %s", host, exc_info=True)
        return ""

    async def _via_mymemory(self, text: str, src: str, tgt: str) -> str:
        try:
            response = await self._client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": f"{src}|{tgt}"},
            )
            if response.status_code != 200:
                return ""
            payload = response.json()
            out = str(payload.get("responseData", {}).get("translatedText") or "").strip()
            if out and "MYMEMORY WARNING" not in out.upper():
                return out
        except Exception:
            log.debug("mymemory failed", exc_info=True)
        return ""

    async def aclose(self) -> None:
        await self._client.aclose()
