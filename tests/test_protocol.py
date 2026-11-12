"""Protocol tests for the FastAPI app (``backend.main``).

These run against the offline providers (mock STT + passthrough translation),
so no audio is really transcribed and no network call is made:

* mock STT emits ``[mock <lang> partial]`` once ~1 s of audio has arrived and
  ``[mock <lang> final · <seconds>s]`` on ``end_utterance``;
* passthrough translation prefixes the text with ``[<tgt>] ``.
"""

from __future__ import annotations

import pytest
from fastapi import WebSocketDisconnect


def join(ws, lang: str) -> dict:
    ws.send_json({"type": "join", "lang": lang})
    return ws.receive_json()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_healthz_reports_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stt"] == "mock"
    assert body["translator"] == "passthrough"
    assert body["rooms"] == 0
    assert body["clients"] == 0
    assert body["uptime_s"] >= 0


def test_index_serves_frontend_html(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "WebTalk" in response.text


def test_room_url_also_serves_frontend_html(client):
    response = client.get("/room/demo")

    assert response.status_code == 200
    assert "WebTalk" in response.text


# ---------------------------------------------------------------------------
# Join / peer notifications
# ---------------------------------------------------------------------------


def test_join_returns_joined_payload(client):
    with client.websocket_connect("/ws/room-a/alice") as ws:
        # A client-supplied sample_rate must be ignored: the server always
        # advertises its own fixed 16 kHz rate.
        ws.send_json({"type": "join", "lang": "th", "sample_rate": 48_000})
        payload = ws.receive_json()

    assert payload == {
        "type": "joined",
        "client_id": "alice",
        "room_id": "room-a",
        "lang": "th",
        "sample_rate": 16_000,
        "stt": "mock",
        "targets": ["th", "vi", "en"],
        "participants": [],
    }


def test_second_client_join_notifies_first(client):
    with client.websocket_connect("/ws/room-b/alice") as alice:
        join(alice, "th")

        with client.websocket_connect("/ws/room-b/bob") as bob:
            joined = join(bob, "vi")

            # The newcomer sees the people already in the room.
            assert joined["participants"] == [
                {"client_id": "alice", "lang": "th"}
            ]
            # ...and the incumbent is told about it.
            notice = alice.receive_json()

    assert notice == {"type": "peer_joined", "client_id": "bob", "lang": "vi"}


# ---------------------------------------------------------------------------
# Audio -> partial -> final
# ---------------------------------------------------------------------------


def test_binary_audio_and_end_utterance_broadcast_partial_and_final(client):
    with client.websocket_connect("/ws/room-c/speaker") as speaker:
        join(speaker, "vi")

        with client.websocket_connect("/ws/room-c/listener") as listener:
            join(listener, "th")

            # 48 000 bytes ~= 1.5 s of 16 kHz mono PCM16 -> one partial.
            speaker.send_bytes(b"\x00\x00" * 24_000)
            partial = listener.receive_json()
            assert partial == {
                "type": "partial",
                "from": "speaker",
                "src_lang": "vi",
                "text": "[mock vi partial]",
            }

            speaker.send_json({"type": "end_utterance"})
            final = listener.receive_json()

    assert final["type"] == "final"
    assert final["from"] == "speaker"
    assert final["src_lang"] == "vi"
    assert final["id"] == "speaker-1"
    assert final["text"] == "[mock vi final · 1.5s]"
    # Source language is excluded, every other target is translated exactly once.
    assert final["translations"] == {
        "th": f"[th] {final['text']}",
        "en": f"[en] {final['text']}",
    }
    assert final["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# Control messages
# ---------------------------------------------------------------------------


def test_ping_returns_pong_with_same_timestamp(client):
    with client.websocket_connect("/ws/room-d/alice") as ws:
        join(ws, "th")
        ws.send_json({"type": "ping", "t": 1731400000000})
        pong = ws.receive_json()

    assert pong == {"type": "pong", "t": 1731400000000}


def test_unknown_message_type_returns_bad_message(client):
    with client.websocket_connect("/ws/room-e/alice") as ws:
        join(ws, "th")
        ws.send_json({"type": "teleport"})
        error = ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "bad_message"
    assert "teleport" in error["message"]


def test_invalid_json_returns_bad_message(client):
    with client.websocket_connect("/ws/room-f/alice") as ws:
        join(ws, "th")
        ws.send_text("{not json")
        error = ws.receive_json()

    assert error == {
        "type": "error",
        "code": "bad_message",
        "message": "invalid JSON",
    }


def test_join_with_unsupported_lang_falls_back_to_default(client):
    with client.websocket_connect("/ws/room-f2/alice") as ws:
        ws.send_json({"type": "join", "lang": "kr"})

        error = ws.receive_json()
        joined = ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "bad_message"
    assert "kr" in error["message"]
    assert joined["type"] == "joined"
    assert joined["lang"] == "th"


# ---------------------------------------------------------------------------
# Leave / room lifecycle
# ---------------------------------------------------------------------------


def test_leave_notifies_peer_and_drops_empty_room(client):
    with client.websocket_connect("/ws/room-g/alice") as alice:
        join(alice, "th")

        with client.websocket_connect("/ws/room-g/bob") as bob:
            join(bob, "vi")
            assert alice.receive_json()["type"] == "peer_joined"

            alice.send_json({"type": "leave"})
            assert bob.receive_json() == {
                "type": "peer_left",
                "client_id": "alice",
            }

            rooms = client.get("/api/rooms").json()
            assert rooms["stats"] == {"rooms": 1, "clients": 1}
            assert rooms["requests"] == [
                {
                    "room_id": "room-g",
                    "participants": [{"client_id": "bob", "lang": "vi"}],
                }
            ]

            bob.send_json({"type": "leave"})
            # The server closes the socket during teardown; that is how we know
            # the empty room has been garbage-collected before the assertion.
            with pytest.raises(WebSocketDisconnect):
                bob.receive_json()

    rooms = client.get("/api/rooms").json()
    assert rooms["stats"] == {"rooms": 0, "clients": 0}
    assert rooms["requests"] == []


# ---------------------------------------------------------------------------
# Limits and validation
# ---------------------------------------------------------------------------


def test_reconnect_with_same_client_id_evicts_the_stale_socket(client):
    with client.websocket_connect("/ws/room-re/alice") as stale:
        join(stale, "th")

        with client.websocket_connect("/ws/room-re/alice") as fresh:
            joined = join(fresh, "vi")

            assert joined["client_id"] == "alice"
            assert joined["lang"] == "vi"
            assert joined["participants"] == []

            # The previous socket is closed instead of being rejected with a
            # duplicate_client error.
            with pytest.raises(WebSocketDisconnect):
                stale.receive_json()

            rooms = client.get("/api/rooms").json()
            assert rooms["stats"] == {"rooms": 1, "clients": 1}
            assert rooms["requests"] == [
                {
                    "room_id": "room-re",
                    "participants": [{"client_id": "alice", "lang": "vi"}],
                }
            ]


def test_room_full_rejects_extra_client(api_client):
    with api_client(MAX_ROOM_CLIENTS=1) as client:
        with client.websocket_connect("/ws/tiny/first") as first:
            join(first, "th")

            with client.websocket_connect("/ws/tiny/second") as second:
                error = second.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "room_full"
                assert error["message"] == "room is full"

                with pytest.raises(WebSocketDisconnect) as excinfo:
                    second.receive_json()
                assert excinfo.value.code == 1013


def test_server_room_limit_rejects_a_new_room(api_client):
    with api_client(MAX_ROOMS=1) as client:
        with client.websocket_connect("/ws/first-room/alice") as first:
            join(first, "th")

            with client.websocket_connect("/ws/second-room/bob") as second:
                error = second.receive_json()
                assert error["type"] == "error"
                assert error["code"] == "room_limit"

                with pytest.raises(WebSocketDisconnect) as excinfo:
                    second.receive_json()
                assert excinfo.value.code == 1013


@pytest.mark.parametrize(
    "room_id",
    [
        "bad%20id!",  # space + punctuation
        "bad.id",  # dot is not allowed
        "abc%0A",  # trailing newline must not slip through fullmatch
    ],
)
def test_invalid_room_id_is_rejected_with_close_code_1008(client, room_id):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws/{room_id}/client"):
            pass

    assert excinfo.value.code == 1008
