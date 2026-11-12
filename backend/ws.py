"""WebSocket endpoint: connection lifecycle, message dispatch, broadcast.

Wire protocol (v1) is documented in DEVELOPER_CONTEXT.md §5.

* binary frames  -> raw mono PCM16LE audio, 16 kHz (200 ms frames)
* text frames    -> JSON control messages (``join``, ``set_lang``, ``ping`` ...)

Reconnect policy: a client may reuse its ``client_id``. The stale connection is
evicted and the new one takes over, so a dropped Wi-Fi link never locks the
user out of their own room.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from backend.config import SUPPORTED_LANGS, Settings
from backend.pipeline import TranscriptPipeline
from backend.rooms import Participant, Room, RoomRegistry, close_participant

log = logging.getLogger(__name__)

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SAMPLE_RATE = 16_000

MAX_AUDIO_CHUNK_BYTES = 64 * 1024
MAX_TEXT_MESSAGE_BYTES = 8 * 1024
MAX_TRANSCRIPT_CHARS = 2_000


def _valid_id(value: str) -> bool:
    return bool(ID_RE.fullmatch(value))


async def handle_websocket(websocket: WebSocket, room_id: str, client_id: str) -> None:
    """FastAPI websocket endpoint for ``/ws/{room_id}/{client_id}``."""
    if not _valid_id(room_id) or not _valid_id(client_id):
        await websocket.close(code=1008, reason="invalid room_id or client_id")
        return

    state = websocket.app.state
    settings: Settings = state.settings
    registry: RoomRegistry = state.registry

    await websocket.accept()

    room = await registry.get_or_create(room_id)
    if room is None:
        await _send_error(websocket, "room_limit", "server room limit reached")
        await websocket.close(code=1013)
        return

    await _evict_stale(room, client_id)

    if room.is_full():
        await _send_error(websocket, "room_full", "room is full")
        await websocket.close(code=1013)
        return

    participant = Participant(
        client_id=client_id,
        websocket=websocket,
        lang=settings.default_src_lang,
    )
    room.add(participant)
    log.info(
        "client %s joined %s (%d in room)", client_id, room_id, len(room.participants)
    )

    try:
        await _run_session(
            websocket=websocket,
            room=room,
            participant=participant,
            settings=settings,
            state=state,
        )
    except WebSocketDisconnect:
        log.info("client %s disconnected", client_id)
    except Exception:
        log.exception("websocket handler crashed for %s", client_id)
    finally:
        await _teardown(room, participant)
        registry.drop_if_empty(room.room_id)


async def _evict_stale(room: Room, client_id: str) -> None:
    """Replace a previous connection that used the same client id."""
    stale = room.get(client_id)
    if stale is None:
        return
    log.info("evicting stale connection for %s", client_id)
    room.remove(client_id)
    await close_participant(stale)
    dead = await room.broadcast({"type": "peer_left", "client_id": client_id})
    if dead:
        room.schedule_purge(dead)


async def _run_session(
    *,
    websocket: WebSocket,
    room: Room,
    participant: Participant,
    settings: Settings,
    state,
) -> None:
    while True:
        message = await websocket.receive()

        if message.get("type") == "websocket.disconnect":
            return

        audio = message.get("bytes")
        if audio is not None:
            await _handle_audio(room, participant, audio)
            continue

        text = message.get("text")
        if text is None:
            continue

        if len(text.encode("utf-8")) > MAX_TEXT_MESSAGE_BYTES:
            await _send_error(websocket, "bad_message", "text message too large")
            continue

        keep_open = await _handle_control(
            text=text,
            websocket=websocket,
            room=room,
            participant=participant,
            settings=settings,
            state=state,
        )
        if not keep_open:
            return


async def _handle_audio(room: Room, participant: Participant, audio: bytes) -> None:
    if not audio:
        return
    if len(audio) > MAX_AUDIO_CHUNK_BYTES:
        if not participant.warned_oversize:
            participant.warned_oversize = True
            log.warning(
                "dropping oversized audio chunk (%d bytes) from %s",
                len(audio),
                participant.client_id,
            )
            await _send_error(
                participant.websocket,
                "bad_message",
                f"audio frame over {MAX_AUDIO_CHUNK_BYTES} bytes; dropped",
            )
        return
    if participant.pipeline is None:
        # Audio before `join`: ignore instead of buffering unbounded data.
        return
    if getattr(participant.pipeline, "provider_name", "") == "browser":
        # Browser STT mode: audio is recognised client-side, nothing to do.
        return
    await participant.pipeline.feed(audio)
    _ = room  # reserved for future VAD / per-room metering


async def _handle_control(
    *,
    text: str,
    websocket: WebSocket,
    room: Room,
    participant: Participant,
    settings: Settings,
    state,
) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        await _send_error(websocket, "bad_message", "invalid JSON")
        return True

    if not isinstance(payload, dict):
        await _send_error(websocket, "bad_message", "message must be an object")
        return True

    kind = payload.get("type")

    if kind == "join":
        await _on_join(websocket, room, participant, payload, settings, state)
        return True

    if kind == "ping":
        await room.send(participant.client_id, {"type": "pong", "t": payload.get("t")})
        return True

    if kind == "set_lang":
        await _on_set_lang(websocket, room, participant, payload)
        return True

    if kind == "transcript":
        await _on_transcript(websocket, participant, payload)
        return True

    if kind == "end_utterance":
        if participant.pipeline is not None:
            await participant.pipeline.end_utterance()
        return True

    if kind == "leave":
        return False

    await _send_error(websocket, "bad_message", f"unknown message type: {kind!r}")
    return True


async def _on_join(
    websocket: WebSocket,
    room: Room,
    participant: Participant,
    payload: dict,
    settings: Settings,
    state,
) -> None:
    lang = str(payload.get("lang") or settings.default_src_lang).lower()
    if lang not in SUPPORTED_LANGS:
        await _send_error(websocket, "bad_message", f"unsupported lang: {lang}")
        lang = settings.default_src_lang

    participant.lang = lang
    participant.echo_self = payload.get("echo_self", True) not in (False, "false", 0, None)

    if participant.pipeline is None:
        participant.pipeline = _build_pipeline(room, participant, settings, state)
        if not await participant.pipeline.start():
            # Drop the dead pipeline so a later `join` retries the provider.
            pipeline, participant.pipeline = participant.pipeline, None
            await pipeline.close()
            await _send_error(
                websocket, "stt_unavailable", "speech-to-text unavailable; audio ignored"
            )

    await room.send(
        participant.client_id,
        {
            "type": "joined",
            "client_id": participant.client_id,
            "room_id": room.room_id,
            "lang": participant.lang,
            "sample_rate": SAMPLE_RATE,
            "stt": state.provider.name,
            "targets": settings.targets,
            "participants": [
                p.public_view()
                for p in room.participants.values()
                if p.client_id != participant.client_id
            ],
        },
    )

    if not participant.announced:
        participant.announced = True
        dead = await room.broadcast(
            {
                "type": "peer_joined",
                "client_id": participant.client_id,
                "lang": participant.lang,
            },
            exclude=participant.client_id,
        )
        if dead:
            room.schedule_purge(dead)


async def _on_transcript(
    websocket: WebSocket,
    participant: Participant,
    payload: dict,
) -> None:
    """Client-side STT: the browser already recognised the speech."""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        await _send_error(websocket, "bad_message", "transcript text required")
        return
    if len(text) > MAX_TRANSCRIPT_CHARS:
        await _send_error(websocket, "bad_message", "transcript too long")
        return

    pipeline = participant.pipeline
    if pipeline is None:
        await _send_error(websocket, "bad_message", "join before sending transcripts")
        return

    if payload.get("is_final", True):
        await pipeline.submit_final(text)
    else:
        await pipeline.submit_partial(text)


async def _on_set_lang(
    websocket: WebSocket,
    room: Room,
    participant: Participant,
    payload: dict,
) -> None:
    lang = str(payload.get("lang") or "").lower()
    if lang not in SUPPORTED_LANGS:
        await _send_error(websocket, "bad_message", f"unsupported lang: {lang!r}")
        return

    participant.lang = lang
    if participant.pipeline is not None:
        if not await participant.pipeline.set_lang(lang):
            await _send_error(
                websocket, "stt_unavailable", "speech-to-text unavailable after language switch"
            )

    dead = await room.broadcast(
        {"type": "peer_lang", "client_id": participant.client_id, "lang": lang},
        exclude=participant.client_id,
    )
    if dead:
        room.schedule_purge(dead)
    await room.send(participant.client_id, {"type": "lang_set", "lang": lang})


def _build_pipeline(
    room: Room,
    participant: Participant,
    settings: Settings,
    state,
) -> TranscriptPipeline:
    client_id = participant.client_id

    async def emit_partial(text: str) -> None:
        dead = await room.broadcast(
            {
                "type": "partial",
                "from": client_id,
                "src_lang": participant.lang,
                "text": text,
            },
            exclude=None if participant.echo_self else client_id,
        )
        if dead:
            room.schedule_purge(dead)

    async def emit_final(
        text: str,
        translations: dict[str, str],
        latency_ms: int,
        utterance_id: str,
    ) -> None:
        dead = await room.broadcast(
            {
                "type": "final",
                "id": utterance_id,
                "from": client_id,
                "src_lang": participant.lang,
                "text": text,
                "translations": translations,
                "latency_ms": latency_ms,
            },
            exclude=None if participant.echo_self else client_id,
        )
        if dead:
            room.schedule_purge(dead)

    return TranscriptPipeline(
        client_id=client_id,
        lang=participant.lang,
        provider=state.provider,
        translator=state.translator,
        targets=settings.targets,
        emit_partial=emit_partial,
        emit_final=emit_final,
    )


async def _teardown(room: Room, participant: Participant) -> None:
    if not room.holds(participant):
        # Already replaced by a reconnect (or purged): only close our socket.
        try:
            if participant.websocket.client_state is WebSocketState.CONNECTED:
                await participant.websocket.close()
        except Exception:
            pass
        return

    room.remove(participant.client_id)
    await close_participant(participant)

    if participant.announced:
        dead = await room.broadcast(
            {"type": "peer_left", "client_id": participant.client_id}
        )
        if dead:
            room.schedule_purge(dead)

    log.info("client %s left %s", participant.client_id, room.room_id)


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_text(
            json.dumps({"type": "error", "code": code, "message": message})
        )
    except Exception:
        pass
