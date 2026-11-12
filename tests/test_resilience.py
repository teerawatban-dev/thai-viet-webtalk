"""Resilience: token bucket, translator circuit breaker, dedupe, rate limiting."""

from __future__ import annotations

import asyncio

from backend.adapters.translate.lingva import LingvaTranslator
from backend.limits import TokenBucket


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


# ------------------------------------------------------------------ limiter


def test_token_bucket_allows_burst_then_blocks():
    bucket = TokenBucket(rate=0.001, capacity=3)
    assert [bucket.allow() for _ in range(3)] == [True, True, True]
    assert bucket.allow() is False


# --------------------------------------------------------- circuit breaker


def test_circuit_opens_after_two_failures_and_stops_calling(monkeypatch):
    translator = LingvaTranslator(
        hosts=["https://dead.example"], mymemory=False, cooldown_s=600
    )
    calls: list[str] = []

    async def always_403(url, params=None):
        calls.append(url)
        return FakeResponse(403)

    monkeypatch.setattr(translator._client, "get", always_403)

    # 1st phrase: one failed attempt, host still considered healthy.
    assert asyncio.run(translator.translate("một", "vi", "th")) == "một"
    assert len(calls) == 1
    assert translator.healthy_hosts() == ["https://dead.example"]

    # 2nd phrase: second failure opens the circuit.
    assert asyncio.run(translator.translate("hai", "vi", "th")) == "hai"
    assert len(calls) == 2
    assert translator.healthy_hosts() == []
    assert translator.stats()["hosts_down"] == 1

    # 3rd phrase: no HTTP request at all, straight to the fallback.
    assert asyncio.run(translator.translate("ba", "vi", "th")) == "ba"
    assert len(calls) == 2, "open circuit must not be probed"
    assert translator.stats()["circuit_open"] >= 1


def test_success_closes_the_circuit(monkeypatch):
    translator = LingvaTranslator(
        hosts=["https://flaky.example"], mymemory=False, failures_before_open=1
    )

    async def fail(url, params=None):
        return FakeResponse(500)

    monkeypatch.setattr(translator._client, "get", fail)
    asyncio.run(translator.translate("một", "vi", "th"))
    assert translator.healthy_hosts() == []

    async def succeed(url, params=None):
        return FakeResponse(200, {"translation": "หนึ่ง"})

    monkeypatch.setattr(translator._client, "get", succeed)
    # Cooldown has not expired, so the host must be reopened manually.
    translator._open_until.clear()
    assert asyncio.run(translator.translate("hai", "vi", "th")) == "หนึ่ง"
    assert translator.healthy_hosts() == ["https://flaky.example"]


def test_falls_back_to_mymemory_when_lingva_is_down(monkeypatch):
    translator = LingvaTranslator(
        hosts=["https://dead.example"], mymemory=True, failures_before_open=1
    )

    async def always_403(url, params=None):
        return FakeResponse(403)

    async def mymemory(text, src, tgt):
        return "สวัสดี"

    monkeypatch.setattr(translator._client, "get", always_403)
    monkeypatch.setattr(translator, "_via_mymemory", mymemory)

    assert asyncio.run(translator.translate("xin chào", "vi", "th")) == "สวัสดี"
    assert translator.stats()["fallback"] == 1


# --------------------------------------------------------------- protocol


def _next_type(ws, wanted: set[str], limit: int = 40) -> dict | None:
    for _ in range(limit):
        payload = ws.receive_json()
        if payload.get("type") in wanted:
            return payload
    return None


def test_identical_finals_are_deduped(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/dedupe/c1") as ws:
            ws.send_json({"type": "join", "lang": "th"})
            _next_type(ws, {"joined"})

            for _ in range(2):
                ws.send_json({"type": "transcript", "text": "สวัสดี", "is_final": True})

            first = ws.receive_json()
            assert first["type"] == "final"

            # If a second final were emitted it would arrive before this pong.
            ws.send_json({"type": "ping", "t": 7})
            nxt = ws.receive_json()
            assert nxt["type"] == "pong", nxt


def test_transcript_flood_is_rate_limited(api_client):
    with api_client(STT_PROVIDER="browser") as client:
        with client.websocket_connect("/ws/flood/c1") as ws:
            ws.send_json({"type": "join", "lang": "th"})
            _next_type(ws, {"joined"})

            for i in range(30):
                ws.send_json(
                    {"type": "transcript", "text": f"ข้อความ {i}", "is_final": True}
                )

            error = _next_type(ws, {"error"})
            assert error is not None and error["code"] == "rate_limited", error
