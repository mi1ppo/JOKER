from __future__ import annotations

import random
import secrets
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.game import sidebets
from app.game.abilities import GRANT_EVERY_HANDS, STARTING_COUNT, ability_lookup, draw_ability, starting_abilities
from app.game.engine import EnginePlayer, HoldemEngine
from app.game.evaluator import best_five
from app.game.events import CAROUSEL_WINDOW_SEC, VETO_WINDOW_SEC, event_catalog, event_due, random_event_id
from app.dealer.base import AiDealerMixin
from app.dealer.human import DecreeMixin
from app.stats import StatsMixin
from app.tasks.penalties import TaskMixin
from app.tasks.physical import ActivityMixin
from app.tasks.photo_tasks import PhotoMixin

# Pause between hands so everybody can see the showdown.
NEXT_HAND_DELAY_SEC = 6.0
FEED_LIMIT = 50

# Room phases: lobby -> [event_reveal] -> [carousel] -> playing -> between_hands -> ... -> game_over
PHASE_LOBBY = "lobby"
PHASE_EVENT_REVEAL = "event_reveal"
PHASE_CAROUSEL = "carousel"
PHASE_PLAYING = "playing"
PHASE_BETWEEN_HANDS = "between_hands"
PHASE_GAME_OVER = "game_over"


def new_token() -> str:
    return secrets.token_urlsafe(16)


def tokens_equal(known: str, given: str) -> bool:
    """Constant-time comparison that is safe for arbitrary user input."""
    return bool(known) and secrets.compare_digest(known.encode(), given.encode())


class LobbySettings(BaseModel):
    starting_stack: int = Field(default=1000, ge=1)
    blind_interval_hands: int = Field(default=8, ge=1)
    turn_timer_sec: int = Field(default=45, ge=5)
    chaos_level: str = Field(default="medium", pattern="^(light|medium|full)$")
    dealer_mode: str = "human"
    task_spiciness: str = "medium"
    physical_activity: str = "off"
    four_color_deck: bool = False
    allow_join_during_game: bool = False
    hand_limit: int | None = Field(default=None, ge=1)
    # Penalty task for the biggest loser of a pot of at least N big blinds (section 9.1).
    penalty_after_big_pot: bool = True
    penalty_threshold_bb: int = Field(default=10, ge=1)
    # The human dealer may issue a decree at most once per N hands (section 8.1).
    decree_interval_hands: int = Field(default=5, ge=1)

class Player(BaseModel):
    id: str
    name: str
    avatar: str = "🃏"
    agreed_to_terms: bool = False
    connected: bool = False
    session_token: str = ""
    # Mirrors of the engine state; the engine is the source of truth during a game.
    stack: int = 1000
    hole_cards: list[str | None] = Field(default_factory=list)
    abilities: list[str] = Field(default_factory=list)
    # Used one-shot abilities stay visible (greyed out) on the owner's screen.
    used_abilities: list[str] = Field(default_factory=list)

