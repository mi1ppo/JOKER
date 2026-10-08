import json
import random
import time

import pytest

from app.game.cards import Card
from app.game.engine import EnginePlayer, HoldemEngine, Street
from app.game.evaluator import FLUSH, FOUR_OF_A_KIND, evaluate_hand_with_joker
from app.game.events import EFFECTS, event_catalog, event_due
from app.models import LobbySettings, Room


def cards(*values):
    return [Card.parse(value) for value in values]


def make_engine(count=3, stack=1000, seed=1):
    return HoldemEngine([EnginePlayer(f"p{index}", stack) for index in range(count)], rng=random.Random(seed))


def check_down(engine):
    while not engine.hand_over:
        player = engine.players[engine.current_index]
        engine.act(player.player_id, "check" if engine.legal_actions()["check"] else "call")


def rig_showdown(engine, event_id, holes, board):
    engine.start_hand(event_id)
    for player, hole in zip(engine.players, holes):
        player.hole_cards = cards(*hole)
    check_down(engine)
    engine.board[:] = cards(*board)
    return engine.settle_showdown()


def make_room(count=3, **settings):
    room = Room(room_id="ev", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(**settings))
    for index in range(count):
        room.add_player(name=f"P{index + 1}", agreed_to_terms=True)
    return room


def test_catalog_comes_from_data_file_and_every_event_is_implemented():
    catalog = event_catalog()
    assert set(catalog) == {"mirror", "double_stakes", "spy", "carousel", "blind_flop", "wealth_tax", "joker"}
    assert set(catalog) <= set(EFFECTS)
    assert all(entry["title"] and entry["description"] for entry in catalog.values())


def test_mirror_worst_hand_wins():
    engine = make_engine(2)
    winnings = rig_showdown(engine, "mirror", [("As", "Ad"), ("7c", "2d")], ("Kh", "9s", "5c", "4h", "3d"))
    assert list(winnings) == ["p1"]


def test_without_event_best_hand_wins():
    engine = make_engine(2)
    winnings = rig_showdown(engine, None, [("As", "Ad"), ("7c", "2d")], ("Kh", "9s", "5c", "4h", "3d"))
    assert list(winnings) == ["p0"]


def test_double_stakes_doubles_blinds_only_for_one_hand():
    engine = make_engine(3)
    engine.start_hand("double_stakes")
    assert sum(player.committed for player in engine.players) == 60
    assert engine.legal_actions()["min_raise_to"] == 80
    while not engine.hand_over:
        engine.act(engine.players[engine.current_index].player_id, "fold")
    engine.start_next_hand()
    assert (engine.small_blind, engine.big_blind) == (10, 20)


def test_spy_reveals_a_real_card_of_an_active_player():
    engine = make_engine(3)
    engine.start_hand("spy")
    spy = engine.event_state["spy"]
    owner = engine.player(spy["player_id"])
    assert spy["card"] == str(owner.hole_cards[spy["card_index"]])


def test_carousel_passes_chosen_cards_to_the_left_and_blocks_betting():
    engine = make_engine(3)
    engine.start_hand("carousel")
    before = {player.player_id: list(player.hole_cards) for player in engine.players}
    current = engine.players[engine.current_index].player_id
    with pytest.raises(ValueError, match="Waiting"):
        engine.act(current, "call")

    engine.pick_carousel("p0", 0)
    engine.pick_carousel("p1", 1)
    assert engine.carousel_waiting() == ["p2"]
    engine.resolve_carousel()  # p2 gets a random pick

    assert before["p0"][0] in engine.player("p1").hole_cards
    assert before["p1"][1] in engine.player("p2").hole_cards
    assert all(len(player.hole_cards) == 2 for player in engine.players)
    all_before = {card for hole in before.values() for card in hole}
    assert {card for player in engine.players for card in player.hole_cards} == all_before
    engine.act(current, "call")


def test_blind_flop_hides_flop_until_showdown():
    engine = make_engine(2)
    engine.start_hand("blind_flop")
    engine.act(engine.players[engine.current_index].player_id, "call")
    engine.act(engine.players[engine.current_index].player_id, "check")
    assert engine.street == Street.FLOP
    assert engine.visible_board() == [None, None, None]
    check_down(engine)
    engine.settle_showdown()
    assert None not in engine.visible_board()


def test_blind_flop_stays_hidden_when_hand_ends_without_showdown():
    engine = make_engine(2)
    engine.start_hand("blind_flop")
    engine.act(engine.players[engine.current_index].player_id, "call")
    engine.act(engine.players[engine.current_index].player_id, "check")
    engine.act(engine.players[engine.current_index].player_id, "fold")
    engine.settle_showdown()
    assert engine.visible_board() == [None, None, None]


