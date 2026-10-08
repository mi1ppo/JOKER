"""AI dealer side of the room (section 8.2). Mixed into Room.

The room stays synchronous and network-free: it only queues AI requests (`ai_requests`);
the server runs them in background tasks and feeds the answers back through the apply_* methods.
If an answer never comes, the game simply goes on with the fallback behaviour.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from app.game.events import event_catalog, event_due

if TYPE_CHECKING:
    from app.models import Room

BRIBE_WISHES = {"hint", "change_event"}
SUIT_NAMES = {"s": "пики ♠", "h": "червы ♥", "d": "бубны ♦", "c": "трефы ♣"}


class AiDealerMixin:
    @property
    def ai_mode(self: Room) -> bool:
        return self.settings.dealer_mode == "ai"

    def request_ai(self: Room, kind: str, **params: Any) -> None:
        self.ai_requests.append({"kind": kind, **params})

    def pop_ai_requests(self: Room) -> list[dict[str, Any]]:
        requests, self.ai_requests = self.ai_requests, []
        return requests

    # ---- hooks called by the game flow ------------------------------------------------------

    def _ai_after_hand(self: Room, hand: dict[str, Any]) -> None:
        if not self.ai_mode:
            return
        self.request_ai("comment", hand=hand)
        next_hand = self.hand_number + 1
        if self.pending_event is None and event_due(self.settings.chaos_level, next_hand):
            # Chosen during the pause between hands; if the answer is late the event is random.
            self.request_ai("event", for_hand=next_hand)

    # ---- answers from the AI --------------------------------------------------------------------

    def add_dealer_comment(self: Room, text: str, source: str = "ai") -> None:
        text = text.strip()
        if not text:
            return
        self.last_comment = {"text": text, "source": source}
        self.announce(f"🎙 {text}")
        self.notify("dealer_comment", {"text": text})

    def apply_ai_event(self: Room, event_id: str, explanation: str | None, for_hand: int) -> bool:
        """Queue the AI's event for `for_hand` if that hand has not been dealt yet."""
        catalog = event_catalog()
        if event_id not in catalog or self.pending_event is not None or self.game_over:
            return False
        if self.hand_number + 1 != for_hand or self.phase not in {"between_hands"}:
            return False
        event = dict(catalog[event_id])
        if explanation:
            event["ai_explanation"] = explanation[:300]
        self.pending_event = event
        return True

    def set_protocol(self: Room, protocol: dict[str, Any], source: str) -> None:
        if self.results is not None:
            self.results["protocol"] = {**protocol, "source": source}
            self.announce("📜 Судья огласил итоговый протокол вечера")

    # ---- bribes -------------------------------------------------------------------------------------

    def start_bribe(self: Room, player_id: str, amount: int, wish: str) -> dict[str, Any]:
        """A player offers N chips to the AI judge. The chips go to the next pot whatever the answer."""
        if not self.ai_mode:
            raise ValueError("Bribes are possible only with the AI dealer")
        if wish not in BRIBE_WISHES:
            raise ValueError("Unknown bribe wish")
        if self.engine is None or self.game_over:
            raise ValueError("Game is not running")
        player = self.get_player(player_id)
        if self.bribe_hands.get(player_id) == self.hand_number:
            raise ValueError("One bribe per hand")
        big_blind = self.big_blind()
        if amount < big_blind:
            raise ValueError(f"A bribe starts at {big_blind}")
        if amount > player.stack:
            raise ValueError("Not enough chips")
        paid = self._charge(player_id, amount)
        self.bribe_hands[player_id] = self.hand_number
        bribe = {"id": f"bribe_{len(self.bribes) + 1}", "player_id": player_id, "amount": paid, "wish": wish,
                 "hand_number": self.hand_number, "status": "pending"}
        self.bribes.append(bribe)
        self.announce(f"💰 {player.name} предлагает судье {paid} фишек…")
        self.request_ai("bribe", bribe_id=bribe["id"])
        return bribe

    def get_bribe(self: Room, bribe_id: str) -> dict[str, Any]:
        bribe = next((item for item in self.bribes if item["id"] == bribe_id), None)
        if bribe is None:
            raise ValueError("Unknown bribe")
        return bribe

    def resolve_bribe(self: Room, bribe_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        bribe = self.get_bribe(bribe_id)
        if bribe["status"] != "pending":
            return bribe
        accepted = bool(decision.get("accepted"))
        bribe["status"] = "accepted" if accepted else "rejected"
        bribe["comment"] = str(decision.get("comment") or "")[:300]
        name = self.get_player(bribe["player_id"]).name
        verdict = "принимает" if accepted else "отклоняет"
        self.announce(f"Взятка ({name}, {bribe['amount']}): судья {verdict}. {bribe['comment']}".strip())
        result: dict[str, Any] = {"bribe_id": bribe_id, "accepted": accepted, "comment": bribe["comment"]}
        if accepted and bribe["wish"] == "hint":
            result["hint"] = self._bribe_hint(bribe["player_id"])
        elif accepted and bribe["wish"] == "change_event":
            current = self.pending_event["id"] if self.pending_event else None
            options = [event_id for event_id in event_catalog() if event_id != current]
            self.pending_event = dict(event_catalog()[random.choice(options)], bribed_by=bribe["player_id"])
            result["event"] = self.pending_event["title"]
            self.announce("Судья обещает сюрприз в следующей раздаче…")
        self.notify_to(bribe["player_id"], "private_notice", {"bribe": result})
        return bribe

    def _bribe_hint(self: Room, player_id: str) -> str:
        """Computed by the server (the AI never sees cards): the suit of one card of a random opponent."""
        engine = self.engine
        if engine is None or not engine.hand_started or engine.hand_over:
            return "Судья подмигивает: следующая раздача будет интересной."
        opponents = [player for player in engine.players
                     if player.player_id != player_id and not player.folded and player.hole_cards]
        if not opponents:
            return "Судья подмигивает: соперников в раздаче не осталось."
        target = random.choice(opponents)
        card = random.choice(target.hole_cards)
        return f"У игрока {self.get_player(target.player_id).name} есть карта масти {SUIT_NAMES[card.suit]}."
