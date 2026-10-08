import random
import time

import pytest
from fastapi.testclient import TestClient

from app.game import sidebets
from app.game.cards import Card
from app.game.engine import EnginePlayer, HoldemEngine
from app.main import app, rooms
from app.models import LobbySettings, Room


def cards(*values):
    return [Card.parse(value) for value in values]


def make_engine(count=3, stack=1000, seed=5):
    engine = HoldemEngine([EnginePlayer(f"p{index}", stack) for index in range(count)], rng=random.Random(seed))
    engine.start_hand()
    return engine


def offers(*ids):
    return [{"id": bet_id, "kind": sidebets.offer_kind(bet_id)} for bet_id in ids]


def play_to_showdown(engine):
    while not engine.hand_over:
        player = engine.players[engine.current_index]
        engine.act(player.player_id, "check" if engine.legal_actions()["check"] else "call")


def test_server_offers_three_different_statements():
    seen = set()
    for seed in range(30):
        engine = make_engine(seed=seed)
        ids = [offer["kind"] for offer in engine.side_bet_offers]
        assert len(ids) == sidebets.OFFERS_PER_HAND == len(set(ids))
        for offer in engine.side_bet_offers:
            seen.add(offer["kind"])
            if offer["kind"] == sidebets.PLAYER_WINS:
                assert offer["player_id"] in {"p0", "p1", "p2"}
    assert seen == {sidebets.PAIR_ON_BOARD, sidebets.FLOP_SUITED, sidebets.SHOWDOWN, sidebets.PLAYER_WINS}


def test_bet_costs_one_big_blind_once_per_hand_before_first_action():
    engine = make_engine()
    bet_id = engine.side_bet_offers[0]["id"]
    stack = engine.player("p1").stack
    assert engine.place_side_bet("p1", bet_id) == 20
    assert engine.player("p1").stack == stack - 20
    with pytest.raises(ValueError, match="already"):
        engine.place_side_bet("p1", bet_id)
    with pytest.raises(ValueError, match="Unknown"):
        engine.place_side_bet("p2", "made_up")
    engine.act(engine.players[engine.current_index].player_id, "call")
    with pytest.raises(ValueError, match="closed"):
        engine.place_side_bet("p2", bet_id)


def test_bet_needs_enough_chips():
    engine = HoldemEngine([EnginePlayer("a", 1000), EnginePlayer("b", 1000), EnginePlayer("poor", 40)])
    engine.start_hand()
    with pytest.raises(ValueError, match="Not enough"):
        engine.place_side_bet("poor", engine.side_bet_offers[0]["id"])


@pytest.mark.parametrize("bet_id, board, expected", [
    ("pair_on_board", ("As", "Ad", "7c", "4h", "2s"), True),
    ("pair_on_board", ("As", "Kd", "7c", "4h", "2s"), False),
    ("flop_suited", ("As", "Ks", "7s", "4h", "2d"), True),
    ("flop_suited", ("As", "Ks", "7c", "4s", "2s"), False),
])
def test_board_statements(bet_id, board, expected):
    assert sidebets.resolve(bet_id, board=cards(*board), main_pot_winners=[], went_to_showdown=True) is expected


def test_winners_split_pool_equally():
    engine = make_engine()
    engine.side_bet_offers = offers("showdown", "pair_on_board")
    for player_id in ("p0", "p1", "p2"):
        engine.place_side_bet(player_id, "showdown" if player_id != "p2" else "pair_on_board")
    play_to_showdown(engine)
    engine.board[:] = cards("As", "Kd", "7c", "4h", "2s")
    engine.settle_showdown()
    results = engine.side_bet_results
    assert results["outcomes"] == {"showdown": True, "pair_on_board": False}
    assert results["payouts"] == {"p0": 30, "p1": 30} or results["payouts"] == {"p1": 30, "p0": 30}
    assert sum(player.stack for player in engine.players) == 3000


