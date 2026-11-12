"""Free mode: browser-side STT (``transcript`` messages) + key-free translation."""

from __future__ import annotations

import asyncio

from backend.adapters.translate.lingva import LingvaTranslator


def next_of_type(ws, wanted: set[str], limit: int = 12) -> dict:
    """Read messages until one matches, skipping unrelated room events."""
    for _ in range(limit):
        payload = ws.receive_json()
        if payload.get("type") in wanted:
            return payload
    raise AssertionError(f"no {wanted} message arrived")


# --------------------------------------------------------------- protocol


def test_joined_reports_browser_stt(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/c1") as ws:
            ws.send_json({"type": "join", "lang": "th"})
            joined = next_of_type(ws, {"joined"})
            assert joined["stt"] == "browser"


def test_final_transcript_is_translated_and_broadcast(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/spk") as speaker:
            speaker.send_json({"type": "join", "lang": "vi"})
            next_of_type(speaker, {"joined"})

            with client.websocket_connect("/ws/free/lsn") as listener:
                listener.send_json({"type": "join", "lang": "th"})
                next_of_type(listener, {"joined"})
                next_of_type(speaker, {"peer_joined"})

                speaker.send_json(
                    {"type": "transcript", "text": "xin chào", "is_final": True}
                )

                heard = next_of_type(listener, {"final"})
                assert heard["src_lang"] == "vi"
                assert heard["text"] == "xin chào"
                assert heard["translations"]["th"] == "[th] xin chào"
                assert heard["translations"]["en"] == "[en] xin chào"

                # The speaker gets its own final too (echo_self defaults true).
                own = next_of_type(speaker, {"final"})
                assert own["id"] == heard["id"]


def test_interim_transcript_is_a_partial(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/a") as a:
            a.send_json({"type": "join", "lang": "th"})
            next_of_type(a, {"joined"})
            a.send_json({"type": "transcript", "text": "สวัส", "is_final": False})
            partial = next_of_type(a, {"partial"})
            assert partial["text"] == "สวัส"


def test_audio_is_ignored_in_browser_mode(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/x") as ws:
            ws.send_json({"type": "join", "lang": "th"})
            next_of_type(ws, {"joined"})

            # Would produce a mock partial/final if audio were still processed.
            ws.send_bytes(b"\x00\x00" * 24_000)
            ws.send_json({"type": "transcript", "text": "สวัสดี", "is_final": True})

            first = ws.receive_json()
            assert first["type"] == "final", first
            assert first["text"] == "สวัสดี"


def test_transcript_before_join_is_rejected(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/early") as ws:
            ws.send_json({"type": "transcript", "text": "hi", "is_final": True})
            error = next_of_type(ws, {"error"})
            assert error["code"] == "bad_message"


def test_oversized_transcript_is_rejected(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/free/big") as ws:
            ws.send_json({"type": "join", "lang": "th"})
            next_of_type(ws, {"joined"})
            ws.send_json({"type": "transcript", "text": "ก" * 2_001, "is_final": True})
            error = next_of_type(ws, {"error"})
            assert error["code"] == "bad_message"


# ------------------------------------------------------------- lingva unit


def test_lingva_same_language_is_a_passthrough():
    translator = LingvaTranslator(hosts=["https://unused.invalid"], mymemory=False)
    assert asyncio.run(translator.translate("สวัสดี", "th", "th")) == "สวัสดี"


def test_lingva_caches_repeated_phrases(monkeypatch):
    translator = LingvaTranslator(hosts=["https://unused.invalid"], mymemory=False)
    calls: list[str] = []

    async def fake(text: str, src: str, tgt: str) -> str:
        calls.append(text)
        return "สวัสดี"

    monkeypatch.setattr(translator, "_via_lingva", fake)

    first = asyncio.run(translator.translate("xin chào", "vi", "th"))
    second = asyncio.run(translator.translate("xin chào", "vi", "th"))
    assert first == second == "สวัสดี"
    assert calls == ["xin chào"], "second call must be served from cache"


def test_lingva_falls_back_to_mymemory(monkeypatch):
    translator = LingvaTranslator(hosts=["https://unused.invalid"], mymemory=True)

    async def no_lingva(text: str, src: str, tgt: str) -> str:
        return ""

    async def mymemory(text: str, src: str, tgt: str) -> str:
        return "สวัสดีครับ"

    monkeypatch.setattr(translator, "_via_lingva", no_lingva)
    monkeypatch.setattr(translator, "_via_mymemory", mymemory)

    assert asyncio.run(translator.translate("xin chào", "vi", "th")) == "สวัสดีครับ"


def test_lingva_returns_source_when_all_backends_fail(monkeypatch):
    translator = LingvaTranslator(hosts=["https://unused.invalid"], mymemory=False)

    async def no_lingva(text: str, src: str, tgt: str) -> str:
        return ""

    monkeypatch.setattr(translator, "_via_lingva", no_lingva)

    assert asyncio.run(translator.translate("xin chào", "vi", "th")) == "xin chào"
