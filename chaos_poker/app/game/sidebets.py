"""Side bets (section 7): statements offered for a hand and how they resolve. Pure logic, no I/O."""
from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.game.cards import Card

PAIR_ON_BOARD = "pair_on_board"
FLOP_SUITED = "flop_suited"
SHOWDOWN = "showdown"
PLAYER_WINS = "player_wins"

# How many statements the server offers for one hand.
OFFERS_PER_HAND = 3


def make_offers(seated_ids: list[str], rng: random.Random, count: int = OFFERS_PER_HAND) -> list[dict[str, Any]]:
    """Pick the statements for this hand; "player X wins" gets a random seated player."""
    kinds = rng.sample([PAIR_ON_BOARD, FLOP_SUITED, SHOWDOWN, PLAYER_WINS], count)
    offers = []
    for kind in kinds:
        if kind == PLAYER_WINS:
            player_id = rng.choice(seated_ids)
            offers.append({"id": f"{PLAYER_WINS}:{player_id}", "kind": kind, "player_id": player_id})
        else:
            offers.append({"id": kind, "kind": kind})
    return offers


def offer_kind(bet_id: str) -> str:
    return bet_id.split(":", 1)[0]


def resolve(bet_id: str, *, board: list[Card], main_pot_winners: list[str], went_to_showdown: bool) -> bool:
    """Whether a statement came true.

    TODO: уточнить — если раздача закончилась до ривера, «пара на столе» и «флоп одной масти»
    проверяются только по реально открытым картам.
    """
    kind = offer_kind(bet_id)
    if kind == PAIR_ON_BOARD:
        ranks = [card.rank for card in board]
        return len(ranks) != len(set(ranks))
    if kind == FLOP_SUITED:
        return len(board) >= 3 and len({card.suit for card in board[:3]}) == 1
    if kind == SHOWDOWN:
        return went_to_showdown
    if kind == PLAYER_WINS:
        return bet_id.split(":", 1)[1] in main_pot_winners
    raise ValueError(f"Unknown side bet: {bet_id}")
