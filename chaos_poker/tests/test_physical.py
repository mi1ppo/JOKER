import json
import time

import pytest
from fastapi.testclient import TestClient

from app.game.activities import duel_winner
from app.main import app, rooms
from app.models import LobbySettings, Room


def make_room(count=3, *, mode="human", dealer=True, intensity="medium", start=True):
    room = Room(room_id="ph", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(chaos_level="light", dealer_mode=mode, physical_activity=intensity))
    for index in range(count):
        room.add_player(name=f"P{index + 1}", agreed_to_terms=True)
    if dealer:
        room.connect_dealer()
    if start:
        room.start_game()
    return room


def chips(room):
    engine = room.engine
    on_table = 0 if engine.settled else sum(player.committed for player in engine.players) + engine.hand_carryover
    return sum(player.stack for player in room.players) + on_table + engine.carryover


def test_activities_respect_lobby_setting():
    room = make_room(intensity="off")
    with pytest.raises(ValueError, match="off"):
        room.start_activity("freeze")
    room = make_room()
    with pytest.raises(ValueError, match="Unknown activity"):
        room.start_activity("bungee")


def test_duel_rules():
    assert duel_winner("rock", "scissors") == 0
    assert duel_winner("rock", "paper") == 1
    assert duel_winner("paper", "paper") is None


# ---- task activities ---------------------------------------------------------------


@pytest.mark.parametrize("intensity, reps", [("light", 10), ("medium", 20), ("hard", 30)])
def test_squats_text_follows_intensity(intensity, reps):
    room = make_room(intensity=intensity)
    task = room.start_activity("squats", players=["player_2"])
    assert task["task_type"] == "physical" and task["activity"] == "squats"
    assert f"{reps} приседаний" in task["description"]


def test_physical_task_confirmed_by_human_dealer():
    room = make_room()
    task = room.start_activity("standing_table", players=["player_1"])
    room.complete_task("player_1", task["id"])
    assert task["status"] == "pending_review"
    room.review_task(task["id"], approved=True)
    assert task["status"] == "approved"


def test_with_ai_dealer_the_player_confirms_himself():
    room = make_room(mode="ai", dealer=False)
    physical = room.start_activity("squats", players=["player_1"])
    room.complete_task("player_1", physical["id"])
    assert physical["status"] == "approved"
    text = room.issue_task("player_2", kind="bonus")
    before = room.get_player("player_2").stack
    room.complete_task("player_2", text["id"])
    assert text["status"] == "approved"
    assert room.get_player("player_2").stack == before + text["reward"]


def test_without_dealer_physical_task_is_voted():
    room = make_room(mode="none", dealer=False)
    task = room.start_activity("squats", players=["player_1"])
    room.complete_task("player_1", task["id"])
    assert task["status"] == "voting"


def test_skipping_a_physical_task_costs_one_big_blind():
    room = make_room()
    task = room.start_activity("squats", players=["player_3"])
    before = room.get_player("player_3").stack
    room.refuse_task("player_3", task["id"])
    assert task["refuse_cost"] == 20
    assert room.get_player("player_3").stack == before - 20


# ---- shake battle -------------------------------------------------------------------------


def test_shake_battle_judged_by_dealer():
    room = make_room()
    activity = room.start_activity("shake_battle", players=["player_1", "player_2"])
    assert activity["judge"] == "dealer"
    with pytest.raises(ValueError, match="in progress"):
        room.start_activity("freeze")
    with pytest.raises(ValueError, match="dealer decides"):
        room.report_activity("player_1", winner_id="player_1")
    with pytest.raises(ValueError, match="participants"):
        room.resolve_activity(winner_id="player_3")
    before = room.get_player("player_2").stack
    room.resolve_activity(winner_id="player_2")
    assert activity["status"] == "done"
    assert room.get_player("player_2").stack == before + 40


def test_shake_battle_without_human_dealer_needs_agreement():
    room = make_room(mode="ai", dealer=False)
    activity = room.start_activity("shake_battle")
    assert len(activity["players"]) == 2 and activity["judge"] == "players"
    first, second = activity["players"]
    room.report_activity(first, winner_id=second)
    assert activity["status"] == "active"
    room.report_activity(second, winner_id=second)
    assert activity["result"]["winner_id"] == second

    room.start_activity("shake_battle", players=[first, second])
    room.report_activity(first, winner_id=first)
    room.report_activity(second, winner_id=second)
    assert room.active_activity["result"] == {"draw": True}


# ---- freeze ---------------------------------------------------------------------------------


