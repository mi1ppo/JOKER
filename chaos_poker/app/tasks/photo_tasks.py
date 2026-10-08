"""Photo tasks: uploaded photos, access rules, the 8-second showcase, reactions and the album (9.2, 10, 13).

Mixed into Room. File I/O lives in app/media.py; this module only keeps metadata and rules.
"""
from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import Room

SHOWCASE_SEC = 8
UPLOAD_COOLDOWN_SEC = 3.0
REACTIONS = ("👍", "😂", "🔥", "😱", "👏", "❤️")


def new_photo_id() -> str:
    # Unguessable: the id is part of the media URL.
    return secrets.token_urlsafe(12)


class PhotoMixin:
    def check_upload_allowed(self: Room, player_id: str, task_id: str, now: float | None = None) -> dict[str, Any]:
        """Validate an upload before the file is processed; returns the task."""
        task = self.get_task(task_id)
        if task["player_id"] != player_id:
            raise ValueError("The uploaded photo does not match the task owner")
        if task["task_type"] != "photo" or task["status"] != "pending":
            raise ValueError("Task is not waiting for a photo")
        now = time.monotonic() if now is None else now
        last = self.last_upload_at.get(player_id)
        if last is not None and now - last < UPLOAD_COOLDOWN_SEC:
            raise ValueError("Too many uploads, wait a moment")
        self.last_upload_at[player_id] = now
        return task

    def attach_photo(self: Room, player_id: str, task_id: str, photo_id: str) -> dict[str, Any]:
        """Register a processed photo for a task; it goes to the dealer for review."""
        task = self.submit_photo_task(player_id, f"/media/{photo_id}", task_id)
        task["submission"]["photo_id"] = photo_id
        self.photos[photo_id] = {
            "id": photo_id,
            "task_id": task_id,
            "player_id": player_id,
            "status": "review",
            "album": False,
            "reactions": {},
            "created_at": time.time(),
        }
        self.notify_to("dealer", "photo_ready_for_review", {"task_id": task_id, "url": f"/media/{photo_id}"})
        self.announce(f"{self.get_player(player_id).name}: фото отправлено на проверку")
        return self.photos[photo_id]

    def photo_visible(self: Room, photo_id: str, role: str, player_id: str | None) -> bool:
        """Author and dealer see a photo under review; after approval everybody in the room does."""
        photo = self.photos.get(photo_id)
        if photo is None:
            return False
        if photo["status"] == "approved":
            return role in {"host", "dealer", "player"}
        return role == "dealer" or (role == "player" and player_id == photo["player_id"])

    def _on_photo_decided(self: Room, task: dict[str, Any], approved: bool) -> None:
        photo_id = (task.get("submission") or {}).get("photo_id")
        photo = self.photos.get(photo_id) if photo_id else None
        if photo is None:
            return
        photo["status"] = "approved" if approved else "rejected"
        if not approved:
            return
        photo["comment"] = task.get("dealer_comment")
        self.showcase = {"photo_id": photo_id, "task_id": task["id"], "title": task["title"],
                         "player_id": task["player_id"], "url": f"/media/{photo_id}"}
        self.showcase_until = time.monotonic() + SHOWCASE_SEC
        self.notify("photo_show", {**self.showcase, "seconds": SHOWCASE_SEC})

    def react(self: Room, player_id: str, task_id: str, emoji: str) -> None:
        """Emoji reaction to an accepted photo (one per player, the last one counts)."""
        if emoji not in REACTIONS:
            raise ValueError("Unknown reaction")
        self.get_player(player_id)
        photo = self._approved_photo_of_task(task_id)
        photo["reactions"][player_id] = emoji
        self.notify("reaction", {"task_id": task_id, "player_id": player_id, "emoji": emoji})

    def save_to_album(self: Room, photo_id: str) -> None:
        photo = self.photos.get(photo_id)
        if photo is None or photo["status"] != "approved":
            raise ValueError("Only accepted photos can be saved to the album")
        if not photo["album"]:
            photo["album"] = True
            self.announce("Фото добавлено в альбом вечера")

    def album_photo_ids(self: Room) -> set[str]:
        return {photo_id for photo_id, photo in self.photos.items() if photo["album"]}

    def album_entries(self: Room) -> list[dict[str, Any]]:
        entries = []
        for photo in self.photos.values():
            if not photo["album"]:
                continue
            task = self.get_task(photo["task_id"])
            entries.append({
                "photo_id": photo["id"],
                "title": task["title"],
                "player": self.get_player(photo["player_id"]).name,
                "comment": photo.get("comment"),
            })
        return entries

    def gallery(self: Room, player_id: str | None) -> list[dict[str, Any]]:
        """Accepted photos with reaction counts (public)."""
        items = []
        for photo in self.photos.values():
            if photo["status"] != "approved":
                continue
            counts: dict[str, int] = {}
            for emoji in photo["reactions"].values():
                counts[emoji] = counts.get(emoji, 0) + 1
            task = self.get_task(photo["task_id"])
            items.append({
                "photo_id": photo["id"],
                "task_id": photo["task_id"],
                "player_id": photo["player_id"],
                "title": task["title"],
                "url": f"/media/{photo['id']}",
                "reactions": counts,
                "your_reaction": photo["reactions"].get(player_id) if player_id else None,
                "album": photo["album"],
                "comment": photo.get("comment"),
            })
        return items

    def _approved_photo_of_task(self: Room, task_id: str) -> dict[str, Any]:
        photo = next((item for item in self.photos.values() if item["task_id"] == task_id and item["status"] == "approved"), None)
        if photo is None:
            raise ValueError("No accepted photo for this task")
        return photo

    def showcase_view(self: Room) -> dict[str, Any] | None:
        if self.showcase is None or self.showcase_until is None or time.monotonic() >= self.showcase_until:
            return None
        return {**self.showcase, "seconds_left": max(0, round(self.showcase_until - time.monotonic()))}
