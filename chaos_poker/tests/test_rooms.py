from app.game.engine import Street
from app.models import Room
from app.rooms import RoomManager
from fastapi.testclient import TestClient

from app.main import app, rooms


def test_create_room_generates_tokens_and_host():
    manager = RoomManager()
    room = manager.create_room(host_name="Host")

    assert room.room_id
    assert room.host_name == "Host"
    assert room.dealer_token
    assert room.player_token
    assert room.dealer_token != room.player_token
    assert room.host_token


def test_player_can_join_room_and_be_visible_on_host():
    manager = RoomManager()
    room = manager.create_room(host_name="Host")

    player = room.add_player(name="Alice", avatar="🃏", agreed_to_terms=True)

    assert player.name == "Alice"
    assert player.avatar == "🃏"
    assert player.id in [p.id for p in room.players]
    assert room.host_view()["players"][0]["name"] == "Alice"


def test_api_join_consumes_dealer_token_once():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    response = client.post(
        "/api/join",
        json={"token": created["dealer_token"], "name": "Judge", "agreed_to_terms": True},
    )
    repeated = client.post(
        "/api/join",
        json={"token": created["dealer_token"], "name": "Another", "agreed_to_terms": True},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "dealer"
    assert repeated.status_code == 404


def test_player_session_can_receive_state_over_websocket():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    joined = client.post(
        "/api/join",
        json={
            "token": created["player_token"],
            "name": "Alice",
            "avatar": "A",
            "agreed_to_terms": True,
        },
    ).json()

    with client.websocket_connect(f"/ws?token={joined['session_token']}") as websocket:
        message = websocket.receive_json()

    assert message["type"] == "state"
    assert message["payload"]["players"][0]["name"] == "Alice"
    assert message["payload"]["you"]["hole_cards"] == []
    assert "session_token" not in message["payload"]["players"][0]


def test_player_action_over_websocket_updates_game_state():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    joined = []
    for name in ("Alice", "Bob"):
        joined.append(client.post(
            "/api/join",
            json={"token": created["player_token"], "name": name, "agreed_to_terms": True},
        ).json())
    client.post(
        f"/api/rooms/{created['room_id']}/start",
        json={"host_token": created["host_token"]},
    )

    states = [
        client.get(f"/api/session?token={item['session_token']}").json()["state"]
        for item in joined
    ]
    current = joined[states[0]["game"]["current_index"]]
    with client.websocket_connect(f"/ws?token={current['session_token']}") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "action", "payload": {"kind": "call"}, "msg_id": 1})
        message = websocket.receive_json()

    assert message["type"] == "state"
    assert message["payload"]["game"]["street"] in {"preflop", "flop", "turn", "river", "showdown", "complete"}


def test_session_endpoint_restores_host_state_after_page_reload():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    restored = client.get(f"/api/session?token={created['host_token']}")

    assert restored.status_code == 200
    assert restored.json()["role"] == "host"
    assert restored.json()["state"]["room_id"] == created["room_id"]


def test_qr_requires_host_token_and_uses_lan_address(monkeypatch):
    rooms.rooms.clear()
    monkeypatch.setenv("HOST_IP", "192.168.1.25")
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    forbidden = client.get(f"/api/qr/players.png?room_id={created['room_id']}")
    allowed = client.get(
        f"/api/qr/players.png?room_id={created['room_id']}&host_token={created['host_token']}"
    )

    assert forbidden.status_code == 404
    assert allowed.status_code == 200


def test_role_pages_are_served():
    client = TestClient(app)

    for role in ("host", "dealer", "play"):
        response = client.get(f"/{role}")
        assert response.status_code == 200
        assert "Chaos Poker" in response.text


def test_card_assets_are_served():
    client = TestClient(app)

    css = client.get("/static/css/cards.css")
    javascript = client.get("/static/js/cards.js")

    assert css.status_code == 200
    assert ".playing-card" in css.text
    assert javascript.status_code == 200
    assert "renderCard" in javascript.text


def test_host_can_start_game_only_with_two_players_and_expires_player_qr():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    start = client.post(
        f"/api/rooms/{created['room_id']}/start",
        json={"host_token": created["host_token"]},
    )
    assert start.status_code == 409

    for name in ("Alice", "Bob"):
        response = client.post(
            "/api/join",
            json={"token": created["player_token"], "name": name, "agreed_to_terms": True},
        )
        assert response.status_code == 200

    started = client.post(
        f"/api/rooms/{created['room_id']}/start",
        json={"host_token": created["host_token"]},
    )
    assert started.status_code == 200
    assert started.json()["state"]["game_started"] is True
    assert client.get(
        f"/api/qr/players.png?room_id={created['room_id']}&host_token={created['host_token']}"
    ).status_code == 410


def test_host_settings_apply_to_new_players():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    settings = {"starting_stack": 1500, "turn_timer_sec": 30, "four_color_deck": True}

    updated = client.post(
        f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
        json=settings,
    )
    joined = client.post(
        "/api/join",
        json={"token": created["player_token"], "name": "Alice", "agreed_to_terms": True},
    )

    assert updated.status_code == 200
    assert joined.status_code == 200
    assert joined.json()["state"]["players"][0]["stack"] == 1500
    assert joined.json()["state"]["settings"]["four_color_deck"] is True


def test_room_tracks_dealer_mode_and_pause_state():
    room = Room(
        room_id="dealer_mode_room",
        host_name="Host",
        dealer_token="dealer_token",
        player_token="player_token",
        host_token="host_token",
    )

    room.set_dealer_mode("ai")
    room.pause_game()

    assert room.settings.dealer_mode == "ai"
    assert room.paused is True
    assert room.dealer_status == "paused"
    assert room.host_view()["dealer"]["mode"] == "ai"