class Room(TaskMixin, PhotoMixin, ActivityMixin, DecreeMixin, AiDealerMixin, StatsMixin, BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    room_id: str
    host_name: str
    host_token: str
    dealer_token: str
    dealer_session_token: str = ""
    player_token: str
    players: list[Player] = Field(default_factory=list)
    settings: LobbySettings = Field(default_factory=LobbySettings)
    dealer_connected: bool = False
    game_started: bool = False
    game_over: bool = False
    engine: HoldemEngine | None = Field(default=None, exclude=True)
    turn_deadline_at: float | None = Field(default=None, exclude=True)
    next_hand_at: float | None = Field(default=None, exclude=True)
    phase: str = PHASE_LOBBY
    # End of the veto window / carousel choice window (monotonic time).
    phase_deadline_at: float | None = Field(default=None, exclude=True)
    paused: bool = False
    active_task: dict[str, Any] | None = None
    task_history: list[dict[str, Any]] = Field(default_factory=list)
    dealer_status: str = "idle"
    activity_mode: str = "off"
    active_activity: dict[str, Any] | None = None
    activity_history: list[dict[str, Any]] = Field(default_factory=list)
    activity_deadline_at: float | None = Field(default=None, exclude=True)
    # Event of the current hand (public) and the one the dealer queued for the next hand (dealer only).
    current_event: dict[str, Any] | None = None
    pending_event: dict[str, Any] | None = None
    # Public one-off notifications for client animations, drained by the server after each change.
    notices: list[tuple[str, dict[str, Any]]] = Field(default_factory=list, exclude=True)
    feed: list[str] = Field(default_factory=list)
    last_hand: dict[str, Any] | None = None
    results: dict[str, Any] | None = None
    # player_id -> (hand number, street) of the last ability use; one ability per betting round.
    ability_rounds: dict[str, tuple[int, str]] = Field(default_factory=dict, exclude=True)
    active_decree: dict[str, Any] | None = None
    # Uploaded photos (metadata only; files are in media_tmp/<room_id>/), the 8-second showcase on host.
    photos: dict[str, dict[str, Any]] = Field(default_factory=dict)
    showcase: dict[str, Any] | None = None
    showcase_until: float | None = Field(default=None, exclude=True)
    last_upload_at: dict[str, float] = Field(default_factory=dict, exclude=True)
    # Results of finished hands (public facts for the AI context) and per-player statistics.
    hand_log: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # AI dealer: queued requests for the server, last comment, bribes, status for the host lobby.
    ai_requests: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    last_comment: dict[str, Any] | None = None
    bribes: list[dict[str, Any]] = Field(default_factory=list)
    bribe_hands: dict[str, int] = Field(default_factory=dict, exclude=True)
    ai_status: dict[str, Any] = Field(default_factory=dict)
    # Notifications for one audience only: ("dealer" | "host" | player_id, type, payload).
    direct_notices: list[tuple[str, str, dict[str, Any]]] = Field(default_factory=list, exclude=True)
    last_decree_hand: int | None = None

    # ---- lookup helpers -------------------------------------------------

    def get_player(self, player_id: str) -> Player:
        player = next((item for item in self.players if item.id == player_id), None)
        if player is None:
            raise ValueError(f"Unknown player: {player_id}")
        return player

    def player_by_session(self, token: str) -> Player | None:
        return next((item for item in self.players if tokens_equal(item.session_token, token)), None)

    def announce(self, text: str) -> None:
        self.feed.append(text)
        del self.feed[:-FEED_LIMIT]

    def notify(self, message_type: str, payload: dict[str, Any]) -> None:
        self.notices.append((message_type, payload))

    def pop_notices(self) -> list[tuple[str, dict[str, Any]]]:
        notices, self.notices = self.notices, []
        return notices

    def notify_to(self, audience: str, message_type: str, payload: dict[str, Any]) -> None:
        self.direct_notices.append((audience, message_type, payload))

    def pop_direct_notices(self) -> list[tuple[str, str, dict[str, Any]]]:
        notices, self.direct_notices = self.direct_notices, []
        return notices

    # ---- lobby ------------------------------------------------------------

    def add_player(self, *, name: str, avatar: str = "🃏", agreed_to_terms: bool = True) -> Player:
        if not agreed_to_terms:
            raise ValueError("Player consent is required")
        if len(self.players) >= 8:
            raise ValueError("Room is full")
        if self.game_started and not self.settings.allow_join_during_game:
            raise ValueError("The game has already started")
        player = Player(
            id=f"player_{len(self.players) + 1}",
            name=name,
            avatar=avatar,
            agreed_to_terms=agreed_to_terms,
            session_token=new_token(),
            stack=self.settings.starting_stack,
        )
        self.players.append(player)
        if self.engine is not None:
            # Joins as folded for the current hand and is dealt in from the next one.
            self.engine.players.append(EnginePlayer(player.id, player.stack, folded=True, out=True))
        self.announce(f"{player.name} в игре")
        return player

    def connect_dealer(self) -> None:
        if self.dealer_connected:
            raise ValueError("Dealer token has already been used")
        self.dealer_connected = True
        self.dealer_session_token = new_token()
        self.dealer_token = ""

    def set_connected(self, player_id: str | None, connected: bool) -> None:
        if player_id is None:
            return
        player = next((item for item in self.players if item.id == player_id), None)
        if player is not None:
            player.connected = connected

    def update_settings(self, settings: LobbySettings) -> None:
        if self.game_started:
            raise ValueError("Cannot change lobby settings after the game starts")
        self.settings = settings
        for player in self.players:
            player.stack = settings.starting_stack

    def set_dealer_mode(self, mode: str) -> None:
        valid_modes = {"human", "ai", "none"}
        if mode not in valid_modes:
            raise ValueError(f"Unknown dealer mode: {mode}")
        self.settings.dealer_mode = mode
        self.dealer_status = "active" if self.game_started else "idle"

    def pause_game(self) -> None:
        self.paused = True
        self.dealer_status = "paused"
        self.announce("Игра на паузе")

    def resume_game(self) -> None:
        self.paused = False
        self.dealer_status = "active" if self.game_started else "idle"
        # TODO: уточнить — сейчас после паузы таймеры (ход, окно вето, карусель) начинаются заново.
        self.refresh_turn_deadline()
        if self.next_hand_at is not None:
            self.next_hand_at = time.monotonic() + NEXT_HAND_DELAY_SEC
        if self.phase == PHASE_EVENT_REVEAL:
            self.phase_deadline_at = time.monotonic() + VETO_WINDOW_SEC
        elif self.phase == PHASE_CAROUSEL:
            self.phase_deadline_at = time.monotonic() + CAROUSEL_WINDOW_SEC
        self.announce("Игра продолжается")

    # ---- activities, events, AI ---------------------------------------------

    def set_activity_mode(self, mode: str) -> None:
        valid_modes = {"off", "light", "medium", "hard"}
        if mode not in valid_modes:
            raise ValueError(f"Unknown activity mode: {mode}")
        self.activity_mode = mode
        self.settings.physical_activity = mode

    def set_physical_activity(self, mode: str) -> None:
        self.set_activity_mode(mode)

    def select_event(self, event_id: str) -> dict[str, Any]:
        """Queue an event (from the catalog only) for the start of the next hand."""
        catalog = event_catalog()
        if event_id not in catalog:
            raise ValueError(f"Unknown event: {event_id}")
        self.pending_event = catalog[event_id]
        return self.pending_event

    # ---- game flow ----------------------------------------------------------

    def _sync_engine_state(self) -> None:
        if self.engine is None:
            return
        for engine_player in self.engine.players:
            room_player = next((item for item in self.players if item.id == engine_player.player_id), None)
            if room_player is None:
                continue
            room_player.stack = engine_player.stack
            room_player.hole_cards = [str(card) for card in engine_player.hole_cards]

    def start_game(self) -> None:
        if len(self.players) < 2:
            raise ValueError("At least 2 players are required")
        if self.game_started:
            raise ValueError("Game has already started")
        if self.abilities_enabled:
            for player in self.players:
                if not player.abilities:
                    player.abilities = starting_abilities(STARTING_COUNT)
        self.engine = HoldemEngine(
            [EnginePlayer(player.id, player.stack) for player in self.players],
            dealer_index=0,
            blind_level=0,
            blind_interval_hands=self.settings.blind_interval_hands,
        )
        self.engine.side_bets_enabled = self.abilities_enabled
        self.game_started = True
        self.dealer_status = "active"
        if not self.settings.allow_join_during_game:
            self.player_token = ""
        self.announce("Игра началась!")
        self._begin_hand()

    @property
    def hand_number(self) -> int:
        return self.engine.hands_played + 1 if self.engine is not None else 0

    def start_next_hand(self) -> None:
        if self.engine is None or self.game_over:
            raise ValueError("Game is not running")
        self.engine.prepare_next_hand()
        self.next_hand_at = None
        self.ability_rounds.clear()
        self._sync_engine_state()
        self._begin_hand()

    def _choose_event(self) -> dict[str, Any] | None:
        if self.pending_event is not None:
            event, self.pending_event = self.pending_event, None
            return event
        if not event_due(self.settings.chaos_level, self.hand_number):
            return None
        # TODO: уточнить — в режиме ai событие выбирает ИИ из каталога (этап 9); пока случайное.
        return event_catalog()[random_event_id(self.engine.rng if self.engine else None)]

    def _begin_hand(self) -> None:
        """EVENT_REVEAL phase: show the event and open the veto window, or deal right away."""
        self.current_event = self._choose_event()
        if self.current_event is None:
            self._deal_hand()
            return
        self.phase = PHASE_EVENT_REVEAL
        self.phase_deadline_at = time.monotonic() + VETO_WINDOW_SEC
        self.turn_deadline_at = None
        self.announce(f"Событие раздачи №{self.hand_number}: {self.current_event['title']}")
        self.notify("event_revealed", {
            "event_id": self.current_event["id"],
            "title": self.current_event["title"],
            "description": self.current_event["description"],
            "icon": self.current_event.get("icon"),
            "veto_window_sec": VETO_WINDOW_SEC,
            "explanation": self.current_event.get("ai_explanation"),
        })

    def _deal_hand(self) -> None:
        engine = self.engine
        assert engine is not None
        event_id = self.current_event["id"] if self.current_event else None
        engine.start_hand(event_id)
        self.phase_deadline_at = None
        self.announce(f"Раздача №{self.hand_number}")
        self._announce_event_effects()
        if engine.pending_choice == "carousel":
            self.phase = PHASE_CAROUSEL
            self.phase_deadline_at = time.monotonic() + CAROUSEL_WINDOW_SEC
            self.turn_deadline_at = None
            self._sync_engine_state()
            return
        self.phase = PHASE_PLAYING
        self._after_engine_change()

    def _announce_event_effects(self) -> None:
        state = self.engine.event_state if self.engine else {}
        names = {player.id: player.name for player in self.players}
        tax = state.get("wealth_tax")
        if tax and tax.get("amount"):
            self.announce(f"Налог: {names[tax['from_id']]} отдаёт {tax['amount']} игроку {names[tax['to_id']]}")
        spy = state.get("spy")
        if spy:
            self.announce(f"Шпион: у {names[spy['player_id']]} есть {spy['card']}")

    def pick_carousel(self, player_id: str, card_index: int) -> None:
        if self.phase != PHASE_CAROUSEL or self.engine is None:
            raise ValueError("No carousel in progress")
        self.engine.pick_carousel(player_id, card_index)
        if not self.engine.carousel_waiting():
            self._finish_carousel()

    def _finish_carousel(self) -> None:
        assert self.engine is not None
        self.engine.resolve_carousel()
        self.phase = PHASE_PLAYING
        self.phase_deadline_at = None
        self.announce("Карусель: карты переданы игрокам слева")
        self.notify("carousel_done", {})
        self._after_engine_change()

    def end_game(self) -> None:
        if self.game_over:
            return
        engine = self.engine
        if engine is not None:
            if not engine.hand_over:
                # TODO: уточнить — незаконченная раздача: фишки на столе и побочные ставки возвращаются игрокам.
                for engine_player in engine.players:
                    engine_player.stack += engine_player.committed
                    engine_player.committed = 0
                for player_id in engine.side_bets:
                    engine.player(player_id).stack += engine.big_blind
                engine.side_bets, engine.side_bet_pool = {}, 0
            # Fines waiting for the next hand go to the chip leader.
            leftover = engine.carryover + (engine.hand_carryover if not engine.settled else 0)
            if leftover:
                max(engine.players, key=lambda item: item.stack).stack += leftover
                engine.carryover = engine.hand_carryover = 0
            self._sync_engine_state()
        self.game_over = True
        self.phase = PHASE_GAME_OVER
        self.phase_deadline_at = None
        self.turn_deadline_at = None
        self.next_hand_at = None
        standings = sorted(self.players, key=lambda item: item.stack, reverse=True)
        self.results = {
            "hands_played": (self.engine.hands_played + 1) if self.engine else 0,
            "standings": [
                {"place": place, "id": player.id, "name": player.name, "stack": player.stack}
                for place, player in enumerate(standings, start=1)
            ],
            "stats": self.results_stats(),
            "protocol": None,
        }
        self.announce(f"Игра окончена. Победитель: {standings[0].name}" if standings else "Игра окончена")
        if self.ai_mode:
            self.request_ai("protocol")

    def _finish_hand(self) -> None:
        engine = self.engine
        assert engine is not None
        winnings = engine.settle_showdown()
        names = {player.id: player.name for player in self.players}
        revealed = {}
        if engine.went_to_showdown:
            revealed = {
                player.player_id: [str(card) for card in player.hole_cards]
                for player in engine.players
                if not player.folded
            }
        self.last_hand = {
            "hand_number": engine.hands_played + 1,
            # Blind flop stays face down when the hand ended without a showdown.
            "board": engine.visible_board(),
            "event": self.current_event,
            "joker_card": str(engine.joker_card()) if engine.joker_card() else None,
            "winnings": winnings,
            "showdown": engine.went_to_showdown,
            "revealed": revealed,
            # Cards of the winning combination(s), highlighted on the showdown screen (11.4).
            "best_cards": {
                player_id: [str(card) for card in best_five(engine.player(player_id).hole_cards + engine.board)]
                for player_id in engine.main_pot_winners
            } if engine.went_to_showdown else {},
        }
        self.last_hand["side_bets"] = self._side_bet_summary()
        self._record_hand_stats(winnings)
        self.hand_log.append({
            "hand_number": self.last_hand["hand_number"],
            "winnings": dict(winnings),
            "showdown": engine.went_to_showdown,
            "event": self.current_event["title"] if self.current_event else None,
        })
        del self.hand_log[:-20]
        self.announce(
            ", ".join(f"{names.get(player_id, player_id)} забирает {amount}" for player_id, amount in winnings.items())
        )
        self._announce_side_bets(names)
        busted = [player.player_id for player in engine.players if player.stack == 0 and not player.out]
        # The knockout reward goes to whoever took the biggest part of the pot.
        knocker = max((player_id for player_id in winnings if player_id not in busted),
                      key=lambda player_id: winnings[player_id], default=None)
        for player_id in busted:
            self.announce(f"{names.get(player_id)} выбывает")
            if knocker is not None:
                self._grant_ability(knocker, f"за выбивание игрока {names.get(player_id)}")
        self._maybe_auto_penalty(
            {player.player_id: player.committed for player in engine.players if player.committed},
            winnings, engine.big_blind,
        )
        self._ai_after_hand(self.last_hand)
        finished_hands = engine.hands_played + 1
        if finished_hands % GRANT_EVERY_HANDS == 0:
            for engine_player in engine.players:
                if engine_player.stack > 0:
                    self._grant_ability(engine_player.player_id, f"за {finished_hands} раздач")
        self.turn_deadline_at = None
        self._sync_engine_state()
        with_chips = [player for player in engine.players if player.stack > 0]
        hand_limit = self.settings.hand_limit
        if len(with_chips) <= 1 or (hand_limit is not None and engine.hands_played + 1 >= hand_limit):
            self.end_game()
        else:
            self.phase = PHASE_BETWEEN_HANDS
            self.next_hand_at = time.monotonic() + NEXT_HAND_DELAY_SEC

    @property
    def abilities_enabled(self) -> bool:
        # Abilities and side bets are off on the lowest chaos level (sections 7 and 12).
        return self.settings.chaos_level != "light"

    def _grant_ability(self, player_id: str, reason: str) -> None:
        if not self.abilities_enabled:
            return
        player = self.get_player(player_id)
        ability_id = draw_ability(player.abilities, self.engine.rng if self.engine else None)
        if ability_id is None:
            return  # already holds the maximum
        player.abilities.append(ability_id)
        self.announce(f"{player.name} получает способность {reason}")
        self.notify("ability_granted", {"player_id": player_id})

    def place_side_bet(self, player_id: str, bet_id: str) -> None:
        if self.engine is None or self.phase not in {PHASE_PLAYING, PHASE_CAROUSEL}:
            raise ValueError("Side bets are closed")
        if self.paused:
            raise ValueError("Game is paused")
        amount = self.engine.place_side_bet(player_id, bet_id)
        offer = next(item for item in self.engine.side_bet_offers if item["id"] == bet_id)
        self.announce(f"{self.get_player(player_id).name} ставит {amount} на «{self.side_bet_text(offer)}»")
        self._sync_engine_state()

    def side_bet_text(self, offer: dict[str, Any]) -> str:
        texts = {
            sidebets.PAIR_ON_BOARD: "Среди общих карт будет пара",
            sidebets.FLOP_SUITED: "Флоп будет одной масти",
            sidebets.SHOWDOWN: "Дойдём до шоудауна",
        }
        if offer["kind"] == sidebets.PLAYER_WINS:
            player = next((item for item in self.players if item.id == offer["player_id"]), None)
            return f"Победит {player.name if player else offer['player_id']}"
        return texts[offer["kind"]]

    def _side_bet_summary(self) -> dict[str, Any] | None:
        engine = self.engine
        if engine is None or not engine.side_bets:
            return None
        return {**engine.side_bet_results, "bets": dict(engine.side_bets)}

    def _announce_side_bets(self, names: dict[str, str]) -> None:
        results = self.engine.side_bet_results if self.engine else {}
        if not results or not results.get("pool"):
            return
        if results["winner_ids"]:
            paid = ", ".join(f"{names[player_id]} +{amount}" for player_id, amount in results["payouts"].items())
            self.announce(f"Побочные ставки: {paid}")
        else:
            self.announce(f"Побочные ставки: никто не угадал, {results['pool']} ушло в банк")

    def _after_engine_change(self) -> None:
        self._sync_engine_state()
        if self.engine is not None and self.engine.hand_over and not self.engine.settled:
            self._finish_hand()
        else:
            self.refresh_turn_deadline()

    def refresh_turn_deadline(self) -> None:
        if self.engine is None or self.engine.current_index is None or self.game_over:
            self.turn_deadline_at = None
            return
        self.turn_deadline_at = time.monotonic() + self.settings.turn_timer_sec

    def trigger_turn_timeout(self) -> None:
        if self.engine is None or self.engine.current_index is None:
            return
        current_player = self.engine.players[self.engine.current_index]
        legal = self.engine.legal_actions()
        action = "check" if legal.get("check") else "fold"
        self.apply_player_action(current_player.player_id, action)

    def process_timers(self, now: float | None = None) -> bool:
        """Run server-side timers; returns True when the state changed."""
        now = time.monotonic() if now is None else now
        if self.process_activity_timer(now):
            return True
        if self.showcase_until is not None and now >= self.showcase_until:
            # The accepted photo was shown on host for its 8 seconds.
            self.showcase = None
            self.showcase_until = None
            return True
        if self.paused or self.game_over or self.engine is None:
            return False
        if self.phase == PHASE_EVENT_REVEAL:
            if self.phase_deadline_at is not None and now >= self.phase_deadline_at:
                self._deal_hand()
                return True
            return False
        if self.phase == PHASE_CAROUSEL:
            if self.phase_deadline_at is not None and now >= self.phase_deadline_at:
                self._finish_carousel()
                return True
            return False
        if self.turn_deadline_at is not None and now >= self.turn_deadline_at:
            self.trigger_turn_timeout()
            return True
        if self.next_hand_at is not None and now >= self.next_hand_at:
            self.start_next_hand()
            return True
        return False

    def apply_player_action(self, player_id: str, kind: str, amount: int | None = None) -> None:
        if self.engine is None or self.game_over:
            raise ValueError("Game is not running")
        if self.paused:
            raise ValueError("Game is paused")
        if self.phase != PHASE_PLAYING:
            raise ValueError("Betting is not open")
        self.engine.act(player_id, kind, amount)
        self._after_engine_change()

    # ---- abilities ------------------------------------------------------------

    def use_ability(
        self,
        player_id: str,
        ability_id: str,
        *,
        target_id: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Apply a one-shot ability. The returned dict may contain private data for the owner only."""
        player = self.get_player(player_id)
        if ability_id not in player.abilities:
            raise ValueError(f"Player does not have ability: {ability_id}")
        if ability_id == "veto":
            return self._use_veto(player)
        engine = self.engine
        if engine is None or engine.hand_over or self.phase != PHASE_PLAYING:
            raise ValueError("Abilities can only be used during a hand")
        owner = engine.player(player_id)
        if owner.folded:
            raise ValueError("Folded players cannot use abilities")
        round_key = (engine.hands_played, engine.street.value)
        if self.ability_rounds.get(player_id) == round_key:
            raise ValueError("Only one ability per betting round")
        target = None
        if ability_id in {"peek", "freeze", "steal"}:
            if target_id is None:
                raise ValueError(f"{ability_id} requires a target player")
            if target_id == player_id:
                raise ValueError("Cannot target yourself")
            target = engine.player(target_id)
            if target.folded or target.out:
                raise ValueError("Target is not in this hand")

        result: dict[str, Any] = {"ability": ability_id, "target_id": target_id}
        if ability_id == "peek":
            if not target.hole_cards:
                raise ValueError("Target has no cards to reveal")
            result["revealed_card"] = str(engine.rng.choice(target.hole_cards))
        elif ability_id == "redraw":
            if index is None or not 0 <= index < len(owner.hole_cards):
                raise ValueError("Redraw requires a valid card index")
            if engine.street.value == "river":
                raise ValueError("Redraw is only allowed before the river")
            new_card = engine.deck.deal()
            owner.hole_cards[index] = new_card
            result.update(replaced_index=index, new_card=str(new_card))
        elif ability_id == "freeze":
            engine.frozen_players.add(target_id)
        elif ability_id == "steal":
            if target.all_in:
                raise ValueError("Cannot steal from an all-in player")
            if engine.street.value == "river":
                raise ValueError("Steal is only allowed before the river")
            amount = min(max(1, target.stack // 20), target.stack)
            target.stack -= amount
            owner.stack += amount
            result["stolen_amount"] = amount
        else:
            raise ValueError(f"Unknown ability: {ability_id}")

        player.abilities.remove(ability_id)
        player.used_abilities.append(ability_id)
        self.ability_rounds[player_id] = round_key
        title = ability_lookup(ability_id)["title"]
        self.announce(f"{player.name} использует «{title}» (цель: {self.get_player(target_id).name})"
                      if target_id is not None else f"{player.name} использует «{title}»")
        self._sync_engine_state()
        return result

    def _use_veto(self, player: Player) -> dict[str, Any]:
        """Veto works only inside the veto window after an event is revealed."""
        if self.phase != PHASE_EVENT_REVEAL or self.current_event is None:
            raise ValueError("Veto is only possible in the veto window")
        if player.stack <= 0:
            raise ValueError("Busted players cannot use abilities")
        player.abilities.remove("veto")
        player.used_abilities.append("veto")
        vetoed = self.current_event
        self.current_event = None
        self.announce(f"{player.name} использует «Вето»: событие «{vetoed['title']}» отменено")
        self.notify("event_vetoed", {"event_id": vetoed["id"], "player_id": player.id})
        self._deal_hand()
        return {"ability": "veto", "target_id": None, "vetoed": True, "event_id": vetoed["id"]}

    # ---- views ----------------------------------------------------------------

    def host_view(self) -> dict[str, Any]:
        return self.view_for("host")

    def view_for(self, role: str, player_id: str | None = None) -> dict[str, Any]:
        from app.views import state_for_connection

        return state_for_connection(self, role, player_id)
