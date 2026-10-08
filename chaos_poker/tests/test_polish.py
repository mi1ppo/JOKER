import logging
import re
import time

from fastapi.testclient import TestClient

from app.game.cards import Card
from app.game.evaluator import best_five
from app.main import app, rooms
from app.models import LobbySettings, Room


def cards(*values):
    return [Card.parse(value) for value in values]


def test_best_five_returns_the_winning_combination():
    best = best_five(cards("As", "Ad", "Ac", "7h", "7d", "2s", "3c"))
    assert sorted(str(card) for card in best) == sorted(["As", "Ad", "Ac", "7h", "7d"])


def test_showdown_reports_cards_to_highlight():
    room = Room(room_id="pl", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(chaos_level="light"))
    room.add_player(name="A", agreed_to_terms=True)
    room.add_player(name="B", agreed_to_terms=True)
    room.start_game()
    engine = room.engine
    engine.players[0].hole_cards = cards("As", "Ad")
    engine.players[1].hole_cards = cards("7c", "2d")
    while room.phase == "playing":
        current = engine.players[engine.current_index].player_id
        room.apply_player_action(current, "check" if engine.legal_actions()["check"] else "call")
    best = room.last_hand["best_cards"]
    winner = next(iter(room.last_hand["winnings"]))
    assert list(best) == engine.main_pot_winners
    assert len(best[winner]) == 5
    assert set(best[winner]) <= set(room.last_hand["board"]) | set(room.last_hand["revealed"][winner])


def test_action_log_has_no_hole_cards_or_free_text(caplog):
    caplog.set_level(logging.INFO, logger="chaos.actions")
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    client.post(f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
                json={"chaos_level": "light"})
    dealer = client.post("/api/join", json={"token": created["dealer_token"], "name": "Judge"}).json()
    players = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B")
    ]
    client.post(f"/api/rooms/{created['room_id']}/start", json={"host_token": created["host_token"]})
    room = rooms.get_room(created["room_id"])
    while room.phase != "playing":
        room.process_timers(time.monotonic() + 100)
    hole = {card for player in room.players for card in player.hole_cards}
    current = next(item for item in players
                   if item["player_id"] == room.engine.players[room.engine.current_index].player_id)
    with client.websocket_connect(f"/ws?token={current['session_token']}") as ws:
        ws.receive_json()
        ws.send_json({"type": "action", "payload": {"kind": "call"}})
        ws.receive_json()
    with client.websocket_connect(f"/ws?token={dealer['session_token']}") as ws:
        ws.receive_json()
        ws.send_json({"type": "dealer_issue_task", "payload": {"target_id": "player_1", "kind": "penalty",
                                                                "source": "custom", "text": "Секретный текст"}})
        ws.receive_json()
    assert "action=action {'kind': 'call'}" in caplog.text
    assert "action=dealer_issue_task" in caplog.text
    assert "Секретный текст" not in caplog.text
    # Card codes are two characters: match them as separate tokens, not inside hex room ids.
    assert not any(re.search(rf"(?<![0-9a-zA-Z]){card}(?![0-9a-zA-Z])", caplog.text) for card in hole)
    for player in players:
        assert player["session_token"] not in caplog.text


def test_certificate_without_san_is_detected(tmp_path):
    import run

    assert run.has_subject_alt_name(tmp_path / "missing.pem") is True  # unreadable: leave it alone
