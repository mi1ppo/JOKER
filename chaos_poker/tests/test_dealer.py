import time

import pytest
from fastapi.testclient import TestClient

from app.game.cards import Card
from app.main import app, rooms
from app.models import LobbySettings, Room


def make_room(count=3, *, dealer=True, start=True, **settings):
    settings.setdefault("chaos_level", "light")
    room = Room(room_id="dl", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(**settings))
    for index in range(count):
        room.add_player(name=f"P{index + 1}", agreed_to_terms=True)
    if dealer:
        room.connect_dealer()
    if start:
        room.start_game()
        while room.phase not in {"playing", "game_over"}:
            room.process_timers(time.monotonic() + 100)
    return room


def total_chips(room):
    engine = room.engine
    on_table = 0 if engine.settled else sum(player.committed for player in engine.players) + engine.hand_carryover
    return sum(player.stack for player in room.players) + on_table + engine.carryover + engine.side_bet_pool


def current(room):
    return room.engine.players[room.engine.current_index].player_id


# ---- issuing ------------------------------------------------------------------------


def test_custom_bank_and_ai_tasks():
    room = make_room()
    custom = room.issue_task("player_1", source="custom", title="  Спой гимн диллера  ")
    assert custom["title"] == "Спой гимн диллера" and custom["source"] == "custom"
    bank = room.issue_task("player_2", source="bank", difficulty="spicy")
    assert bank["title"] and bank["source"] == "bank"
    ai = room.issue_task("player_3", source="ai")  # falls back to the bank until stage 9
    assert ai["title"]
    with pytest.raises(ValueError, match="needs a text"):
        room.issue_task("player_1", source="custom", title="   ")
    assert len(room.open_tasks()) == 3


def test_random_target_and_task_notice():
    room = make_room()
    task = room.issue_task("random", kind="bonus")
    assert task["player_id"] in {"player_1", "player_2", "player_3"}
    assert task["reward"] == 40
    assigned = [payload for kind, payload in room.pop_notices() if kind == "task_assigned"]
    assert assigned == [{
        "task_id": task["id"], "target_id": task["player_id"], "title": task["title"],
        "description": task["description"], "time_limit_sec": task["time_limit_sec"], "photo": False,
    }]


def test_photo_tasks_only_with_human_dealer():
    room = make_room(dealer_mode="none", dealer=False)
    with pytest.raises(ValueError, match="human dealer"):
        room.issue_task("player_1", task_type="photo")


# ---- confirmation by the dealer -------------------------------------------------------


def test_bonus_accepted_by_dealer_pays_reward():
    room = make_room()
    task = room.issue_task("player_2", kind="bonus")
    before = room.get_player("player_2").stack
    room.complete_task("player_2", task["id"])
    assert task["status"] == "pending_review"
    room.review_task(task["id"], approved=True)
    assert task["status"] == "approved"
    assert room.get_player("player_2").stack == before + 40


def test_rejected_penalty_gets_one_retry_then_costs_two_big_blinds():
    room = make_room()
    task = room.issue_task("player_2", kind="penalty")
    room.complete_task("player_2", task["id"])
    room.review_task(task["id"], approved=False)
    assert task["status"] == "pending"  # retry
    before = room.get_player("player_2").stack
    room.complete_task("player_2", task["id"])
    room.review_task(task["id"], approved=False)
    assert task["status"] == "rejected"
    assert room.get_player("player_2").stack == before - 40
    assert room.engine.carryover == 40


def test_refusing_penalty_costs_two_big_blinds_bonus_is_free():
    room = make_room()
    penalty = room.issue_task("player_2", kind="penalty")
    bonus = room.issue_task("player_3", kind="bonus")
    stacks = {player.id: player.stack for player in room.players}
    room.refuse_task("player_2", penalty["id"])
    room.refuse_task("player_3", bonus["id"])
    assert room.get_player("player_2").stack == stacks["player_2"] - 40
    assert room.get_player("player_3").stack == stacks["player_3"]
    with pytest.raises(ValueError, match="another player"):
        room.refuse_task("player_1", room.issue_task("player_2")["id"])


