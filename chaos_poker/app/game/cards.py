from __future__ import annotations

import random
from dataclasses import dataclass

RANKS = "23456789TJQKA"
SUITS = "shdc"
RANK_VALUES = {rank: index + 2 for index, rank in enumerate(RANKS)}


@dataclass(frozen=True, slots=True)
class Card:
    rank: str
    suit: str

    def __post_init__(self) -> None:
        if self.rank not in RANKS or self.suit not in SUITS:
            raise ValueError(f"Invalid card: {self.rank}{self.suit}")

    @classmethod
    def parse(cls, value: str) -> "Card":
        if len(value) != 2:
            raise ValueError(f"Invalid card: {value}")
        return cls(value[0], value[1])

    @property
    def value(self) -> int:
        return RANK_VALUES[self.rank]

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


class Deck:
    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self.cards = [Card(rank, suit) for suit in SUITS for rank in RANKS]
        self._rng.shuffle(self.cards)

    def deal(self) -> Card:
        if not self.cards:
            raise IndexError("Deck is empty")
        return self.cards.pop()

    def burn(self) -> Card:
        return self.deal()

    def deal_many(self, count: int) -> list[Card]:
        if count < 0 or count > len(self.cards):
            raise ValueError("Not enough cards")
        return [self.deal() for _ in range(count)]
