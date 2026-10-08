from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.tasks.photo_tasks import REACTIONS

if TYPE_CHECKING:
    from app.models import Room

NO_EVENT = {"id": "none", "title": "Нет события", "description": "Событие пока не выбрано"}


def _player_status(room: Room, player_id: str) -> dict[str, Any]:
    engine = room.engine
    if engine is None:
        return {"status": "waiting", "committed": 0, "street_commitment": 0}
    engine_player = next((item for item in engine.players if item.player_id == player_id), None)
    if engine_player is None:
        return {"status": "waiting", "committed": 0, "street_commitment": 0}
    if engine_player.out:
        status = "out"
    elif engine_player.folded:
        status = "folded"
    elif engine_player.all_in:
        status = "all_in"
    else:
        status = "active"
    return {
        "status": status,
        "committed": engine_player.committed,
        "street_commitment": engine_player.street_commitment,
    }


def _task_view(room: Room, task: dict[str, Any], role: str, player_id: str | None) -> dict[str, Any]:
    view = dict(task)
    submission = view.pop("submission", None)
    votes = view.pop("votes", {})
    if task["status"] == "voting":
        voters = room.task_voters(task)
        view["vote"] = {
            "yes": sum(1 for vote in votes.values() if vote),
            "no": sum(1 for vote in votes.values() if not vote),
            "needed": len(voters) // 2 + 1,
            "total": len(voters),
            "your_vote": votes.get(player_id) if player_id else None,
            "can_vote": role == "player" and player_id in voters and player_id not in votes,
        }
    # A photo under review is visible only to its author and the dealer (section 10).
    if submission is not None and (
        role == "dealer" or task.get("player_id") == player_id or task.get("status") == "approved"
    ):
        view["submission"] = submission
    return view


def _seconds_left(deadline: float | None) -> int | None:
    if deadline is None:
        return None
    return max(0, round(deadline - time.monotonic()))


def _event_view(room: Room) -> dict[str, Any]:
    if room.current_event is None:
        return NO_EVENT
    view = dict(room.current_event)
    engine = room.engine
    if engine is None or not engine.hand_started:
        return view
    state = engine.event_state
    # Only public results: the spy card is open to everybody by the rules of the event.
    if "spy" in state:
        view["spy"] = state["spy"]
    if "wealth_tax" in state:
        view["wealth_tax"] = state["wealth_tax"]
    joker = engine.joker_card()
    if joker is not None:
        view["joker"] = {"index": engine.joker_index, "card": str(joker)}
    if engine.pending_choice == "carousel":
        view["carousel_waiting"] = engine.carousel_waiting()
    return view


def _game_view(room: Room) -> dict[str, Any] | None:
    engine = room.engine
    if engine is None:
        return None
    betting = room.phase == "playing"
    current = engine.players[engine.current_index] if engine.current_index is not None and betting else None
    return {
        "hand_number": engine.hands_played + 1,
        "street": engine.street.value,
        # Hidden cards (blind flop) are sent as null, never as their value.
        "board": engine.visible_board(),
        "current_index": engine.current_index if betting else None,
        "current_player_id": current.player_id if current else None,
        "current_bet": engine.current_bet,
        "dealer_player_id": engine.players[engine.dealer_index].player_id,
        "small_blind": engine.small_blind,
        "big_blind": engine.big_blind,
        "pots": [{"amount": pot.amount, "eligible": list(pot.eligible)} for pot in engine.pots],
        "pot_total": sum(player.committed for player in engine.players) + engine.hand_carryover,
        "next_hand_pot": engine.carryover,
        "decree": engine.decree,
        "turn_seconds_left": _seconds_left(room.turn_deadline_at) if betting else None,
        "side_bets": {
            "enabled": engine.side_bets_enabled,
            "open": engine.side_bets_open and room.phase in {"playing", "carousel"},
            "stake": engine.big_blind,
            "pool": engine.side_bet_pool,
            "offers": [{**offer, "text": room.side_bet_text(offer)} for offer in engine.side_bet_offers],
            # Side bets are placed openly at the table.
            "bets": dict(engine.side_bets),
        },
    }


def state_for_connection(room: Room, role: str, player_id: str | None = None) -> dict[str, Any]:
    """Return only the state allowed for a connection's authenticated role.

    Public data goes to everybody; hole cards, abilities and private results
    only to their owner. Hole cards are never sent to host or dealer.
    """
    state: dict[str, Any] = {
        "room_id": room.room_id,
        "host_name": room.host_name,
        "role": role,
        "dealer_connected": room.dealer_connected,
        "game_started": room.game_started,
        "game_over": room.game_over,
        "phase": room.phase,
        "phase_seconds_left": _seconds_left(room.phase_deadline_at),
        "paused": room.paused,
        "settings": room.settings.model_dump(),
        "dealer": {
            "mode": room.settings.dealer_mode,
            "connected": room.dealer_connected,
            "status": room.dealer_status,
            "paused": room.paused,
        },
        "players": [
            {
                "id": player.id,
                "name": player.name,
                "avatar": player.avatar,
                "connected": player.connected,
                "stack": player.stack,
                "ability_count": len(player.abilities),
                **_player_status(room, player.id),
            }
            for player in room.players
        ],
        "tasks": [_task_view(room, task, role, player_id) for task in room.task_history],
        "decree": room.active_decree,
        # Accepted photos are public; photos under review appear only in their task's submission.
        "gallery": room.gallery(player_id),
        "showcase": room.showcase_view(),
        "reactions": list(REACTIONS),
        "album": room.album_entries(),
        "dealer_comment": room.last_comment,
        "activity": room.activity_view(role, player_id),
        "event": _event_view(room),
        "feed": list(room.feed[-20:]),
        "last_hand": room.last_hand,
        "results": room.results,
    }
    if room.active_task is not None:
        state["active_task"] = _task_view(room, room.active_task, role, player_id)
    if role == "host":
        # AI judge indicator and token usage for the lobby (section 15.4).
        state["ai"] = room.ai_status
    if role == "dealer":
        # The queued event is a surprise for everybody else.
        state["next_event"] = room.pending_event
        state["decree_cooldown"] = room.decree_cooldown()
    game = _game_view(room)
    if game is not None:
        state["game"] = game

    if role == "player" and player_id is not None:
        player = next((item for item in room.players if item.id == player_id), None)
        if player is not None:
            you: dict[str, Any] = {
                "id": player.id,
                "name": player.name,
                "hole_cards": list(player.hole_cards),
                "abilities": list(player.abilities),
                "used_abilities": list(player.used_abilities),
            }
            engine = room.engine
            if engine is not None and game is not None and game["current_player_id"] == player_id and not room.paused:
                you["legal_actions"] = engine.legal_actions()
            if engine is not None and engine.pending_choice == "carousel":
                you["carousel_pick"] = engine.carousel_picks.get(player_id)
            state["you"] = you
    return state
