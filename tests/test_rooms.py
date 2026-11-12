"""Unit tests for the in-memory room registry (``backend/rooms.py``).

No websockets here: participants hold tiny fake sockets, which keeps these
tests fast and independent of FastAPI's transport layer.
"""

from __future__ import annotations

import asyncio
import json

from backend.config import Settings
from backend.rooms import Participant, Room, RoomRegistry


class FakeWebSocket:
    """Minimal stand-in for Starlette's ``WebSocket``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("socket is gone")
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


def make_participant(
    client_id: str,
    *,
    lang: str = "th",
    fail: bool = False,
) -> Participant:
    return Participant(
        client_id=client_id,
        websocket=FakeWebSocket(fail=fail),
        lang=lang,
    )


def received(participant: Participant) -> list[dict]:
    return [json.loads(raw) for raw in participant.websocket.sent]


def make_registry(**overrides) -> RoomRegistry:
    return RoomRegistry(Settings(_env_file=None, **overrides))


# ---------------------------------------------------------------------------
# Room membership
# ---------------------------------------------------------------------------


def test_room_add_remove_and_lookup():
    room = Room("r1", max_clients=2)
    alice = make_participant("alice")

    assert room.is_empty()
    room.add(alice)

    assert not room.is_empty()
    assert room.get("alice") is alice
    assert room.get("nobody") is None
    assert room.remove("alice") is alice
    assert room.remove("alice") is None
    assert room.is_empty()


def test_room_is_full_at_configured_capacity():
    room = Room("r1", max_clients=2)
    assert not room.is_full()

    room.add(make_participant("alice"))
    assert not room.is_full()

    room.add(make_participant("bob"))
    assert room.is_full()


def test_room_roster_and_others():
    room = Room("r1", max_clients=3)
    alice = make_participant("alice", lang="th")
    bob = make_participant("bob", lang="vi")
    room.add(alice)
    room.add(bob)

    assert room.roster() == [
        {"client_id": "alice", "lang": "th"},
        {"client_id": "bob", "lang": "vi"},
    ]
    assert [p.client_id for p in room.others("alice")] == ["bob"]
    assert [p.client_id for p in room.others("bob")] == ["alice"]
    assert [p.client_id for p in room.others("stranger")] == ["alice", "bob"]


def test_holds_uses_identity_not_client_id():
    room = Room("r1", max_clients=2)
    old = make_participant("alice")
    replacement = make_participant("alice")

    room.add(old)
    assert room.holds(old) is True
    assert room.holds(replacement) is False

    # Simulate a reconnect that replaces the object behind the same id.
    room.remove("alice")
    room.add(replacement)
    assert room.holds(replacement) is True
    assert room.holds(old) is False


# ---------------------------------------------------------------------------
# Broadcasting
# ---------------------------------------------------------------------------


def test_broadcast_excludes_client_and_reports_dead_sockets():
    async def scenario():
        room = Room("r1", max_clients=4)
        alice = make_participant("alice")
        bob = make_participant("bob")
        dead = make_participant("dead", fail=True)
        for participant in (alice, bob, dead):
            room.add(participant)

        failed = await room.broadcast(
            {"type": "peer_joined", "client_id": "new"},
            exclude="alice",
        )
        return failed, alice, bob, dead

    failed, alice, bob, dead = asyncio.run(scenario())

    # Failed ``Participant`` objects are reported, not bare client ids.
    assert failed == [dead]
    # The excluded client must not receive the payload.
    assert alice.websocket.sent == []
    assert received(bob) == [{"type": "peer_joined", "client_id": "new"}]


def test_broadcast_without_exclusion_reaches_everyone():
    async def scenario():
        room = Room("r1", max_clients=3)
        alice = make_participant("alice")
        bob = make_participant("bob")
        room.add(alice)
        room.add(bob)
        failed = await room.broadcast({"type": "ping"})
        return failed, alice, bob

    failed, alice, bob = asyncio.run(scenario())

    assert failed == []
    assert received(alice) == [{"type": "ping"}]
    assert received(bob) == [{"type": "ping"}]


def test_purge_removes_dead_participants_and_notifies_survivors():
    async def scenario():
        room = Room("r1", max_clients=3)
        alice = make_participant("alice")
        dead = make_participant("dead", fail=True)
        room.add(alice)
        room.add(dead)

        failed = await room.broadcast({"type": "partial", "text": "hi"})
        await room.purge(failed)
        return room, alice, dead

    room, alice, dead = asyncio.run(scenario())

    assert room.get("dead") is None
    assert room.get("alice") is alice
    assert dead.websocket.closed is True
    assert received(alice) == [
        {"type": "partial", "text": "hi"},
        {"type": "peer_left", "client_id": "dead"},
    ]


def test_purge_never_removes_a_reconnected_participant():
    async def scenario():
        room = Room("r1", max_clients=2)
        stale = make_participant("alice", fail=True)
        room.add(stale)

        failed = await room.broadcast({"type": "ping"})
        assert failed == [stale]

        # A reconnect replaced the stale object before purge ran.
        room.remove("alice")
        fresh = make_participant("alice")
        room.add(fresh)

        await room.purge(failed)
        return room, fresh

    room, fresh = asyncio.run(scenario())

    assert room.get("alice") is fresh


# ---------------------------------------------------------------------------
# RoomRegistry
# ---------------------------------------------------------------------------


def test_get_or_create_reuses_room_and_returns_none_at_max_rooms():
    async def scenario():
        registry = make_registry(max_rooms=2, max_room_clients=3)
        first = await registry.get_or_create("r1")
        again = await registry.get_or_create("r1")
        second = await registry.get_or_create("r2")
        rejected = await registry.get_or_create("r3")
        return registry, first, again, second, rejected

    registry, first, again, second, rejected = asyncio.run(scenario())

    assert first is again  # existing rooms are reused, not recreated
    assert first.max_clients == 3
    assert second is not None
    assert rejected is None
    assert registry.get("r3") is None


def test_drop_if_empty_only_drops_empty_rooms():
    async def scenario():
        registry = make_registry()
        empty = await registry.get_or_create("empty")
        occupied = await registry.get_or_create("occupied")
        occupied.add(make_participant("alice"))

        registry.drop_if_empty("empty")
        dropped_empty = registry.get("empty") is None
        registry.drop_if_empty("occupied")
        kept_occupied = registry.get("occupied") is occupied
        registry.drop_if_empty("does-not-exist")  # must not raise

        return dropped_empty, kept_occupied, empty

    dropped_empty, kept_occupied, empty = asyncio.run(scenario())

    assert dropped_empty is True
    assert kept_occupied is True
    assert empty.room_id == "empty"


def test_stats_and_summary_snapshot():
    async def scenario():
        registry = make_registry(max_room_clients=3)
        r1 = await registry.get_or_create("r1")
        r2 = await registry.get_or_create("r2")
        r1.add(make_participant("alice", lang="th"))
        r1.add(make_participant("bob", lang="vi"))
        r2.add(make_participant("carol", lang="en"))
        return registry.stats(), registry.summary()

    stats, summary = asyncio.run(scenario())

    assert stats == {"rooms": 2, "clients": 3}
    assert summary == [
        {
            "room_id": "r1",
            "participants": [
                {"client_id": "alice", "lang": "th"},
                {"client_id": "bob", "lang": "vi"},
            ],
        },
        {
            "room_id": "r2",
            "participants": [{"client_id": "carol", "lang": "en"}],
        },
    ]
