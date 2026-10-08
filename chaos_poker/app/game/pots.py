from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SidePot:
    amount: int
    eligible: tuple[str, ...]
    cap: int


def build_side_pots(contributions: dict[str, int], folded: set[str] | None = None) -> list[SidePot]:
    folded = folded or set()
    if any(amount < 0 for amount in contributions.values()):
        raise ValueError("Contributions cannot be negative")
    levels = sorted({amount for amount in contributions.values() if amount > 0})
    pots: list[SidePot] = []
    previous = 0
    for level in levels:
        width = level - previous
        amount = width * sum(value >= level for value in contributions.values())
        eligible = tuple(player_id for player_id, value in contributions.items() if value >= level and player_id not in folded)
        if amount and eligible:
            pots.append(SidePot(amount=amount, eligible=eligible, cap=level))
        elif amount:
            pots.append(SidePot(amount=amount, eligible=(), cap=level))
        previous = level
    return pots


def split_pot(amount: int, winners: list[str], order: list[str]) -> dict[str, int]:
    if amount < 0 or not winners:
        raise ValueError("Amount and winners must be valid")
    unique_winners = [player_id for player_id in order if player_id in winners]
    if not unique_winners:
        raise ValueError("Winners must be in order")
    share, remainder = divmod(amount, len(unique_winners))
    result = {player_id: share for player_id in unique_winners}
    for player_id in unique_winners[:remainder]:
        result[player_id] += 1
    return result
