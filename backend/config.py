"""Application configuration.

All runtime settings come from environment variables (or a local ``.env``).
Nothing here may raise at import time for a missing *optional* value: the app
must boot with the offline ``mock`` / ``passthrough`` providers so a fresh
clone is runnable without any API key.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Languages the product cares about. Anything else is rejected at join time.
SUPPORTED_LANGS: frozenset[str] = frozenset({"th", "vi", "en"})

#: Long names used when prompting a translation model.
LANG_NAMES: dict[str, str] = {
    "th": "Thai",
    "vi": "Vietnamese",
    "en": "English",
}

_PLACEHOLDERS = {"", "changeme", "change-me", "your_key_here", "xxx"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- speech to text ---
    # browser: Web Speech API in the client (free, no key, no audio upload)
    stt_provider: str = "browser"
    stt_api_key: str = ""
    stt_model: str = "stt-rt-v4"
    stt_ws_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"

    # --- translation ---
    # lingva: key-free public Google proxy, MyMemory fallback
    translate_provider: str = "lingva"
    translate_api_key: str = ""
    translate_model: str = "gpt-4o-mini"
    translate_base_url: str = "https://api.openai.com/v1"
    translate_timeout_s: float = 12.0

    # --- app behaviour ---
    default_src_lang: str = "th"
    target_langs: str = "th,vi,en"
    max_room_clients: int = 6
    max_rooms: int = 100
    room_ttl_seconds: int = 300
    log_level: str = "INFO"
    tts_enabled: bool = False

    @property
    def targets(self) -> list[str]:
        """Parsed ``TARGET_LANGS`` as an ordered list of language codes."""
        seen: list[str] = []
        for raw in self.target_langs.split(","):
            code = raw.strip().lower()
            if code in SUPPORTED_LANGS and code not in seen:
                seen.append(code)
        return seen or [self.default_src_lang]

    def key_for(self, provider: str) -> str:
        """Return a usable API key for *provider*, or ``""`` when unset."""
        candidates = {
            "soniox": self.stt_api_key,
            "deepgram": self.stt_api_key,
            "llm": self.translate_api_key,
            "google": self.translate_api_key,
        }
        value = (candidates.get(provider) or "").strip()
        return "" if value.lower() in _PLACEHOLDERS else value

    def is_usable(self, provider: str) -> bool:
        """True when *provider* has everything it needs to talk to the network."""
        return provider in {"mock", "passthrough"} or bool(self.key_for(provider))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
