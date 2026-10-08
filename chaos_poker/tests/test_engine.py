import pytest

from app.game.cards import Card
from app.game.engine import EnginePlayer, HoldemEngine, Street


def make_engine(count=3, stack=200):
    return HoldemEngine([EnginePlayer(f"p{index}", stack) for index in range(count)], deck=None)


def test_start_hand_deals_hole_cards_and_blinds():
    engine = make_engine()
    engine.start_hand()

    assert engine.street == Street.PREFLOP
    assert all(len(player.hole_cards) == 2 for player in engine.players)
    assert sum(player.committed for player in engine.players) == 30
    assert engine.current_index is not None


def test_turn_validation_and_minimum_raise():
    engine = make_engine(2)
    engine.start_hand()
    current = engine.players[engine.current_index]
    with pytest.raises(ValueError, match="not this player's turn"):
        engine.act("not-current", "fold")
    with pytest.raises(ValueError, match="Invalid raise"):
        engine.act(current.player_id, "raise", amount=engine.current_bet)


def test_check_call_actions_advance_to_flop():
    engine = make_engine(2)
    engine.start_hand()
    first = engine.players[engine.current_index]
    engine.act(first.player_id, "call")
    second = engine.players[engine.current_index]
    engine.act(second.player_id, "check")

    assert engine.street == Street.FLOP
    assert len(engine.board) == 3


def test_all_in_players_reach_showdown_and_create_pot():
    engine = make_engine(2, stack=40)
    engine.start_hand()
    first = engine.players[engine.current_index]
    engine.act(first.player_id, "all_in")
    second = engine.players[engine.current_index]
    engine.act(second.player_id, "all_in")

    assert engine.street == Street.SHOWDOWN
    assert len(engine.pots) == 1
    assert engine.pots[0].amount == 80


def test_showdown_distributes_side_pots_to_best_eligible_hands():
    engine = make_engine(3, stack=0)
    engine.players[0].hole_cards = []
    engine.players[1].hole_cards = []
    engine.players[2].hole_cards = []
    engine.players[0].committed = 100
    engine.players[1].committed = 50
    engine.players[2].committed = 200
    engine.players[0].hole_cards = [Card.parse(value) for value in ("As", "Ad")]
    engine.players[1].hole_cards = [Card.parse(value) for value in ("Ks", "Kd")]
    engine.players[2].hole_cards = [Card.parse(value) for value in ("2s", "2d")]
    engine.board = [Card.parse(value) for value in ("Ac", "7h", "4c", "9d", "3s")]
    engine.street = Street.SHOWDOWN

    winnings = engine.settle_showdown()

    assert winnings == {"p0": 250, "p2": 100}
    assert engine.players[0].stack == 250


def test_next_hand_rotates_button_and_raises_blinds():
    engine = HoldemEngine([EnginePlayer(f"p{index}", 100) for index in range(2)], blind_interval_hands=1)
    engine.start_hand()
    engine.street = Street.COMPLETE
    engine.start_next_hand()

    assert engine.dealer_index == 1
    assert (engine.small_blind, engine.big_blind) == (20, 40)


def test_blinds_stay_until_interval_is_reached():
    engine = make_engine(3, stack=1000)
    engine.start_hand()
    for _ in range(7):
        engine.act(engine.players[engine.current_index].player_id, "fold")
        engine.act(engine.players[engine.current_index].player_id, "fold")
        engine.start_next_hand()
        assert engine.big_blind == 20
    engine.act(engine.players[engine.current_index].player_id, "fold")
    engine.act(engine.players[engine.current_index].player_id, "fold")
    engine.start_next_hand()

    assert (engine.small_blind, engine.big_blind) == (20, 40)


def test_fold_awards_pot_to_last_player():
    engine = make_engine(2, stack=1000)
    engine.start_hand()
    folder = engine.players[engine.current_index]
    engine.act(folder.player_id, "fold")

    assert engine.street == Street.COMPLETE
    winnings = engine.settle_showdown()
    winner = next(player for player in engine.players if player is not folder)
    assert winnings == {winner.player_id: 30}
    assert winner.stack == 1010
    assert folder.stack == 990
    assert engine.went_to_showdown is False


def test_player_facing_all_in_gets_to_respond():
    engine = HoldemEngine([EnginePlayer("short", 100), EnginePlayer("deep", 1000)])
    engine.start_hand()
    assert engine.players[engine.current_index].player_id == "short"
    engine.act("short", "all_in")

    assert engine.street == Street.PREFLOP
    assert engine.players[engine.current_index].player_id == "deep"
    engine.act("deep", "call")
    assert engine.street == Street.SHOWDOWN
    assert len(engine.board) == 5


def test_chips_are_conserved_over_many_random_hands():
    import random

    rng = random.Random(7)
    engine = HoldemEngine([EnginePlayer(f"p{index}", 500) for index in range(4)], rng=rng, blind_interval_hands=3)
    engine.start_hand()
    for _ in range(60):
        while not engine.hand_over:
            player = engine.players[engine.current_index]
            legal = engine.legal_actions()
            choices = [kind for kind in ("fold", "check", "call", "raise", "all_in") if legal[kind]]
            engine.act(player.player_id, rng.choice(choices))
        engine.settle_showdown()
        assert sum(player.stack for player in engine.players) == 2000
        if len(engine.seated()) < 2 or sum(player.stack > 0 for player in engine.players) < 2:
            break
        engine.start_next_hand()


def test_busted_player_is_skipped_in_next_hand():
    engine = make_engine(3, stack=1000)
    engine.start_hand()
    engine.players[2].stack = 0
    engine.street = Street.COMPLETE
    engine.settled = True
    engine.start_next_hand()

    assert engine.players[2].out is True
    assert engine.players[2].hole_cards == []
    assert engine.players[2].folded is True
    assert engine.players[2].committed == 0
    assert sum(player.committed for player in engine.players) == 30


def test_frozen_player_cannot_raise():
    engine = make_engine(2)
    engine.start_hand()
    current = engine.players[engine.current_index]
    engine.frozen_players.add(current.player_id)

    assert engine.legal_actions()["raise"] is False
    with pytest.raises(ValueError):
        engine.act(current.player_id, "raise", amount=60)
