"""Ability catalog (data/abilities.json) and dealing rules (section 6). Effects live in Room.use_ability."""
from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "abilities.json"

IMPLEMENTED = {"peek", "redraw", "veto", "freeze", "steal"}
MAX_IN_HAND = 3
STARTING_COUNT = 2
# Every N finished hands each player with chips receives one more ability.
GRANT_EVERY_HANDS = 5


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, dict[str, Any]]:
    with DATA_FILE.open(encoding="utf-8") as file:
        entries = json.load(file)
    catalog: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry["id"] not in IMPLEMENTED:
            raise ValueError(f"Ability {entry['id']!r} from abilities.json has no implementation")
        catalog[entry["id"]] = {"icon": "✨", "target": None, **entry}
    return catalog


def ability_catalog() -> dict[str, dict[str, Any]]:
    return {ability_id: config.copy() for ability_id, config in _load_catalog().items()}


def ability_lookup(ability_id: str) -> dict[str, Any]:
    catalog = _load_catalog()
    if ability_id not in catalog:
        raise ValueError(f"Unknown ability: {ability_id}")
    return catalog[ability_id].copy()


def ability_ids() -> list[str]:
    return list(_load_catalog())


def starting_abilities(count: int = STARTING_COUNT, *, available: list[str] | None = None,
                       rng: random.Random | None = None) -> list[str]:
    """`count` different random abilities."""
    candidates = available or ability_ids()
    if count <= 0:
        return []
    if count >= len(candidates):
        return list(candidates)
    return (rng or random).sample(candidates, count)


def draw_ability(held: list[str], rng: random.Random | None = None) -> str | None:
    """One more ability for a player, preferring ones he does not hold; None when the hand is full."""
    if len(held) >= MAX_IN_HAND:
        return None
    candidates = [ability_id for ability_id in ability_ids() if ability_id not in held] or ability_ids()
    return (rng or random).choice(candidates)