def test_freeze_dealer_marks_movers_who_pay_one_big_blind():
    room = make_room()
    total = chips(room)
    activity = room.start_activity("freeze")
    assert activity["players"] == ["player_1", "player_2", "player_3"]
    room.resolve_activity(movers=["player_1", "player_3"])
    assert activity["result"]["fines"] == {"player_1": 20, "player_3": 20}
    assert room.engine.carryover == 40
    assert chips(room) == total


def test_freeze_self_reports_and_timeout():
    room = make_room(mode="none", dealer=False)
    activity = room.start_activity("freeze")
    room.report_activity("player_1", moved=True)
    room.report_activity("player_2", moved=False)
    assert activity["status"] == "active"
    assert room.view_for("host")["activity"]["reported"] == ["player_1", "player_2"]
    assert room.process_timers(time.monotonic() + 100) is True  # player_3 is silent -> did not move
    assert activity["result"]["movers"] == ["player_1"]


# ---- duel -------------------------------------------------------------------------------------


def test_duel_choices_are_secret_and_winner_takes_the_stake():
    room = make_room()
    total = chips(room)
    activity = room.start_activity("duel", players=["player_2", "player_3"])
    with pytest.raises(ValueError, match="duelists"):
        room.duel_pick("player_1", "rock")
    room.duel_pick("player_2", "rock")
    for role, player_id in (("host", None), ("dealer", None), ("player", "player_3")):
        dumped = json.dumps(room.view_for(role, player_id)["activity"])
        assert "rock" not in dumped
    assert room.view_for("player", "player_2")["activity"]["your_pick"] == "rock"
    before = {player_id: room.get_player(player_id).stack for player_id in ("player_2", "player_3")}
    room.duel_pick("player_3", "scissors")
    assert activity["result"]["winner_id"] == "player_2"
    assert room.get_player("player_2").stack == before["player_2"] + 20
    assert room.get_player("player_3").stack == before["player_3"] - 20
    assert chips(room) == total


def test_duel_draws_replay_up_to_three_rounds():
    room = make_room()
    activity = room.start_activity("duel", players=["player_1", "player_2"])
    for round_number in (1, 2, 3):
        assert activity["round"] == round_number
        room.duel_pick("player_1", "paper")
        room.duel_pick("player_2", "paper")
    assert activity["status"] == "done" and activity["result"]["draw"] is True


def test_skipping_a_contest_costs_a_fee_and_cancels_when_too_few_left():
    room = make_room()
    activity = room.start_activity("duel", players=["player_1", "player_2"])
    before = room.get_player("player_1").stack
    room.skip_activity("player_1")
    assert room.get_player("player_1").stack == before - 20
    assert activity["status"] == "cancelled"


def test_dealer_or_host_can_cancel():
    room = make_room()
    activity = room.start_activity("freeze")
    room.cancel_activity()
    assert activity["status"] == "cancelled"
    with pytest.raises(ValueError, match="No activity"):
        room.cancel_activity()


# ---- websocket ------------------------------------------------------------------------------------


def test_activity_commands_over_websocket():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    client.post(f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
                json={"physical_activity": "medium"})
    dealer = client.post("/api/join", json={"token": created["dealer_token"], "name": "Judge"}).json()
    players = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B")
    ]
    room = rooms.get_room(created["room_id"])

    def first_of(ws, kinds):
        while True:
            message = ws.receive_json()
            if message["type"] in kinds:
                return message

    with client.websocket_connect(f"/ws?token={dealer['session_token']}") as ws:
        first_of(ws, {"state"})
        ws.send_json({"type": "dealer_start_activity", "payload": {"type": "duel", "players": ["player_1", "player_2"]}})
        state = first_of(ws, {"state"})["payload"]
    assert state["activity"]["type"] == "duel"

    with client.websocket_connect(f"/ws?token={players[0]['session_token']}") as ws:
        first_of(ws, {"state"})
        ws.send_json({"type": "activity_resolve", "payload": {"winner_id": "player_1"}})
        assert first_of(ws, {"error"})["payload"]["code"] == "forbidden"
        ws.send_json({"type": "duel_pick", "payload": {"choice": "paper"}})
        first_of(ws, {"state"})
    with client.websocket_connect(f"/ws?token={players[1]['session_token']}") as ws:
        first_of(ws, {"state"})
        ws.send_json({"type": "duel_pick", "payload": {"choice": "rock"}})
        result = first_of(ws, {"activity_result"})["payload"]
    assert result["winner_id"] == "player_1"
    assert room.active_activity["status"] == "done"
