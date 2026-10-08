from __future__ import annotations

from itertools import combinations
from collections import Counter

from app.game.cards import RANKS, SUITS, Card

# Category values increase from high card to straight flush.
HIGH_CARD = 0
PAIR = 1
TWO_PAIR = 2
THREE_OF_A_KIND = 3
STRAIGHT = 4
FLUSH = 5
FULL_HOUSE = 6
FOUR_OF_A_KIND = 7
STRAIGHT_FLUSH = 8

ALL_CARDS = tuple(Card(rank, suit) for suit in SUITS for rank in RANKS)


def _five_card_rank(cards: tuple[Card, ...]) -> tuple[int, ...]:
    values = sorted((card.value for card in cards), reverse=True)
    counts = Counter(values)
    groups = sorted(((count, value) for value, count in counts.items()), reverse=True)
    flush = len({card.suit for card in cards}) == 1
    unique = set(values)
    straight_high = max(unique) if len(unique) == 5 and max(unique) - min(unique) == 4 else 0
    if unique == {14, 2, 3, 4, 5}:
        straight_high = 5
    if flush and straight_high:
        return STRAIGHT_FLUSH, straight_high
    if groups[0][0] == 4:
        four = groups[0][1]
        return FOUR_OF_A_KIND, four, groups[1][1]
    if groups[0][0] == 3 and groups[1][0] == 2:
        return FULL_HOUSE, groups[0][1], groups[1][1]
    if flush:
        return FLUSH, *values
    if straight_high:
        return STRAIGHT, straight_high
    if groups[0][0] == 3:
        kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
        return THREE_OF_A_KIND, groups[0][1], *kickers
    pairs = sorted((value for value, count in counts.items() if count == 2), reverse=True)
    if len(pairs) == 2:
        kicker = next(value for value, count in counts.items() if count == 1)
        return TWO_PAIR, *pairs, kicker
    if len(pairs) == 1:
        kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
        return PAIR, pairs[0], *kickers
    return HIGH_CARD, *values


def evaluate_hand(cards: list[Card] | tuple[Card, ...]) -> tuple[int, ...]:
    if len(cards) < 5 or len(cards) > 7:
        raise ValueError("A Hold'em hand must contain 5 to 7 cards")
    return max(_five_card_rank(combo) for combo in combinations(tuple(cards), 5))


def best_five(cards: list[Card] | tuple[Card, ...]) -> tuple[Card, ...]:
    """The five cards forming the best combination (for highlighting at the showdown)."""
    if len(cards) < 5:
        return tuple(cards)
    return max(combinations(tuple(cards), 5), key=_five_card_rank)


def evaluate_hand_with_joker(cards: list[Card] | tuple[Card, ...], joker: Card | None) -> tuple[int, ...]:
    """Best rank when `joker` may stand for any card the player does not already hold (event `joker`)."""
    if joker is None or joker not in cards:
        return evaluate_hand(cards)
    others = [card for card in cards if card != joker]
    used = set(others)
    return max(evaluate_hand(others + [candidate]) for candidate in ALL_CARDS if candidate not in used)


def evaluate_strings(values: list[str]) -> tuple[int, ...]:
    return evaluate_hand([Card.parse(value) for value in values])
