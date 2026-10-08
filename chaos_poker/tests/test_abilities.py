import time

import pytest

from app.game.abilities import MAX_IN_HAND, ability_catalog, draw_ability
from app.game.cards import Card
from app.game.engine import Street
from app.models import LobbySettings, Room


def make_room(count=3, chaos="medium", stack=1000):
    room = Room(room_id="ab", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(chaos_level=chaos, starting_stack=stack))
    for index in range(count):
        room.add_player(name=f"P{index + 1}", agreed_to_terms=True)
    room.start_game()
    while room.phase not in {"playing", "game_over"}:
        room.process_timers(time.monotonic() + 100)
    return room


def current(room):
    return room.engine.players[room.engine.current_index].player_id


def finish_hand_by_folds(room):
    while room.phase == "playing":
        room.apply_player_action(current(room), "fold")


def test_catalog_has_icons_and_descriptions():
    catalog = ability_catalog()
    assert set(catalog) == {"peek", "redraw", "veto", "freeze", "steal"}
    assert all(entry["icon"] and entry["description"] for entry in catalog.values())


def test_players_start_with_two_different_abilities():
    room = make_room()
    for player in room.players:
        assert len(player.abilities) == 2
        assert len(set(player.abilities)) == 2


def test_light_chaos_has_no_abilities_and_no_side_bets():
    room = make_room(chaos="light")
    assert all(player.abilities == [] for player in room.players)
    assert room.engine.side_bets_enabled is False
    assert room.engine.side_bet_offers == []


def test_draw_ability_respects_hand_limit():
    assert draw_ability(["peek", "veto", "steal"]) is None
    assert draw_ability(["peek", "veto"]) in {"redraw", "freeze", "steal"}
    assert MAX_IN_HAND == 3


def test_one_ability_every_five_hands_up_to_three():
    room = make_room()
    for player in room.players:
        player.abilities = ["peek"]
    for hand in range(1, 11):
        finish_hand_by_folds(room)
        expected = 1 + min(hand // 5, 2)
        assert all(len(player.abilities) == expected for player in room.players), hand
        room.start_next_hand()
        while room.phase not in {"playing", "game_over"}:
            room.process_timers(time.monotonic() + 100)


def test_knockout_gives_ability_to_the_biggest_winner():
    room = make_room(2, stack=100)
    for player in room.players:
        player.abilities = []
    engine = room.engine
    winner, loser = engine.players
    winner.hole_cards = [Card.parse("As"), Card.parse("Ad")]
    loser.hole_cards = [Card.parse("7c"), Card.parse("2d")]
    while room.phase == "playing":
        player = current(room)
        room.apply_player_action(player, "all_in" if room.engine.legal_actions()["all_in"] else "call")
    if room.players[0].stack == 0:  # the board can still beat aces; check whoever won
        winner, loser = loser, winner
    knocker = room.get_player(winner.player_id)
    assert len(knocker.abilities) == 1
    assert any("за выбивание" in line for line in room.feed)


def test_peek_reveals_one_of_target_cards_and_rejects_folded_target():
    room = make_room()
    engine = room.engine
    actor = current(room)
    room.get_player(actor).abilities = ["peek", "peek"]
    folded = actor
    room.apply_player_action(folded, "fold")
    viewer = current(room)
    room.get_player(viewer).abilities = ["peek"]
    with pytest.raises(ValueError, match="not in this hand"):
        room.use_ability(viewer, "peek", target_id=folded)
    target = next(player.player_id for player in engine.players if player.player_id not in {viewer, folded})
    result = room.use_ability(viewer, "peek", target_id=target)
    assert result["revealed_card"] in {str(card) for card in engine.player(target).hole_cards}
    assert "peek" in room.get_player(viewer).used_abilities


def test_redraw_replaces_card_with_one_from_the_deck_but_not_on_river():
    room = make_room()
    engine = room.engine
    actor = current(room)
    room.get_player(actor).abilities = ["redraw", "redraw"]
    old = engine.player(actor).hole_cards[1]
    top = engine.deck.cards[-1]
    result = room.use_ability(actor, "redraw", index=1)
    assert result["new_card"] == str(top)
    assert engine.player(actor).hole_cards[1] == top != old
    assert room.players[[p.id for p in room.players].index(actor)].hole_cards[1] == str(top)
    with pytest.raises(ValueError, match="one ability per betting round"):
        room.use_ability(actor, "redraw", index=0)
    engine.street = Street.RIVER
    room.ability_rounds.clear()
    with pytest.raises(ValueError, match="before the river"):
        room.use_ability(actor, "redraw", index=0)


def test_freeze_blocks_raise_for_the_current_round_only():
    room = make_room(2)
    engine = room.engine
    sb = current(room)
    bb = next(player.player_id for player in engine.players if player.player_id != sb)
    room.get_player(sb).abilities = ["freeze"]
    room.use_ability(sb, "freeze", target_id=bb)
    room.apply_player_action(sb, "call")
    assert engine.legal_actions()["raise"] is False
    with pytest.raises(ValueError):
        room.apply_player_action(bb, "raise", 60)
    room.apply_player_action(bb, "check")
    assert engine.street == Street.FLOP
    assert bb not in engine.frozen_players


def test_steal_takes_five_percent_but_not_from_all_in_or_on_river():
    room = make_room(3)
    engine = room.engine
    thief = current(room)
    victim = next(player for player in engine.players if player.player_id != thief)
    room.get_player(thief).abilities = ["steal", "steal"]
    before = victim.stack
    result = room.use_ability(thief, "steal", target_id=victim.player_id)
    assert result["stolen_amount"] == before // 20
    assert victim.stack == before - before // 20
    room.ability_rounds.clear()
    victim.all_in = True
    with pytest.raises(ValueError, match="all-in"):
        room.use_ability(thief, "steal", target_id=victim.player_id)
    victim.all_in = False
    engine.street = Street.RIVER
    with pytest.raises(ValueError, match="before the river"):
        room.use_ability(thief, "steal", target_id=victim.player_id)


def test_ability_cannot_be_used_twice_or_by_folded_player():
    room = make_room()
    actor = current(room)
    room.get_player(actor).abilities = ["steal"]
    target = next(player.id for player in room.players if player.id != actor)
    room.use_ability(actor, "steal", target_id=target)
    room.ability_rounds.clear()
    with pytest.raises(ValueError, match="does not have"):
        room.use_ability(actor, "steal", target_id=target)
    room.get_player(actor).abilities = ["steal"]
    room.apply_player_action(actor, "fold")
    with pytest.raises(ValueError, match="Folded"):
        room.use_ability(actor, "steal", target_id=target)


def test_used_abilities_are_private_to_the_owner():
    room = make_room()
    actor = current(room)
    room.get_player(actor).abilities = ["steal"]
    target = next(player.id for player in room.players if player.id != actor)
    room.use_ability(actor, "steal", target_id=target)
    assert room.view_for("player", actor)["you"]["used_abilities"] == ["steal"]
    other = room.view_for("player", target)
    assert "used_abilities" not in other["players"][0]
    assert "used_abilities" not in other["you"] or other["you"]["used_abilities"] == []
