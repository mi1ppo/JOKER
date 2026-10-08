"""Human dealer powers that are not tasks: decrees (section 8.1). Mixed into Room."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import Room

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "decrees.json"

# Decrees the engine enforces; the others are announced only.
ENGINE_DECREES = {"bets_multiple_of_bb", "raise_cap"}
NO_DECREE = "none"


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, dict[str, Any]]:
    with DATA_FILE.open(encoding="utf-8") as file:
        return {entry["id"]: entry for entry in json.load(file)}


def decree_catalog() -> dict[str, dict[str, Any]]:
    return {decree_id: dict(entry) for decree_id, entry in _load_catalog().items()}


class DecreeMixin:
    def decree_cooldown(self: Room) -> int:
        """Hands left until the dealer may issue the next decree (0 = available now)."""
        if self.last_decree_hand is None:
            return 0
        return max(0, self.last_decree_hand + self.settings.decree_interval_hands - self.hand_number)

    def issue_decree(self: Room, decree_id: str, text: str | None = None) -> dict[str, Any] | None:
        """Proclaim a decree (or cancel the current one with "none"). Lasts until replaced or cancelled."""
        if self.engine is None or not self.game_started:
            raise ValueError("Decrees are issued during the game")
        if decree_id == NO_DECREE:
            if self.active_decree is not None:
                self.announce(f"Указ «{self.active_decree['title']}» отменён")
            self.active_decree = None
            self.engine.decree = None
            return None
        catalog = _load_catalog()
        if decree_id not in catalog:
            raise ValueError(f"Unknown decree: {decree_id}")
        if self.decree_cooldown() > 0:
            raise ValueError(f"Next decree is possible in {self.decree_cooldown()} hands")
        text = (text or "").strip()
        if decree_id == "free" and not text:
            raise ValueError("A free decree needs a text")
        entry = catalog[decree_id]
        self.active_decree = {
            "id": decree_id,
            "title": entry["title"],
            "icon": entry.get("icon", "📜"),
            "description": text or entry["description"],
            "hand_number": self.hand_number,
        }
        self.engine.decree = decree_id if decree_id in ENGINE_DECREES else None
        self.last_decree_hand = self.hand_number
        self.announce(f"📜 Указ судьи: {self.active_decree['title']} — {self.active_decree['description']}")
        self.notify("decree", dict(self.active_decree))
        return self.active_decree