def test_wealth_tax_moves_ten_percent_from_richest_to_poorest():
    engine = HoldemEngine([EnginePlayer("rich", 1005), EnginePlayer("mid", 500), EnginePlayer("poor", 100)])
    engine.start_hand("wealth_tax")
    assert engine.event_state["wealth_tax"] == {"from_id": "rich", "to_id": "poor", "amount": 100}
    total = {player.player_id: player.stack + player.committed for player in engine.players}
    assert total == {"rich": 905, "mid": 500, "poor": 200}


def test_joker_card_is_used_as_the_best_card():
    hand = cards("As", "Ad", "Ah", "7c", "2d", "9h", "4s")
    assert evaluate_hand_with_joker(hand, Card.parse("7c"))[0] == FOUR_OF_A_KIND
    flush = cards("2h", "7h", "9h", "Jh", "3c", "8d", "Kc")
    assert evaluate_hand_with_joker(flush, Card.parse("3c"))[0] == FLUSH
    assert evaluate_hand_with_joker(hand, None)[0] != FOUR_OF_A_KIND


def test_joker_event_changes_the_winner_and_is_public_only_after_reveal():
    engine = make_engine(2, seed=3)
    engine.start_hand("joker")
    engine.joker_index = 4  # the river becomes the joker
    assert engine.joker_card() is None
    for player, hole in zip(engine.players, [("Kd", "Kc"), ("Ah", "3h")]):
        player.hole_cards = cards(*hole)
    check_down(engine)
    engine.board[:] = cards("Ks", "2h", "4h", "9c", "7s")
    assert engine.joker_card() == Card.parse("7s")
    # Without the joker: trip kings beat ace high. With it: kings make quads,
    # but A-2-3-4 of hearts + joker (5h) is a straight flush.
    winnings = engine.settle_showdown()
    assert list(winnings) == ["p1"]


def test_chaos_schedule():
    assert [event_due("light", hand) for hand in range(1, 7)] == [False, False, True, False, False, True]
    assert [event_due("medium", hand) for hand in range(1, 5)] == [False, True, False, True]
    assert all(event_due("full", hand) for hand in range(1, 5))


def test_room_reveals_event_then_deals_after_veto_window():
    room = make_room(chaos_level="full")
    room.start_game()
    assert room.phase == "event_reveal"
    assert room.engine.hand_started is False
    assert room.pop_notices()[0][0] == "event_revealed"
    with pytest.raises(ValueError, match="Betting is not open"):
        room.apply_player_action("player_1", "fold")

    assert room.process_timers(time.monotonic() + 1) is False
    assert room.process_timers(time.monotonic() + 9) is True
    assert room.engine.hand_started is True
    assert room.phase in {"playing", "carousel"}
    assert room.engine.event.event_id == room.current_event["id"]


def test_veto_only_inside_window():
    room = make_room(chaos_level="light")
    room.players[0].abilities = ["veto"]
    room.start_game()
    assert room.phase == "playing"  # light: no event on hand 1
    with pytest.raises(ValueError, match="veto window"):
        room.use_ability("player_1", "veto")


def test_dealer_event_overrides_schedule_and_is_hidden_until_reveal():
    room = make_room(chaos_level="light")
    room.select_event("mirror")
    assert room.view_for("host")["event"]["id"] == "none"
    room.start_game()
    assert room.current_event["id"] == "mirror"
    assert room.view_for("player", "player_2")["event"]["title"] == "Зеркало"


def test_room_carousel_resolves_when_everybody_picked_or_by_timer():
    room = make_room(chaos_level="light")
    room.select_event("carousel")
    room.start_game()
    room.process_timers(time.monotonic() + 9)
    assert room.phase == "carousel"
    assert room.view_for("host")["game"]["current_player_id"] is None
    room.pick_carousel("player_1", 0)
    assert room.view_for("player", "player_1")["you"]["carousel_pick"] == 0
    assert room.process_timers(time.monotonic() + 16) is True
    assert room.phase == "playing"
    assert room.view_for("host")["game"]["current_player_id"] is not None


def test_blind_flop_is_never_sent_to_clients():
    room = make_room(2, chaos_level="light")
    room.select_event("blind_flop")
    room.start_game()
    room.process_timers(time.monotonic() + 9)
    engine = room.engine
    room.apply_player_action(engine.players[engine.current_index].player_id, "call")
    room.apply_player_action(engine.players[engine.current_index].player_id, "check")
    flop = [str(card) for card in engine.board[:3]]
    for role, player_id in (("host", None), ("dealer", None), ("player", "player_1"), ("player", "player_2")):
        state = room.view_for(role, player_id)
        assert state["game"]["board"] == [None, None, None]
        own = set(state.get("you", {}).get("hole_cards", []))
        dumped = json.dumps(state)
        assert all(f'"{card}"' not in dumped for card in flop if card not in own)
