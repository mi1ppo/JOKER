from app.game.engine import HoldemEngine, EnginePlayer, Street


def test_fold_and_check_game_flow():
    players = [
        EnginePlayer(player_id="p1", stack=1000),
        EnginePlayer(player_id="p2", stack=1000),
        EnginePlayer(player_id="p3", stack=1000),
    ]
    engine = HoldemEngine(players, dealer_index=0)
    engine.start_hand()

    assert engine.current_index is not None

    # Первый игрок делает fold
    first_player_id = engine.players[engine.current_index].player_id
    engine.act(first_player_id, "fold")

    assert engine.current_index is not None
    assert engine.street == Street.PREFLOP

    # Второй игрок делает call
    second_player_id = engine.players[engine.current_index].player_id
    legal = engine.legal_actions()
    if legal["call"]:
        engine.act(second_player_id, "call")
    elif legal["check"]:
        engine.act(second_player_id, "check")

    assert engine.current_index is not None

    # Третий игрок делает check (так как он на BB и ставка не росла выше него)
    third_player_id = engine.players[engine.current_index].player_id
    legal = engine.legal_actions()
    if legal["check"]:
        engine.act(third_player_id, "check")
    elif legal["call"]:
        engine.act(third_player_id, "call")

    # Теперь префлоп завершен, и мы должны перейти на флоп
    assert engine.street == Street.FLOP