def test_nobody_guessed_pool_goes_to_main_pot():
    engine = make_engine(2)
    engine.side_bet_offers = offers("showdown")
    engine.place_side_bet("p0", "showdown")
    engine.place_side_bet("p1", "showdown")
    folder = engine.players[engine.current_index]
    engine.act(folder.player_id, "fold")
    winnings = engine.settle_showdown()
    assert engine.side_bet_results["to_main_pot"] is True
    winner = next(player.player_id for player in engine.players if player is not folder)
    assert winnings == {winner: 30 + 40}
    assert sum(player.stack for player in engine.players) == 2000


def test_player_wins_statement_follows_main_pot_winner():
    engine = make_engine(2)
    engine.side_bet_offers = offers("player_wins:p0", "player_wins:p1")
    engine.place_side_bet("p0", "player_wins:p1")
    engine.place_side_bet("p1", "player_wins:p0")
    engine.players[0].hole_cards = cards("As", "Ad")
    engine.players[1].hole_cards = cards("7c", "2d")
    play_to_showdown(engine)
    engine.board[:] = cards("Ah", "Kd", "9c", "4h", "3s")
    engine.settle_showdown()
    assert engine.main_pot_winners == ["p0"]
    assert engine.side_bet_results["winner_ids"] == ["p1"]
    assert engine.side_bet_results["payouts"] == {"p1": 40}


def test_odd_chip_goes_left_of_button():
    engine = HoldemEngine([EnginePlayer(f"p{index}", 1000) for index in range(3)], rng=random.Random(1))
    engine.start_hand()
    engine.side_bet_offers = offers("showdown")
    engine.side_bet_pool = 1  # extra odd chip on top of the stakes
    for player_id in ("p0", "p1"):
        engine.place_side_bet(player_id, "showdown")
    play_to_showdown(engine)
    engine.settle_showdown()
    payouts = engine.side_bet_results["payouts"]
    assert payouts == {"p1": 21, "p0": 20}


def test_room_side_bets_and_websocket():
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    joined = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B", "C")
    ]
    client.post(f"/api/rooms/{created['room_id']}/start", json={"host_token": created["host_token"]})
    room = rooms.get_room(created["room_id"])
    while room.phase != "playing":
        room.process_timers(time.monotonic() + 100)
    state = room.view_for("player", "player_2")["game"]["side_bets"]
    assert state["open"] and state["stake"] == 20
    assert all(offer["text"] for offer in state["offers"])

    with client.websocket_connect(f"/ws?token={joined[1]['session_token']}") as ws:
        ws.receive_json()
        ws.send_json({"type": "side_bet", "payload": {"bet_id": state["offers"][0]["id"]}, "msg_id": 1})
        message = ws.receive_json()
        while message["type"] != "state":
            message = ws.receive_json()
    assert message["payload"]["game"]["side_bets"]["pool"] == 20
    assert message["payload"]["game"]["side_bets"]["bets"] == {"player_2": state["offers"][0]["id"]}
    assert "ставит 20" in message["payload"]["feed"][-1]


def test_chips_conserved_with_side_bets_over_many_hands():
    rng = random.Random(9)
    room = Room(room_id="sb", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(chaos_level="full", starting_stack=300))
    for index in range(4):
        room.add_player(name=f"P{index}", agreed_to_terms=True)
    room.start_game()
    for _ in range(3000):
        if room.game_over:
            break
        if room.phase != "playing":
            room.process_timers(time.monotonic() + 100)
            continue
        engine = room.engine
        if engine.side_bets_open and rng.random() < 0.5:
            bettor = rng.choice(engine.players)
            if bettor.player_id not in engine.side_bets and not bettor.folded and bettor.stack > engine.big_blind:
                room.place_side_bet(bettor.player_id, rng.choice(engine.side_bet_offers)["id"])
        legal = engine.legal_actions()
        kind = rng.choice([kind for kind in ("fold", "check", "call", "raise", "all_in") if legal[kind]])
        room.apply_player_action(engine.players[engine.current_index].player_id, kind)
        on_table = 0 if engine.settled else sum(player.committed for player in engine.players) + engine.side_bet_pool
        assert sum(player.stack for player in room.players) + on_table == 1200
    assert room.game_over
