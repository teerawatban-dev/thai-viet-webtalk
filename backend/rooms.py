"""In-memory room registry.

MVP constraint: state lives in this process, so the Render service must run
exactly one instance. Multi-instance would need Redis pub/sub (Phase 4).

Two invariants keep reconnect/teardown races safe:

* a participant is identified by ``(client_id, websocket)`` — a reconnect with
  the same ``client_id`` replaces the old object, so teardown of the stale
  socket must never remove the new one;
* nothing awaits a single peer's socket without a timeout, otherwise one stuck
  TCP buffer would stall the whole room.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from backend.config import Settings
from backend.limits import TokenBucket
from backend.pipeline import TranscriptPipeline

log = logging.getLogger(__name__)

GC_INTERVAL_SECONDS = 60
SEND_TIMEOUT_SECONDS = 5.0


@dataclass
class Participant:
    client_id: str
    websocket: Any
    lang: str
    echo_self: bool = True
    sample_rate: int = 16_000
    announced: bool = False
    warned_oversize: bool = False
    pipeline: TranscriptPipeline | None = None
    joined_at: float = field(default_factory=time.time)
    #: 6 transcripts/s with a 12-token burst — far above human speech.
    transcript_limiter: TokenBucket = field(
        default_factory=lambda: TokenBucket(rate=6.0, capacity=12.0)
    )
    #: 200 ms frames = 5/s; allow 30/s so bursts and catch-up still pass.
    audio_limiter: TokenBucket = field(
        default_factory=lambda: TokenBucket(rate=30.0, capacity=60.0)
    )
    rate_warned_at: float = 0.0

    def public_view(self) -> dict[str, str]:
        return {"client_id": self.client_id, "lang": self.lang}


async def close_participant(participant: Participant) -> None:
    """Best-effort resource release for a participant leaving the room."""
    if participant.pipeline is not None:
        try:
            await participant.pipeline.close()
        except Exception:
            log.exception("pipeline close failed for %s", participant.client_id)
    try:
        await asyncio.wait_for(
            participant.websocket.close(), timeout=SEND_TIMEOUT_SECONDS
        )
    except Exception:
        pass


class Room:
    def __init__(self, room_id: str, max_clients: int) -> None:
        self.room_id = room_id
        self.max_clients = max_clients
        self.participants: dict[str, Participant] = {}
        self.created_at = time.time()
        self.last_active = self.created_at
        self._bg_tasks: set[asyncio.Task] = set()

    # -- membership -------------------------------------------------------
    def is_full(self) -> bool:
        return len(self.participants) >= self.max_clients

    def add(self, participant: Participant) -> None:
        self.participants[participant.client_id] = participant
        self.last_active = time.time()

    def remove(self, client_id: str) -> Participant | None:
        participant = self.participants.pop(client_id, None)
        self.last_active = time.time()
        return participant

    def get(self, client_id: str) -> Participant | None:
        return self.participants.get(client_id)

    def holds(self, participant: Participant) -> bool:
        """True when *this exact object* (not just the id) is still in the room."""
        return self.participants.get(participant.client_id) is participant

    def others(self, client_id: str) -> list[Participant]:
        return [p for p in self.participants.values() if p.client_id != client_id]

    def roster(self) -> list[dict[str, str]]:
        return [p.public_view() for p in self.participants.values()]

    def is_empty(self) -> bool:
        return not self.participants

    # -- messaging --------------------------------------------------------
    @staticmethod
    async def _send_one(participant: Participant, data: str) -> bool:
        try:
            await asyncio.wait_for(
                participant.websocket.send_text(data), timeout=SEND_TIMEOUT_SECONDS
            )
            return True
        except Exception:
            log.info("dropping unreachable client %s", participant.client_id)
            return False

    async def broadcast(
        self, payload: dict, *, exclude: str | None = None
    ) -> list[Participant]:
        """Fan out *payload* in parallel. Returns the participants that failed."""
        targets = [
            p
            for p in list(self.participants.values())
            if exclude is None or p.client_id != exclude
        ]
        self.last_active = time.time()
        if not targets:
            return []

        data = json.dumps(payload, ensure_ascii=False)
        results = await asyncio.gather(*(self._send_one(p, data) for p in targets))
        return [p for p, ok in zip(targets, results, strict=False) if not ok]

    async def send(self, client_id: str, payload: dict) -> bool:
        participant = self.participants.get(client_id)
        if participant is None:
            return False
        return await self._send_one(
            participant, json.dumps(payload, ensure_ascii=False)
        )

    async def purge(self, dead: list[Participant]) -> None:
        """Remove failed participants and notify the survivors.

        Safe against replacement: a dead object is only removed when it is
        still the live entry for that client id.
        """
        queue = list(dead)
        while queue:
            current, queue = queue, []
            for participant in current:
                if not self.holds(participant):
                    continue
                del self.participants[participant.client_id]
                log.info("purged dead client %s", participant.client_id)
                try:
                    await asyncio.wait_for(
                        close_participant(participant),
                        timeout=SEND_TIMEOUT_SECONDS * 2,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "close timed out for %s", participant.client_id
                    )
                queue.extend(
                    await self.broadcast(
                        {"type": "peer_left", "client_id": participant.client_id}
                    )
                )
        self.last_active = time.time()

    def schedule_purge(self, dead: list[Participant]) -> None:
        """Clean up dead peers *outside* the caller's call stack.

        Never ``await purge`` from inside a broadcast: the failure may be the
        emitter itself, and closing its pipeline would try to flush a final
        through the emit lock the emitter is still holding.
        """
        if not dead:
            return
        task = asyncio.create_task(self.purge(dead))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)


class RoomRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rooms: dict[str, Room] = {}
        self._lock = asyncio.Lock()

    # -- access -----------------------------------------------------------
    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    async def get_or_create(self, room_id: str) -> Room | None:
        async with self._lock:
            existing = self._rooms.get(room_id)
            if existing is not None:
                return existing
            if len(self._rooms) >= self._settings.max_rooms:
                log.warning("room limit reached (%s)", self._settings.max_rooms)
                return None
            room = Room(room_id, self._settings.max_room_clients)
            self._rooms[room_id] = room
            log.info("room created: %s", room_id)
            return room

    def drop_if_empty(self, room_id: str) -> None:
        room = self._rooms.get(room_id)
        if room is not None and room.is_empty():
            self._rooms.pop(room_id, None)
            log.info("room dropped: %s", room_id)

    # -- maintenance ------------------------------------------------------
    async def gc_loop(self) -> None:
        ttl = self._settings.room_ttl_seconds
        while True:
            try:
                await asyncio.sleep(GC_INTERVAL_SECONDS)
                now = time.time()
                stale = [
                    room_id
                    for room_id, room in self._rooms.items()
                    if room.is_empty() and now - room.last_active > ttl
                ]
                for room_id in stale:
                    self._rooms.pop(room_id, None)
                if stale:
                    log.info("gc removed %d idle room(s)", len(stale))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("room gc iteration failed")

    def stats(self) -> dict[str, int]:
        return {
            "rooms": len(self._rooms),
            "clients": sum(len(room.participants) for room in self._rooms.values()),
        }

    def summary(self) -> list[dict[str, object]]:
        """Read-only snapshot for ``GET /api/rooms``."""
        return [
            {"room_id": room.room_id, "participants": room.roster()}
            for room in list(self._rooms.values())
        ]