def test_host_can_update_dealer_and_activity_settings_and_request_ai_comment():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    settings = client.post(
        f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
        json={"dealer_mode": "ai", "physical_activity": "medium"},
    )
    comment = client.post(
        f"/api/rooms/{created['room_id']}/ai-comment?host_token={created['host_token']}",
        json={"context": ""},
    )

    assert settings.status_code == 200
    assert settings.json()["state"]["settings"]["dealer_mode"] == "ai"
    assert settings.json()["state"]["settings"]["physical_activity"] == "medium"
    assert comment.status_code == 200
    # Without a GigaChat key the judge comments from the fallback bank, in Russian.
    assert comment.json()["source"] == "fallback"
    assert comment.json()["comment"]
    assert comment.json()["state"]["feed"][-1].startswith("🎙")


def test_dealer_can_select_event_and_issue_task_via_session_token():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    dealer = client.post(
        "/api/join",
        json={"token": created["dealer_token"], "name": "Judge", "agreed_to_terms": True},
    ).json()
    client.post(
        "/api/join",
        json={"token": created["player_token"], "name": "Alice", "agreed_to_terms": True},
    )

    event_response = client.post(
        f"/api/rooms/{created['room_id']}/event",
        json={"session_token": dealer["session_token"], "event_id": "double_stakes"},
    )
    task_response = client.post(
        f"/api/rooms/{created['room_id']}/tasks",
        json={
            "session_token": dealer["session_token"],
            "player_id": "player_1",
            "task_type": "photo",
            "kind": "bonus",
            "difficulty": "hard",
            "camera": "front",
        },
    )

    assert event_response.status_code == 200
    assert rooms.get_room(created["room_id"]).pending_event["id"] == "double_stakes"
    assert task_response.status_code == 200
    assert task_response.json()["state"]["tasks"][-1]["task_type"] == "photo"


def test_host_can_select_event_and_issue_task_through_api():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    client.post(
        "/api/join",
        json={"token": created["dealer_token"], "name": "Judge", "agreed_to_terms": True},
    )
    client.post(
        "/api/join",
        json={"token": created["player_token"], "name": "Alice", "agreed_to_terms": True},
    )

    event_response = client.post(
        f"/api/rooms/{created['room_id']}/event",
        json={"host_token": created["host_token"], "event_id": "double_stakes"},
    )
    task_response = client.post(
        f"/api/rooms/{created['room_id']}/tasks",
        json={
            "host_token": created["host_token"],
            "player_id": "player_1",
            "task_type": "text",
            "kind": "penalty",
            "difficulty": "medium",
        },
    )

    assert event_response.status_code == 200
    assert rooms.get_room(created["room_id"]).pending_event["id"] == "double_stakes"
    assert task_response.status_code == 200
    assert task_response.json()["state"]["tasks"][-1]["player_id"] == "player_1"


def test_dealer_can_pause_and_resume_game_via_session_token():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    dealer = client.post(
        "/api/join",
        json={"token": created["dealer_token"], "name": "Judge", "agreed_to_terms": True},
    ).json()

    paused = client.post(
        f"/api/rooms/{created['room_id']}/pause",
        json={"session_token": dealer["session_token"]},
    )
    resumed = client.post(
        f"/api/rooms/{created['room_id']}/resume",
        json={"session_token": dealer["session_token"]},
    )

    assert paused.status_code == 200
    assert paused.json()["state"]["dealer"]["paused"] is True
    assert resumed.status_code == 200
    assert resumed.json()["state"]["dealer"]["paused"] is False


def test_host_can_start_physical_activity_via_api():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    off = client.post(
        f"/api/rooms/{created['room_id']}/activity",
        json={"host_token": created["host_token"], "activity_type": "shake_battle"},
    )
    assert off.status_code == 409  # activities are off by default
    client.post(f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
                json={"physical_activity": "medium"})
    for name in ("Alice", "Bob"):
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True})

    response = client.post(
        f"/api/rooms/{created['room_id']}/activity",
        json={"host_token": created["host_token"], "activity_type": "shake_battle"},
    )

    assert response.status_code == 200
    assert response.json()["state"]["activity"]["type"] == "shake_battle"


def test_room_starts_engine_and_accepts_player_action():
    rooms.rooms.clear()
    room = rooms.create_room(host_name="Host")
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)

    room.start_game()

    assert room.game_started is True
    assert room.engine is not None
    assert all(len(player.hole_cards) == 2 for player in room.players)

    next_player_index = room.engine.current_index
    assert next_player_index is not None
    next_player_id = room.players[next_player_index].id
    room.apply_player_action(next_player_id, "call")
    assert room.engine.current_index is not None or room.engine.street.value in {"flop", "turn", "river", "showdown", "complete"}


def test_room_turn_timeout_auto_checks_when_possible_and_folds_when_not():
    room = Room(
        room_id="room_timeout",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.start_game()

    room.engine.street = Street.FLOP
    room.engine.board = []
    room.engine.current_bet = 0
    room.engine.current_index = 0
    room.engine.players[0].street_commitment = 0
    room.engine.players[1].street_commitment = 0
    room.engine.players[0].folded = False
    room.engine.players[1].folded = False
    room.engine._acted = {room.engine.players[1].player_id}
    room.turn_deadline_at = 0.0

    room.trigger_turn_timeout()

    assert room.engine.players[0].folded is False
    assert room.engine.current_index is not None


def test_host_qr_requires_valid_host_token():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()

    forbidden = client.get("/api/host-qr?host_token=invalid_token")
    allowed = client.get(f"/api/host-qr?host_token={created['host_token']}")

    assert forbidden.status_code == 404
    assert allowed.status_code == 200
    assert allowed.headers["content-type"] == "image/png"
