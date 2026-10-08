"""Event cards: catalog from data/events.json and effects implemented as engine hooks (section 5).

Only events implemented here can be played: the AI dealer picks from the catalog
and cannot invent new mechanics.
"""
from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.game.evaluator import evaluate_hand, evaluate_hand_with_joker

if TYPE_CHECKING:
    from app.game.engine import EnginePlayer, HoldemEngine

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "events.json"

# How often an event happens for each chaos level: every N-th hand (section 12).
CHAOS_EVENT_EVERY = {"light": 3, "medium": 2, "full": 1}

VETO_WINDOW_SEC = 8
CAROUSEL_WINDOW_SEC = 15


class EventEffect:
    """Base effect: every hook is a no-op, so an event overrides only what it changes."""

    event_id = ""

    def on_hand_start(self, engine: HoldemEngine) -> None:
        """Before blinds are posted and cards are dealt."""

    def modify_blinds(self, small: int, big: int) -> tuple[int, int]:
        return small, big

    def on_after_deal(self, engine: HoldemEngine) -> None:
        """Right after hole cards are dealt, before the first action."""

    def on_reveal_flop(self, engine: HoldemEngine) -> None:
        """Right after the flop is dealt."""

    def hand_rank(self, engine: HoldemEngine, player: EnginePlayer) -> tuple[int, ...]:
        return evaluate_hand(player.hole_cards + engine.board)

    def compare_hands(self, ranks: dict[str, tuple[int, ...]]) -> list[str]:
        """Winners among the given ranks (all of them eligible for one pot)."""
        best = max(ranks.values())
        return [player_id for player_id, rank in ranks.items() if rank == best]

    def on_showdown(self, engine: HoldemEngine) -> None:
        """When the hand reaches the showdown, before pots are awarded."""

    def hidden_board(self, engine: HoldemEngine) -> set[int]:
        """Indexes of community cards that must be sent face down."""
        return set()


class Mirror(EventEffect):
    event_id = "mirror"

    def compare_hands(self, ranks: dict[str, tuple[int, ...]]) -> list[str]:
        worst = min(ranks.values())
        return [player_id for player_id, rank in ranks.items() if rank == worst]


class DoubleStakes(EventEffect):
    event_id = "double_stakes"

    def modify_blinds(self, small: int, big: int) -> tuple[int, int]:
        # The minimum raise follows the big blind, so it doubles as well.
        return small * 2, big * 2


class Spy(EventEffect):
    event_id = "spy"

    def on_after_deal(self, engine: HoldemEngine) -> None:
        candidates = [player for player in engine.players if not player.folded and player.hole_cards]
        if not candidates:
            return
        player = engine.rng.choice(candidates)
        index = engine.rng.randrange(len(player.hole_cards))
        engine.event_state["spy"] = {
            "player_id": player.player_id,
            "card_index": index,
            "card": str(player.hole_cards[index]),
        }


class Carousel(EventEffect):
    event_id = "carousel"

    def on_after_deal(self, engine: HoldemEngine) -> None:
        # Betting waits until every player has chosen a card (or the room timer picks randomly).
        engine.pending_choice = "carousel"
        engine.carousel_picks = {}


class BlindFlop(EventEffect):
    event_id = "blind_flop"

    def hidden_board(self, engine: HoldemEngine) -> set[int]:
        if engine.went_to_showdown or engine.street.value == "showdown":
            return set()
        return {index for index in range(3) if index < len(engine.board)}


class WealthTax(EventEffect):
    event_id = "wealth_tax"

    def on_hand_start(self, engine: HoldemEngine) -> None:
        seated = engine.seated()
        if len(seated) < 2:
            return
        # TODO: уточнить — при равенстве стеков берётся первый по порядку мест.
        richest = max(seated, key=lambda player: player.stack)
        poorest = min(seated, key=lambda player: player.stack)
        amount = richest.stack // 10
        if richest is poorest or richest.stack == poorest.stack or amount == 0:
            engine.event_state["wealth_tax"] = {"amount": 0}
            return
        richest.stack -= amount
        poorest.stack += amount
        engine.event_state["wealth_tax"] = {
            "from_id": richest.player_id,
            "to_id": poorest.player_id,
            "amount": amount,
        }


class Joker(EventEffect):
    event_id = "joker"

    def on_hand_start(self, engine: HoldemEngine) -> None:
        # The joker position is fixed now but becomes public only when that card is revealed.
        engine.joker_index = engine.rng.randrange(5)

    def hand_rank(self, engine: HoldemEngine, player: EnginePlayer) -> tuple[int, ...]:
        joker = engine.joker_card()
        return evaluate_hand_with_joker(player.hole_cards + engine.board, joker)


EFFECTS: dict[str, type[EventEffect]] = {
    effect.event_id: effect
    for effect in (Mirror, DoubleStakes, Spy, Carousel, BlindFlop, WealthTax, Joker)
}


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, dict[str, Any]]:
    with DATA_FILE.open(encoding="utf-8") as file:
        entries = json.load(file)
    catalog: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry["id"] not in EFFECTS:
            raise ValueError(f"Event {entry['id']!r} from events.json has no implementation")
        if entry.get("weight", 1) <= 0:
            continue
        catalog[entry["id"]] = {"icon": "⚡", "weight": 1, **entry}
    return catalog


def event_catalog() -> dict[str, dict[str, Any]]:
    return {event_id: config.copy() for event_id, config in _load_catalog().items()}


def make_effect(event_id: str) -> EventEffect:
    if event_id not in _load_catalog():
        raise ValueError(f"Unknown event: {event_id}")
    return EFFECTS[event_id]()


def random_event_id(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    catalog = _load_catalog()
    ids = list(catalog)
    return rng.choices(ids, weights=[catalog[event_id]["weight"] for event_id in ids])[0]


def event_due(chaos_level: str, hand_number: int) -> bool:
    """Whether the chaos schedule asks for an event on this (1-based) hand."""
    every = CHAOS_EVENT_EVERY.get(chaos_level, CHAOS_EVENT_EVERY["light"])
    return hand_number % every == 0