def test_cannot_review_finished_task():
    room = make_room()
    task = room.issue_task("player_1")
    room.review_task(task["id"], approved=True)
    with pytest.raises(ValueError, match="finished"):
        room.review_task(task["id"], approved=False)


# ---- confirmation by vote (no dealer) ---------------------------------------------------


def test_majority_of_other_players_confirms_task():
    room = make_room(4, dealer_mode="none", dealer=False)
    task = room.issue_task("player_1", kind="bonus")
    room.complete_task("player_1", task["id"])
    assert task["status"] == "voting"
    with pytest.raises(ValueError, match="own task"):
        room.vote_task("player_1", task["id"], True)
    room.vote_task("player_2", task["id"], True)
    assert task["status"] == "voting"
    view = room.view_for("player", "player_3")["tasks"][-1]["vote"]
    assert view == {"yes": 1, "no": 0, "needed": 2, "total": 3, "your_vote": None, "can_vote": True}
    room.vote_task("player_3", task["id"], True)
    assert task["status"] == "approved"


def test_majority_against_rejects_task():
    room = make_room(4, dealer_mode="none", dealer=False)
    task = room.issue_task("player_1")
    room.complete_task("player_1", task["id"])
    room.vote_task("player_2", task["id"], False)
    room.vote_task("player_3", task["id"], False)
    assert task["status"] == "pending"  # first rejection: retry


def test_human_mode_without_connected_dealer_falls_back_to_vote():
    room = make_room(dealer=False)
    task = room.issue_task("player_1")
    room.complete_task("player_1", task["id"])
    assert task["status"] == "voting"


# ---- automatic penalty after a big pot ---------------------------------------------------


def play_all_in(room):
    while room.phase == "playing":
        player = current(room)
        legal = room.engine.legal_actions()
        room.apply_player_action(player, "all_in" if legal["all_in"] else "call")


def test_biggest_loser_of_big_pot_gets_a_penalty():
    room = make_room(3)
    engine = room.engine
    engine.players[0].hole_cards = [Card.parse("As"), Card.parse("Ad")]
    engine.players[1].hole_cards = [Card.parse("7c"), Card.parse("2d")]
    engine.players[2].hole_cards = [Card.parse("8h"), Card.parse("3s")]
    play_all_in(room)
    penalties = [task for task in room.task_history if task["source"] == "auto"]
    assert len(penalties) == 1
    losses = {player.id: 1000 - player.stack for player in room.players}
    assert penalties[0]["player_id"] == max(losses, key=losses.get)
    assert penalties[0]["kind"] == "penalty"


def test_small_pot_or_disabled_setting_gives_no_penalty():
    room = make_room(3)
    room.apply_player_action(current(room), "fold")
    room.apply_player_action(current(room), "fold")
    assert not [task for task in room.task_history if task["source"] == "auto"]
    room = make_room(3, penalty_after_big_pot=False)
    play_all_in(room)
    assert not [task for task in room.task_history if task["source"] == "auto"]


# ---- decrees -------------------------------------------------------------------------------


def test_bets_multiple_of_big_blind():
    room = make_room(2)
    room.issue_decree("bets_multiple_of_bb")
    legal = room.engine.legal_actions()
    assert legal["raise_step"] == 20 and legal["min_raise_to"] % 20 == 0
    with pytest.raises(ValueError, match="multiples"):
        room.apply_player_action(current(room), "raise", 50)
    room.apply_player_action(current(room), "raise", 60)
    assert room.view_for("host")["decree"]["id"] == "bets_multiple_of_bb"


