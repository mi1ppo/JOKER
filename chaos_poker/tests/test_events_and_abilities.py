from app.game.cards import Card
from app.game.abilities import ability_catalog, ability_lookup
from app.game.engine import EnginePlayer, HoldemEngine
from app.game.events import event_catalog


def test_known_events_registry_is_available():
    catalog = event_catalog()

    assert "mirror" in catalog
    assert catalog["mirror"]["title"] == "Зеркало"
    assert "double_stakes" in catalog
    assert catalog["double_stakes"]["weight"] > 0


def test_double_stakes_event_multiplies_blinds_and_raise_bounds():
    engine = HoldemEngine([EnginePlayer("a", 1000), EnginePlayer("b", 1000)])
    engine.start_hand("double_stakes")

    assert (engine.small_blind, engine.big_blind) == (20, 40)
    assert engine.legal_actions()["min_raise_to"] == 80


def test_ability_catalog_has_required_abilities():
    abilities = ability_catalog()

    assert {"peek", "redraw", "veto", "freeze", "steal"}.issubset(abilities)
    assert ability_lookup("peek")["title"] == "Подглядеть"


def test_room_assigns_starting_abilities_to_players_on_game_start():
    from app.models import Room

    room = Room(
        room_id="ability_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)

    room.start_game()

    assert len(room.players[0].abilities) == 2
    assert len(room.players[1].abilities) == 2
    assert all(ability in ability_catalog() for ability in room.players[0].abilities)
    assert "abilities" not in room.host_view()["players"][0]
    assert room.view_for("player", "player_1")["you"]["abilities"] == room.players[0].abilities


def test_room_can_use_peek_ability_to_reveal_one_target_card():
    from app.models import Room

    room = Room(
        room_id="peek_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.players[0].abilities = ["peek"]
    room.start_game()
    room.engine.players[1].hole_cards = [Card.parse("Qh"), Card.parse("7c")]

    result = room.use_ability("player_1", "peek", target_id="player_2")

    assert result["ability"] == "peek"
    assert result["target_id"] == "player_2"
    assert result["revealed_card"] in {"Qh", "7c"}
    assert "peek" not in room.players[0].abilities


def test_room_can_redraw_and_freeze_target_for_current_round():
    from app.models import Room

    room = Room(
        room_id="ability_round_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.players[0].abilities = ["redraw"]
    room.players[1].abilities = ["freeze"]
    room.start_game()

    redraw_result = room.use_ability("player_1", "redraw", index=0)
    freeze_result = room.use_ability("player_2", "freeze", target_id="player_1")

    assert redraw_result["ability"] == "redraw"
    assert redraw_result["replaced_index"] == 0
    assert redraw_result["new_card"]
    assert "redraw" not in room.players[0].abilities
    assert freeze_result["ability"] == "freeze"
    assert freeze_result["target_id"] == "player_1"
    assert "freeze" not in room.players[1].abilities


def test_room_can_veto_event_and_steal_stack_from_target():
    from app.models import Room

    room = Room(
        room_id="final_ability_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.players[0].abilities = ["veto", "steal"]
    room.players[1].stack = 200
    room.players[0].stack = 100
    room.select_event("double_stakes")
    room.start_game()
    assert room.phase == "event_reveal"

    veto_result = room.use_ability("player_1", "veto")
    steal_result = room.use_ability("player_1", "steal", target_id="player_2")

    assert veto_result["ability"] == "veto"
    assert room.current_event is None
    assert room.phase == "playing"
    assert room.engine.big_blind == 20
    assert steal_result["ability"] == "steal"
    assert steal_result["target_id"] == "player_2"
    # Blinds 10/20 after the veto: Alice 90, Bob 180, 5% of 180 = 9.
    assert steal_result["stolen_amount"] == 9
    assert room.players[0].stack == 99
    assert room.players[1].stack == 171
    assert "veto" not in room.players[0].abilities
    assert "steal" not in room.players[0].abilities
