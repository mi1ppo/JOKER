from app.game.cards import Card, Deck
from app.game.evaluator import (
    FLUSH, FOUR_OF_A_KIND, FULL_HOUSE, HIGH_CARD, PAIR, STRAIGHT,
    STRAIGHT_FLUSH, THREE_OF_A_KIND, TWO_PAIR, evaluate_strings,
)
from app.game.pots import build_side_pots, split_pot


def test_card_format_and_deck_are_valid():
    assert str(Card.parse("As")) == "As"
    deck = Deck()
    dealt = deck.deal_many(52)
    assert len({str(card) for card in dealt}) == 52


def test_all_hand_categories():
    cases = {
        HIGH_CARD: ["As", "Kd", "9c", "7h", "3s", "2d", "4c"],
        PAIR: ["As", "Ad", "9c", "7h", "3s", "2d", "4c"],
        TWO_PAIR: ["As", "Ad", "9c", "9h", "3s", "2d", "4c"],
        THREE_OF_A_KIND: ["As", "Ad", "Ac", "9h", "3s", "2d", "4c"],
        STRAIGHT: ["As", "Kd", "Qc", "Jh", "Ts", "2d", "4c"],
        FLUSH: ["As", "Js", "9s", "7s", "3s", "2d", "4c"],
        FULL_HOUSE: ["As", "Ad", "Ac", "9h", "9s", "2d", "4c"],
        FOUR_OF_A_KIND: ["As", "Ad", "Ac", "Ah", "9s", "2d", "4c"],
        STRAIGHT_FLUSH: ["As", "Ks", "Qs", "Js", "Ts", "2d", "4c"],
    }
    for category, cards in cases.items():
        assert evaluate_strings(cards)[0] == category


def test_evaluator_handles_wheel_and_tie_ordering():
    wheel = evaluate_strings(["As", "2d", "3c", "4h", "5s", "Kd", "Qc"])
    broadway = evaluate_strings(["As", "Kd", "Qc", "Jh", "Ts", "2d", "4c"])
    assert wheel == (STRAIGHT, 5)
    assert broadway > wheel


def test_side_pots_and_odd_chip_order():
    pots = build_side_pots({"a": 100, "b": 50, "c": 200})
    assert [(pot.amount, pot.eligible) for pot in pots] == [
        (150, ("a", "b", "c")),
        (100, ("a", "c")),
        (100, ("c",)),
    ]
    assert split_pot(5, ["c", "a"], ["a", "b", "c"]) == {"a": 3, "c": 2}


def test_side_bet_round_tracks_pool_and_statement_resolution():
    from app.game.engine import HoldemEngine, EnginePlayer

    engine = HoldemEngine([EnginePlayer("p0", 200), EnginePlayer("p1", 200)])
    engine.start_hand()
    engine.side_bet_offers = [{"id": "pair_on_board", "kind": "pair_on_board"}, {"id": "showdown", "kind": "showdown"}]

    engine.place_side_bet("p0", "pair_on_board")
    engine.place_side_bet("p1", "pair_on_board")

    assert engine.side_bet_pool == 40
    assert engine.side_bets["p0"] == "pair_on_board"

    engine.act(engine.players[engine.current_index].player_id, "fold")
    engine.board = [Card.parse(value) for value in ("As", "Ac", "9d")]
    engine.settle_showdown()

    assert engine.side_bet_results["winner_ids"] == ["p0", "p1"]
    assert engine.side_bet_results["pool"] == 40
    assert sum(player.stack for player in engine.players) == 400