def test_raise_cap_allows_two_raises_per_round():
    room = make_room(3)
    room.issue_decree("raise_cap")
    room.apply_player_action(current(room), "raise", 40)
    room.apply_player_action(current(room), "raise", 80)
    legal = room.engine.legal_actions()
    assert legal["raise"] is False
    with pytest.raises(ValueError, match="not allowed"):
        room.apply_player_action(current(room), "raise", 200)
    room.apply_player_action(current(room), "call")


def test_free_decree_needs_text_and_cooldown_applies():
    room = make_room()
    with pytest.raises(ValueError, match="needs a text"):
        room.issue_decree("free")
    room.issue_decree("free", "Все говорят тостами")
    assert room.active_decree["description"] == "Все говорят тостами"
    assert room.engine.decree is None
    with pytest.raises(ValueError, match="in 5 hands"):
        room.issue_decree("silence")
    room.issue_decree("none")
    assert room.active_decree is None
    room.last_decree_hand = room.hand_number - 5
    room.issue_decree("silence")
    assert room.decree_cooldown() == 5


def test_decrees_only_during_game():
    room = make_room(start=False)
    with pytest.raises(ValueError, match="during the game"):
        room.issue_decree("silence")


# ---- fines -----------------------------------------------------------------------------------


def test_fine_is_limited_to_two_big_blinds_and_goes_to_next_pot():
    room = make_room(2)
    total = total_chips(room)
    with pytest.raises(ValueError, match="between 1 and 40"):
        room.fine_player("player_1", 41)
    assert room.fine_player("player_1", 20, reason="разговоры о картах") == 20
    assert room.engine.carryover == 20
    assert total_chips(room) == total
    room.apply_player_action(current(room), "fold")
    room.start_next_hand()
    while room.phase != "playing":
        room.process_timers(time.monotonic() + 100)
    assert room.view_for("host")["game"]["pot_total"] == 30 + 20
    room.apply_player_action(current(room), "fold")
    assert total_chips(room) == total
    assert sum(room.last_hand["winnings"].values()) == 50


def test_fine_keeps_one_chip_for_a_player_still_in_the_hand():
    room = make_room(2, starting_stack=50)
    player = room.engine.player(current(room))
    paid = room.fine_player(player.player_id, 40)
    assert paid == 39 and player.stack == 1


# ---- websocket ---------------------------------------------------------------------------------


def test_dealer_commands_over_websocket():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    dealer = client.post("/api/join", json={"token": created["dealer_token"], "name": "Judge"}).json()
    players = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B")
    ]
    room = rooms.get_room(created["room_id"])

    def recv_state(ws):
        message = ws.receive_json()
        while message["type"] != "state":
            assert message["type"] != "error", message
            message = ws.receive_json()
        return message["payload"]

    with client.websocket_connect(f"/ws?token={dealer['session_token']}") as ws:
        recv_state(ws)
        ws.send_json({"type": "dealer_issue_task", "payload": {
            "target_id": "player_1", "kind": "bonus", "source": "custom", "text": "Станцуй", "photo": False,
        }})
        state = recv_state(ws)
    task = state["tasks"][-1]
    assert task["title"] == "Станцуй" and task["kind"] == "bonus"

    with client.websocket_connect(f"/ws?token={players[0]['session_token']}") as ws:
        recv_state(ws)
        ws.send_json({"type": "task_done", "payload": {"task_id": task["id"]}})
        recv_state(ws)
        ws.send_json({"type": "dealer_fine", "payload": {"target_id": "player_2", "amount": 10}})
        error = ws.receive_json()
        while error["type"] != "error":
            error = ws.receive_json()
    assert error["payload"]["code"] == "forbidden"
    assert room.get_task(task["id"])["status"] == "pending_review"

    with client.websocket_connect(f"/ws?token={dealer['session_token']}") as ws:
        recv_state(ws)
        ws.send_json({"type": "dealer_review", "payload": {"task_id": task["id"], "accepted": True}})
        state = recv_state(ws)
    assert state["tasks"][-1]["status"] == "approved"
