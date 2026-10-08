import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app, rooms
from app.models import NEXT_HAND_DELAY_SEC, LobbySettings, Room


def make_room(count=3):
    room = Room(room_id="views_room", host_name="Host", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(chaos_level="full"))
    for index in range(count):
        room.add_player(name=f"P{index + 1}", agreed_to_terms=True)
    room.start_game()
    # Skip the event reveal (and a possible carousel) to reach the betting.
    while room.phase != "playing":
        room.process_timers(time.monotonic() + 100)
    return room


def all_cards(room):
    return {card for player in room.players for card in player.hole_cards}


def public_cards(room):
    """Hole cards the rules make public: the card opened by the Spy event."""
    spy = room.engine.event_state.get("spy")
    return {spy["card"]} if spy else set()


def test_player_sees_only_own_hole_cards():
    room = make_room()
    for player in room.players:
        state = room.view_for("player", player.id)
        own = set(player.hole_cards)
        dumped = json.dumps(state)
        assert state["you"]["hole_cards"] == player.hole_cards
        for other in all_cards(room) - own - public_cards(room):
            assert f'"{other}"' not in dumped


@pytest.mark.parametrize("role", ["host", "dealer"])
def test_host_and_dealer_never_receive_hole_cards(role):
    room = make_room()
    dumped = json.dumps(room.view_for(role))
    for card in all_cards(room) - public_cards(room):
        assert f'"{card}"' not in dumped
    assert "you" not in room.view_for(role)


def test_abilities_and_session_tokens_are_private():
    room = make_room()
    state = room.view_for("player", "player_1")
    dumped = json.dumps(state)
    for player in room.players:
        assert player.session_token not in dumped
    assert all("abilities" not in item for item in state["players"])
    assert state["players"][1]["ability_count"] == len(room.players[1].abilities)


def test_photo_under_review_is_hidden_from_other_players():
    room = make_room()
    task = room.issue_task("player_1", task_type="photo", kind="bonus")
    room.submit_photo_task("player_1", "/media/secret.jpg")

    assert "submission" not in room.view_for("player", "player_2")["tasks"][-1]
    assert "submission" not in room.view_for("host")["tasks"][-1]
    assert room.view_for("dealer")["tasks"][-1]["submission"]["url"] == "/media/secret.jpg"
    assert room.view_for("player", "player_1")["tasks"][-1]["submission"]

    room.review_task(task["id"], approved=True)
    assert room.view_for("host")["tasks"][-1]["submission"]


def test_showdown_reveals_only_players_who_did_not_fold():
    room = make_room()
    engine = room.engine
    folder = engine.players[engine.current_index].player_id
    room.apply_player_action(folder, "fold")
    while not engine.hand_over:
        current = engine.players[engine.current_index].player_id
        room.apply_player_action(current, "check" if engine.legal_actions()["check"] else "call")

    revealed = room.view_for("player", folder)["last_hand"]["revealed"]
    assert folder not in revealed
    assert len(revealed) == 2


def test_session_tokens_are_random():
    room = make_room(2)
    tokens = [player.session_token for player in room.players]
    assert len(set(tokens)) == 2
    assert all(len(token) >= 20 and room.room_id not in token for token in tokens)


def test_hand_finishes_and_next_hand_starts_after_delay():
    room = make_room(2)
    engine = room.engine
    room.apply_player_action(engine.players[engine.current_index].player_id, "fold")

    assert room.last_hand["winnings"]
    assert sum(player.stack for player in room.players) == 2000
    assert room.next_hand_at is not None
    assert room.process_timers(time.monotonic()) is False
    assert room.process_timers(time.monotonic() + NEXT_HAND_DELAY_SEC + 1) is True
    assert engine.hands_played == 1
    assert engine.street.value == "preflop"


def test_paused_room_ignores_timers_and_actions():
    room = make_room(2)
    room.pause_game()
    current = room.engine.players[room.engine.current_index].player_id

    assert room.process_timers(time.monotonic() + 1000) is False
    with pytest.raises(ValueError, match="paused"):
        room.apply_player_action(current, "fold")


def test_game_ends_when_one_player_has_all_chips():
    room = make_room(2)
    engine = room.engine
    while not room.game_over:
        if room.phase != "playing":
            # Next hand, event reveal and carousel all advance by timers.
            room.process_timers(time.monotonic() + 1000)
            continue
        current = engine.players[engine.current_index].player_id
        room.apply_player_action(current, "all_in" if engine.legal_actions()["all_in"] else "call")

    assert room.results["standings"][0]["stack"] == 2000
    assert room.next_hand_at is None


def test_join_token_cannot_open_websocket_and_guessed_token_fails():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    client.post("/api/join", json={"token": created["player_token"], "name": "A", "agreed_to_terms": True})

    with client.websocket_connect(f"/ws?token={created['player_token']}") as websocket:
        assert websocket.receive_json()["payload"]["code"] == "unauthorized"
    assert client.get(f"/api/session?token=session_1_{created['room_id']}").status_code == 404
    assert client.get(f"/api/session?token={created['dealer_token']}").status_code == 404


def test_peek_result_is_sent_privately_over_websocket():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    joined = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B")
    ]
    client.post(f"/api/rooms/{created['room_id']}/start", json={"host_token": created["host_token"]})
    room = rooms.get_room(created["room_id"])
    room.players[0].abilities = ["peek"]

    with client.websocket_connect(f"/ws?token={joined[0]['session_token']}") as owner:
        owner.receive_json()
        owner.send_json({"type": "use_ability", "payload": {"ability_id": "peek", "target_id": "player_2"}, "msg_id": 1})
        messages = [owner.receive_json() for _ in range(3)]

    by_type = {message["type"]: message["payload"] for message in messages}
    assert by_type["private_notice"]["revealed_card"] in room.players[1].hole_cards
    assert "revealed_card" not in by_type["ability_used"]
    assert "Подглядеть" in by_type["state"]["feed"][-1]


def test_websocket_rejects_actions_from_wrong_role():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    with client.websocket_connect(f"/ws?token={created['host_token']}") as host:
        host.receive_json()
        host.send_json({"type": "action", "payload": {"kind": "fold"}, "msg_id": 7})
        error = host.receive_json()

    assert error["type"] == "error"
    assert error["payload"]["code"] == "forbidden"
    assert error["msg_id"] == 7
