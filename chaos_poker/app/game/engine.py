from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.game.cards import Card, Deck
from app.game import sidebets
from app.game.events import EventEffect, make_effect
from app.game.pots import SidePot, build_side_pots, split_pot

class Street(StrEnum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"
    COMPLETE = "complete"

@dataclass(slots=True)
class EnginePlayer:
    player_id: str
    stack: int
    hole_cards: list[Card] = field(default_factory=list)
    folded: bool = False
    all_in: bool = False
    committed: int = 0
    street_commitment: int = 0
    # Busted players stay seated as spectators and are skipped when dealing.
    out: bool = False

class HoldemEngine:
    """Network-free state machine for one or more Texas Hold'em hands."""

    BLIND_LEVELS = ((10, 20), (20, 40), (40, 80), (75, 150), (100, 200), (150, 300), (200, 400), (300, 600))
    RAISE_CAP = 2  # raises per betting round under the raise_cap decree

    def __init__(
        self,
        players: list[EnginePlayer],
        *,
        dealer_index: int = 0,
        blind_level: int = 0,
        blind_interval_hands: int = 8,
        deck: Deck | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not 2 <= len(players) <= 8:
            raise ValueError("Hold'em requires 2 to 8 players")
        if any(player.stack < 0 for player in players):
            raise ValueError("Player stacks cannot be negative")
        if blind_interval_hands < 1:
            raise ValueError("Blind interval must be positive")
        self.players = players
        self.dealer_index = dealer_index % len(players)
        self.start_blind_level = blind_level
        self.blind_level = blind_level
        self.blind_interval_hands = blind_interval_hands
        self.rng = rng or random.Random()
        self.deck = deck or Deck(rng=self.rng)
        # Event of the current hand and its public results (spy card, tax, ...).
        self.event: EventEffect | None = None
        self.event_state: dict[str, Any] = {}
        self.joker_index: int | None = None
        # While set (e.g. "carousel"), betting waits for players' choices.
        self.pending_choice: str | None = None
        self.carousel_picks: dict[str, int] = {}
        # (player_id, action) of the current hand, for statistics such as bluffs.
        self.hand_actions: list[tuple[str, str]] = []
        self.board: list[Card] = []
        self.street = Street.PREFLOP
        self.current_index: int | None = None
        self.current_bet = 0
        self.last_raise_size = 0
        self.pots: list[SidePot] = []
        self.hand_started = False
        self.hands_played = 0
        self.settled = False
        self.went_to_showdown = False
        self._acted: set[str] = set()
        self.frozen_players: set[str] = set()
        # Dealer decree affecting betting ("bets_multiple_of_bb" | "raise_cap" | None).
        self.decree: str | None = None
        self.raises_this_round = 0
        # Dead money (fines, refused penalties) that goes into the main pot of the next hand.
        self.carryover = 0
        self.hand_carryover = 0
        self.winnings: dict[str, int] = {}
        self.main_pot_winners: list[str] = []
        # Side bets (section 7): statements offered this hand, bets by player and the separate pool.
        self.side_bets_enabled = True
        self.side_bet_offers: list[dict[str, Any]] = []
        self.side_bet_pool = 0
        self.side_bets: dict[str, str] = {}
        self.side_bet_results: dict[str, Any] = {}

    def _blinds(self) -> tuple[int, int]:
        small, big = self.BLIND_LEVELS[min(self.blind_level, len(self.BLIND_LEVELS) - 1)]
        if self.event is not None:
            small, big = self.event.modify_blinds(small, big)
        return small, big

    @property
    def small_blind(self) -> int:
        return self._blinds()[0]

    @property
    def big_blind(self) -> int:
        return self._blinds()[1]

    @property
    def hand_over(self) -> bool:
        return self.street in {Street.SHOWDOWN, Street.COMPLETE}

    def player(self, player_id: str) -> EnginePlayer:
        found = next((item for item in self.players if item.player_id == player_id), None)
        if found is None:
            raise ValueError(f"Unknown player: {player_id}")
        return found

    def seated(self) -> list[EnginePlayer]:
        """Players who still have chips (or are in the current hand)."""
        return [player for player in self.players if not player.out]

    def set_event(self, event_id: str | None) -> None:
        """Choose the event for the hand that has not started yet (None = no event)."""
        if self.hand_started:
            raise ValueError("The event must be set before the hand starts")
        self.event = make_effect(event_id) if event_id is not None else None

    def start_hand(self, event_id: str | None = None) -> None:
        if self.hand_started:
            raise ValueError("Hand has already started")
        if event_id is not None:
            self.set_event(event_id)
        for player in self.players:
            player.out = player.stack <= 0
        if len(self.seated()) < 2:
            raise ValueError("At least 2 players with chips are required")
        if self.players[self.dealer_index].out:
            self.dealer_index = self._next_index(self.dealer_index)
        self.hand_started = True
        self.settled = False
        self.went_to_showdown = False
        self.winnings = {}
        self.pots = []
        self.board.clear()
        self.street = Street.PREFLOP
        self.current_bet = 0
        self.last_raise_size = self.big_blind
        self._acted.clear()
        self.frozen_players.clear()
        self.raises_this_round = 0
        self.event_state = {}
        self.joker_index = None
        self.pending_choice = None
        self.carousel_picks = {}
        self.hand_actions = []
        self.side_bet_pool = 0
        self.side_bets = {}
        self.side_bet_results = {}
        self.hand_carryover, self.carryover = self.carryover, 0
        self.side_bet_offers = (
            sidebets.make_offers([player.player_id for player in self.seated()], self.rng)
            if self.side_bets_enabled else []
        )
        if self.event is not None:
            self.event.on_hand_start(self)
        for player in self.players:
            player.hole_cards = [] if player.out else self.deck.deal_many(2)
            player.folded = player.out
            player.all_in = False
            player.committed = 0
            player.street_commitment = 0
        if self.event is not None:
            self.event.on_after_deal(self)
        small_index, big_index = self._blind_positions()
        self._commit(self.players[small_index], self.small_blind)
        self._commit(self.players[big_index], self.big_blind)
        self.current_bet = max(self.players[small_index].street_commitment, self.players[big_index].street_commitment)
        self.current_index = self._next_action_index(big_index)
        if self.pending_choice is None:
            self._start_betting()

    def _start_betting(self) -> None:
        if self._betting_round_complete():
            self.current_index = None
        self._finish_if_no_actions()

    def prepare_next_hand(self) -> None:
        """Finish the current hand: rotate the button, raise blinds on schedule, fresh deck.

        The next hand is not dealt yet, so an event can still be revealed and vetoed.
        """
        if not self.hand_over:
            raise ValueError("Current hand is not finished")
        if not self.settled:
            self.settle_showdown()
        self.hands_played += 1
        self.blind_level = self.start_blind_level + self.hands_played // self.blind_interval_hands
        for player in self.players:
            player.out = player.stack <= 0
            player.hole_cards = []
            player.folded = player.out
            player.all_in = False
            player.committed = 0
            player.street_commitment = 0
        self.dealer_index = self._next_index(self.dealer_index)
        self.deck = Deck(rng=self.rng)
        self.board.clear()
        self.pots = []
        self.event = None
        self.event_state = {}
        self.joker_index = None
        self.street = Street.PREFLOP
        self.current_index = None
        self.current_bet = 0
        self.hand_started = False

    def start_next_hand(self, event_id: str | None = None) -> None:
        """Rotate the button, raise blinds on schedule and deal a fresh hand."""
        self.prepare_next_hand()
        self.start_hand(event_id)

    # ---- carousel (event choice phase) ------------------------------------

    def carousel_waiting(self) -> list[str]:
        if self.pending_choice != "carousel":
            return []
        return [player.player_id for player in self.seated() if player.player_id not in self.carousel_picks]

    def pick_carousel(self, player_id: str, card_index: int) -> None:
        if self.pending_choice != "carousel":
            raise ValueError("No carousel in progress")
        player = self.player(player_id)
        if player.out:
            raise ValueError("Player is not in this hand")
        if card_index not in range(len(player.hole_cards)):
            raise ValueError("Invalid card index")
        self.carousel_picks[player_id] = card_index

    def resolve_carousel(self) -> None:
        """Every seated player passes the chosen card (random if none) to the next player clockwise."""
        if self.pending_choice != "carousel":
            return
        seated_indexes = [index for index, player in enumerate(self.players) if not player.out]
        passed: dict[int, Card] = {}
        for index in seated_indexes:
            player = self.players[index]
            pick = self.carousel_picks.get(player.player_id)
            if pick is None:
                pick = self.rng.randrange(len(player.hole_cards))
                self.carousel_picks[player.player_id] = pick
            passed[self._next_index(index)] = player.hole_cards.pop(pick)
        for index, card in passed.items():
            self.players[index].hole_cards.append(card)
        self.event_state["carousel"] = {"done": True}
        self.pending_choice = None
        self._start_betting()

    # ---- board visibility -----------------------------------------------------

    def joker_card(self) -> Card | None:
        if self.joker_index is None or self.joker_index >= len(self.board):
            return None
        return self.board[self.joker_index]

    def visible_board(self) -> list[str | None]:
        """Community cards as sent to clients: hidden ones (blind flop) become None."""
        hidden = self.event.hidden_board(self) if self.event is not None else set()
        return [None if index in hidden else str(card) for index, card in enumerate(self.board)]

    def _blind_positions(self) -> tuple[int, int]:
        if len(self.seated()) == 2:
            return self.dealer_index, self._next_index(self.dealer_index)
        small = self._next_index(self.dealer_index)
        return small, self._next_index(small)

    def _next_index(self, index: int) -> int:
        """Next seat clockwise that is not busted."""
        for offset in range(1, len(self.players) + 1):
            candidate = (index + offset) % len(self.players)
            if not self.players[candidate].out:
                return candidate
        return index

    def _next_action_index(self, index: int) -> int | None:
        for offset in range(1, len(self.players) + 1):
            candidate = self.players[(index + offset) % len(self.players)]
            if not candidate.folded and not candidate.all_in:
                return (index + offset) % len(self.players)
        return None

    def _first_postflop_index(self) -> int | None:
        return self._next_action_index(self.dealer_index)

    def _commit(self, player: EnginePlayer, amount: int) -> int:
        paid = min(max(amount, 0), player.stack)
        player.stack -= paid
        player.committed += paid
        player.street_commitment += paid
        if player.stack == 0:
            player.all_in = True
        return paid

    def _active(self) -> list[EnginePlayer]:
        return [player for player in self.players if not player.folded]

    def _can_bet(self) -> list[EnginePlayer]:
        return [player for player in self._active() if not player.all_in]

    def legal_actions(self) -> dict[str, int | bool]:
        if self.current_index is None or self.hand_over:
            return {"check": False, "call": False, "raise": False, "all_in": False, "fold": False}
        player = self.players[self.current_index]
        to_call = max(0, self.current_bet - player.street_commitment)
        # Freeze ability and the raise_cap decree forbid raising; all-in is fine only up to the call.
        no_raise = player.player_id in self.frozen_players or (
            self.decree == "raise_cap" and self.raises_this_round >= self.RAISE_CAP
        )
        min_raise_to = self.current_bet + max(self.last_raise_size, self.big_blind)
        max_raise_to = player.street_commitment + player.stack
        step = 1
        if self.decree == "bets_multiple_of_bb":
            step = self.big_blind
            min_raise_to = -(-min_raise_to // step) * step
            max_raise_to = max_raise_to // step * step
        can_raise = not no_raise and max_raise_to >= min_raise_to
        return {
            "check": to_call == 0,
            "call": to_call > 0 and player.stack > 0,
            "raise": can_raise,
            "all_in": player.stack > 0 and (not no_raise or player.stack <= to_call),
            "fold": True,
            "to_call": min(to_call, player.stack),
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "raise_step": step,
        }

    def act(self, player_id: str, kind: str, amount: int | None = None) -> None:
        if self.pending_choice is not None:
            raise ValueError("Waiting for event choices")
        if self.current_index is None or self.players[self.current_index].player_id != player_id:
            raise ValueError("It is not this player's turn")
        player = self.players[self.current_index]
        legal = self.legal_actions()
        if kind == "fold":
            player.folded = True
        elif kind == "check":
            if not legal["check"]:
                raise ValueError("Check is not legal")
        elif kind == "call":
            if not legal["call"]:
                raise ValueError("Call is not legal")
            self._commit(player, int(legal["to_call"]))
        elif kind == "raise":
            if player_id in self.frozen_players:
                raise ValueError("Frozen players cannot raise")
            if not legal["raise"]:
                raise ValueError("Raise is not allowed now")
            target = amount if amount is not None else int(legal["min_raise_to"])
            if target < int(legal["min_raise_to"]) or target > player.street_commitment + player.stack:
                raise ValueError("Invalid raise amount")
            if target % int(legal["raise_step"]) != 0:
                raise ValueError("Bets must be multiples of the big blind")
            self._raise_to(player, target)
        elif kind == "all_in":
            if not legal["all_in"]:
                raise ValueError("All-in is not legal")
            target = player.street_commitment + player.stack
            if target <= self.current_bet:
                self._commit(player, player.stack)
            else:
                self._raise_to(player, target)
        else:
            raise ValueError(f"Unknown action: {kind}")
        self._acted.add(player.player_id)
        self.hand_actions.append((player.player_id, kind))
        self._advance_or_next()

    def _raise_to(self, player: EnginePlayer, target: int) -> None:
        raise_size = target - self.current_bet
        # A short all-in does not change the minimum raise size.
        # TODO: уточнить, должен ли неполный all-in-рейз переоткрывать торги.
        if raise_size >= self.last_raise_size:
            self.last_raise_size = raise_size
        self._commit(player, target - player.street_commitment)
        self.current_bet = target
        self.raises_this_round += 1

    def _advance_or_next(self) -> None:
        if len(self._active()) <= 1:
            self.street = Street.COMPLETE
            self.current_index = None
            self._build_pots()
            return
        if self._betting_round_complete():
            self._advance_street()
            return
        next_idx = self._next_action_index(self.current_index)
        if next_idx is None:
            self._advance_street()
        else:
            self.current_index = next_idx

    def _betting_round_complete(self) -> bool:
        eligible = self._can_bet()
        if not eligible:
            return True
        if len(eligible) == 1:
            # Nobody can respond to this player, so he only has to match the bet.
            return eligible[0].street_commitment >= self.current_bet
        return all(
            player.player_id in self._acted and player.street_commitment == self.current_bet
            for player in eligible
        )

    def _build_pots(self) -> None:
        self.pots = build_side_pots(
            {player.player_id: player.committed for player in self.players},
            {player.player_id for player in self.players if player.folded},
        )

    def _advance_street(self) -> None:
        self.frozen_players.clear()
        self.raises_this_round = 0
        for player in self.players:
            player.street_commitment = 0
        self._acted.clear()
        self.current_bet = 0
        self.last_raise_size = self.big_blind
        if self.street == Street.PREFLOP:
            self.deck.burn()
            self.board.extend(self.deck.deal_many(3))
            self.street = Street.FLOP
            if self.event is not None:
                self.event.on_reveal_flop(self)
        elif self.street == Street.FLOP:
            self.deck.burn()
            self.board.append(self.deck.deal())
            self.street = Street.TURN
        elif self.street == Street.TURN:
            self.deck.burn()
            self.board.append(self.deck.deal())
            self.street = Street.RIVER
        elif self.street == Street.RIVER:
            self.street = Street.SHOWDOWN
            self.current_index = None
            self._build_pots()
            return
        # With fewer than two players able to bet there is nothing to play: run the board out.
        if len(self._can_bet()) < 2:
            self.current_index = None
        else:
            self.current_index = self._first_postflop_index()
        self._finish_if_no_actions()

    def _finish_if_no_actions(self) -> None:
        if self.current_index is None and not self.hand_over:
            self._advance_street()

    def charge(self, player_id: str, amount: int) -> int:
        """Take chips (a fine) into the next hand's main pot; returns the amount actually paid.

        A player who still has to act in the current hand keeps at least one chip.
        """
        player = self.player(player_id)
        in_hand = self.hand_started and not self.hand_over and not player.folded and not player.all_in
        limit = player.stack - 1 if in_hand else player.stack
        paid = max(0, min(amount, limit))
        player.stack -= paid
        self.carryover += paid
        return paid

    @property
    def side_bets_open(self) -> bool:
        """Side bets are accepted preflop before the first voluntary action."""
        return (
            self.side_bets_enabled
            and self.hand_started
            and self.street == Street.PREFLOP
            and not self._acted
        )

    def place_side_bet(self, player_id: str, bet_id: str) -> int:
        """Stake one big blind on one of this hand's statements; returns the stake."""
        if not self.side_bets_open:
            raise ValueError("Side bets are closed")
        if player_id in self.side_bets:
            raise ValueError("Player already placed a side bet for this hand")
        if bet_id not in {offer["id"] for offer in self.side_bet_offers}:
            raise ValueError(f"Unknown side bet option: {bet_id}")
        player = self.player(player_id)
        if player.folded or player.out:
            raise ValueError("Only players in the hand can place side bets")
        amount = self.big_blind
        # Keep at least one chip so the player can still act in the hand.
        if player.stack <= amount:
            raise ValueError("Not enough chips for a side bet")
        player.stack -= amount
        self.side_bets[player_id] = bet_id
        self.side_bet_pool += amount
        return amount

    def _resolve_side_bets(self, main_pot_winners: list[str], order: list[str]) -> list[str]:
        """Pay the side pool to the players who guessed right; returns them (empty: pool goes to the main pot)."""
        outcomes = {
            offer["id"]: sidebets.resolve(
                offer["id"], board=self.board, main_pot_winners=main_pot_winners,
                went_to_showdown=self.went_to_showdown,
            )
            for offer in self.side_bet_offers
        }
        winners = [player_id for player_id, bet_id in self.side_bets.items() if outcomes.get(bet_id)]
        payouts = split_pot(self.side_bet_pool, winners, order) if winners and self.side_bet_pool else {}
        for player_id, amount in payouts.items():
            self.player(player_id).stack += amount
        self.side_bet_results = {
            "outcomes": outcomes,
            "winner_ids": winners,
            "payouts": payouts,
            "pool": self.side_bet_pool,
            "to_main_pot": bool(self.side_bet_pool) and not winners,
        }
        return winners

    def settle_showdown(self) -> dict[str, int]:
        """Award every pot to its best eligible hands and finish the hand."""
        if not self.hand_over:
            raise ValueError("Showdown is not ready")
        if self.settled:
            return dict(self.winnings)
        self._build_pots()
        by_id = {player.player_id: player for player in self.players}
        active = self._active()
        effect = self.event or EventEffect()
        ranks: dict[str, tuple[int, ...]] = {}
        if len(active) > 1:
            while len(self.board) < 5:
                self.board.append(self.deck.deal())
            self.street = Street.SHOWDOWN
            effect.on_showdown(self)
            ranks = {player.player_id: effect.hand_rank(self, player) for player in active}
        order = [self.players[(self.dealer_index + offset) % len(self.players)].player_id for offset in range(1, len(self.players) + 1)]
        self.went_to_showdown = len(active) > 1
        pot_winners: list[list[str]] = []
        winners: list[str] = [active[0].player_id] if active else []
        for pot in self.pots:
            # A pot nobody is eligible for (all contributors folded) goes to the previous pot's winners.
            if pot.eligible:
                if ranks:
                    winners = effect.compare_hands({player_id: ranks[player_id] for player_id in pot.eligible})
                else:
                    winners = list(pot.eligible)
            pot_winners.append(winners)
        self.main_pot_winners = pot_winners[0] if pot_winners else []
        amounts = [pot.amount for pot in self.pots]
        if amounts:
            amounts[0] += self.hand_carryover  # fines and refusals from the previous hand
            self.hand_carryover = 0
        if not self._resolve_side_bets(self.main_pot_winners, order) and self.side_bet_pool and amounts:
            amounts[0] += self.side_bet_pool  # nobody guessed: the side pool joins the main pot
        self.winnings = {}
        for amount_to_split, winners in zip(amounts, pot_winners):
            for player_id, amount in split_pot(amount_to_split, winners, order).items():
                by_id[player_id].stack += amount
                self.winnings[player_id] = self.winnings.get(player_id, 0) + amount
        self.street = Street.COMPLETE
        self.current_index = None
        self.settled = True
        return dict(self.winnings)
