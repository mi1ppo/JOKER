from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Lobby spiciness / dealer difficulty -> numeric level used in data/fallback_tasks.json.
LEVELS = {
    "mild": 1, "easy": 1, "мягкий": 1,
    "medium": 2, "средний": 2,
    "spicy": 3, "hard": 3, "острый": 3,
}

def level_of(difficulty: str | int) -> int:
    if isinstance(difficulty, int):
        return min(max(difficulty, 1), 3)
    return LEVELS.get(str(difficulty).lower(), 2)


@lru_cache(maxsize=1)
def _bank() -> dict[str, list[dict[str, Any]]]:
    """data/fallback_tasks.json: safe tasks doable in a flat (section 9)."""
    path = DATA_DIR / "fallback_tasks.json"
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def photo_task_entries(difficulty: str | int) -> list[dict[str, Any]]:
    level = level_of(difficulty)
    bank = _bank()["photo"]
    # Fall back to easier tasks when a level has no entries yet.
    return [entry for entry in bank if entry.get("difficulty", 1) <= level] or list(bank)


def fallback_tasks(difficulty: str | int, *, photo: bool) -> list[str]:
    """Task texts from the fallback bank for the given level."""
    if photo:
        return [entry["title"] for entry in photo_task_entries(difficulty)]
    level = level_of(difficulty)
    return [entry["title"] for entry in _bank()["text"] if entry["level"] == level]


def pick_task(difficulty: str | int, *, photo: bool, rng: random.Random | None = None) -> dict[str, Any]:
    """Return a ready task payload (title, description, time limit, camera)."""
    rng = rng or random.Random()
    if photo:
        entry = rng.choice(photo_task_entries(difficulty))
        return {
            "title": entry["title"],
            "description": entry.get("description", entry["title"]),
            "time_limit_sec": int(entry.get("time_limit_sec", 60)),
            "camera": entry.get("camera", "back"),
        }
    text = rng.choice(fallback_tasks(difficulty, photo=False))
    return {"title": text, "description": text, "time_limit_sec": 60, "camera": None}
