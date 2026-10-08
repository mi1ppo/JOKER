from __future__ import annotations

import secrets
import time

from app.models import Room as RoomModel, new_token, tokens_equal


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[str, RoomModel] = {}

    def create_room(self, host_name: str) -> RoomModel:
        room_id = f"room_{secrets.token_hex(4)}"
        while room_id in self.rooms:
            room_id = f"room_{secrets.token_hex(4)}"
        room = RoomModel(
            room_id=room_id,
            host_name=host_name,
            host_token=new_token(),
            dealer_token=new_token(),
            player_token=new_token(),
        )
        self.rooms[room.room_id] = room
        return room

    def get_room(self, room_id: str) -> RoomModel | None:
        return self.rooms.get(room_id)

    def close_room(self, room_id: str) -> None:
        self.rooms.pop(room_id, None)

    def find_by_token(self, token: str) -> tuple[RoomModel | None, str | None]:
        """Resolve a join token or a session token to its room and role."""
        for room in self.rooms.values():
            if tokens_equal(room.host_token, token):
                return room, "host"
            if tokens_equal(room.dealer_token, token):
                return room, "dealer"
            if tokens_equal(room.dealer_session_token, token):
                return room, "dealer"
            if tokens_equal(room.player_token, token):
                return room, "player"
            if room.player_by_session(token) is not None:
                return room, "player"
        return None, None

    def find_session(self, token: str) -> tuple[RoomModel | None, str | None, str | None]:
        """Resolve only session tokens (not one-time join tokens): room, role, player_id."""
        for room in self.rooms.values():
            if tokens_equal(room.host_token, token):
                return room, "host", None
            if tokens_equal(room.dealer_session_token, token):
                return room, "dealer", None
            player = room.player_by_session(token)
            if player is not None:
                return room, "player", player.id
        return None, None, None

    def process_timers(self) -> list[str]:
        """Run turn timers and next-hand timers; return ids of rooms whose state changed."""
        changed: list[str] = []
        now = time.monotonic()
        for room_id, room in self.rooms.items():
            if room.process_timers(now):
                changed.append(room_id)
        return changed

    # Backwards-compatible name.
    process_turn_timeouts = process_timers
