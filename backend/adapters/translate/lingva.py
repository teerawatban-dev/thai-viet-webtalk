"""Key-free translation with a circuit breaker.

Primary path: **Lingva** (https://github.com/thedaviddelta/lingva-translate), a
public read-only proxy over Google Translate. No API key, no account.

Reality check from production logs (2026-11-12): public Lingva instances are
frequently dead — two returned 403 and one 500 from Render's egress IP, on
*every* request. Blindly retrying them cost ~2.5 s per phrase and burned the
fallback's quota, so hosts now carry health state:

* two consecutive failures open the circuit for ``cooldown_s`` (default 10 min)
* an open host is skipped entirely — no request is even attempted
* one success closes it again
* when every host is open, we go straight to MyMemory

Fallback: **MyMemory**, also key-free (anonymous quota ~5k chars/day).

Neither is SLA-backed: if everything fails the source text is returned and the
failure is logged at ERROR, so `/healthz` + logs show a real problem instead of
silent degradation.
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import httpx

from backend.adapters.translate.base import Translator

log = logging.getLogger(__name__)

MAX_CACHE_ENTRIES = 2_000
MAX_TEXT_CHARS = 600  # public endpoints choke on long inputs
FAILURES_BEFORE_OPEN = 2
DEFAULT_COOLDOWN_S = 600.0


class LingvaTranslator(Translator):
    name = "lingva"

    def __init__(
        self,
        hosts: list[str] | None = None,
        timeout_s: float = 6.0,
        mymemory: bool = True,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        failures_before_open: int = FAILURES_BEFORE_OPEN,
    ) -> None:
        self._hosts = hosts or [
            "https://lingva.ml",
            "https://lingva.garudalinux.org",
            "https://translate.plausibility.cloud",
        ]
        self._mymemory = mymemory
        self._cooldown_s = cooldown_s
        self._failures_before_open = max(1, failures_before_open)
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "thai-viet-webtalk/0.2"},
        )
        self._cache: dict[tuple[str, str, str], str] = {}
        self._failures: dict[str, int] = {host: 0 for host in self._hosts}
        self._open_until: dict[str, float] = {}
        self._stats = {
            "ok": 0,
            "fallback": 0,
            "failed": 0,
            "cached": 0,
            "circuit_open": 0,
            "hosts_down": 0,
        }

    # -- public -----------------------------------------------------------
    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        down = sum(1 for host in self._hosts if self._open_until.get(host, 0.0) > now)
        return {**self._stats, "hosts_down": down}

    def healthy_hosts(self) -> list[str]:
        now = time.monotonic()
        return [host for host in self._hosts if self._open_until.get(host, 0.0) <= now]

    async def translate(self, text: str, src: str, tgt: str) -> str:
        clean = text.strip()
        if not clean or src == tgt:
            return text

        cache_key = (clean, src, tgt)
        hit = self._cache.get(cache_key)
        if hit is not None:
            self._stats["cached"] += 1
            return hit

        trimmed = clean[:MAX_TEXT_CHARS]
        result = await self._via_lingva(trimmed, src, tgt)
        source = "lingva"

        if not result and self._mymemory:
            result = await self._via_mymemory(trimmed, src, tgt)
            source = "mymemory"

        if not result:
            self._stats["failed"] += 1
            log.error(
                "translation unavailable for %s->%s (%d/%d hosts open); sending source text",
                src,
                tgt,
                len(self._open_hosts()),
                len(self._hosts),
            )
            return clean

        self._stats["ok"] += 1
        if source != "lingva":
            self._stats["fallback"] += 1

        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[cache_key] = result
        return result

    # -- circuit breaker --------------------------------------------------
    def _open_hosts(self) -> list[str]:
        now = time.monotonic()
        return [host for host in self._hosts if self._open_until.get(host, 0.0) > now]

    def _record_failure(self, host: str, reason: str) -> None:
        self._failures[host] = self._failures.get(host, 0) + 1
        if self._failures[host] >= self._failures_before_open:
            self._open_until[host] = time.monotonic() + self._cooldown_s
            log.warning(
                "circuit open for %s after %d failures (%s); skipping for %.0fs",
                host,
                self._failures[host],
                reason,
                self._cooldown_s,
            )

    def _record_success(self, host: str) -> None:
        if self._failures.get(host) or self._open_until.get(host):
            log.info("circuit closed for %s", host)
        self._failures[host] = 0
        self._open_until.pop(host, None)

    # -- backends ---------------------------------------------------------
    async def _via_lingva(self, text: str, src: str, tgt: str) -> str:
        encoded = urllib.parse.quote(text, safe="")
        hosts = self.healthy_hosts()
        if not hosts:
            self._stats["circuit_open"] += 1
            log.debug("all lingva hosts are open; going straight to mymemory")
            return ""

        for host in hosts:
            try:
                response = await self._client.get(f"{host}/api/v1/{src}/{tgt}/{encoded}")
            except Exception as exc:  # noqa: BLE001
                self._record_failure(host, type(exc).__name__)
                continue

            if response.status_code != 200:
                self._record_failure(host, f"HTTP {response.status_code}")
                continue

            try:
                out = str(response.json().get("translation") or "").strip()
            except Exception:  # noqa: BLE001
                self._record_failure(host, "bad json")
                continue

            if out:
                self._record_success(host)
                return out
            self._record_failure(host, "empty translation")

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
