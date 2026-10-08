from app.game.engine import HoldemEngine, EnginePlayer, Street


def test_check_flow_transitions_to_flop():
    players = [
        EnginePlayer(player_id="1", stack=1000),
        EnginePlayer(player_id="2", stack=1000),
    ]
    engine = HoldemEngine(players)
    engine.start_hand()

    # Pre-flop: Big Blind (1) acted. Small Blind (0) needs to act.
    # SB (0) calls to match BB
    engine.act("1", "call")
    # BB (1) checks
    engine.act("2", "check")

    # Должны перейти на флоп
    assert engine.street == Street.FLOP
    assert engine.current_index is not None
