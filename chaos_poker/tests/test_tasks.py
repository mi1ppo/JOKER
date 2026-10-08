import time

from app.models import Room


def test_room_can_assign_text_and_photo_tasks():
    room = Room(
        room_id="task_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)

    text_task = room.issue_task("player_1", task_type="text", kind="penalty", difficulty="medium")
    photo_task = room.issue_task("player_1", task_type="photo", kind="bonus", difficulty="hard", camera="front")

    assert text_task["task_type"] == "text"
    assert photo_task["camera"] == "front"
    assert room.active_task["id"] == photo_task["id"]
    assert room.host_view()["tasks"][-1]["task_type"] == "photo"


def test_task_bank_has_fallback_entries_for_levels():
    from app.game.tasks import fallback_tasks

    tasks = fallback_tasks("medium", photo=False)
    photo_tasks = fallback_tasks("hard", photo=True)

    assert tasks
    assert photo_tasks
    assert all(entry.strip() for entry in tasks + photo_tasks)
    # Section 9: at least 40 text tasks over three levels and at least 25 photo tasks.
    text_total = sum(len(fallback_tasks(level, photo=False)) for level in ("mild", "medium", "spicy"))
    assert text_total >= 40
    assert all(fallback_tasks(level, photo=False) for level in ("mild", "medium", "spicy"))
    assert len(fallback_tasks("spicy", photo=True)) >= 25


def test_room_tracks_physical_activities_and_mode():
    room = Room(
        room_id="activity_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.set_activity_mode("medium")
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.start_activity("shake_battle", players=["player_1", "player_2"])

    assert room.activity_mode == "medium"
    assert room.active_activity["type"] == "shake_battle"
    assert room.host_view()["activity"]["type"] == "shake_battle"


def test_room_can_generate_ai_comment_and_toggle_physical_mode():
    room = Room(
        room_id="ai_activity_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.set_dealer_mode("ai")
    room.set_physical_activity("medium")
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.start_activity("duel", players=["player_1", "player_2"])

    room.add_dealer_comment("Alice made a strong bluff")

    assert room.settings.dealer_mode == "ai"
    assert room.settings.physical_activity == "medium"
    assert room.active_activity["type"] == "duel"
    assert room.last_comment["text"] == "Alice made a strong bluff"
    assert ("dealer_comment", {"text": "Alice made a strong bluff"}) in room.pop_notices()


def test_room_tracks_photo_submission_and_dealer_review():
    room = Room(
        room_id="photo_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)

    task = room.issue_task("player_1", task_type="photo", kind="bonus", difficulty="hard", camera="front")
    room.submit_photo_task("player_1", "https://example.test/photo.jpg")

    assert room.active_task["status"] == "pending_review"
    assert room.active_task["submission"]["url"] == "https://example.test/photo.jpg"

    room.review_task(task["id"], approved=True)

    assert room.active_task["status"] == "approved"
    assert room.host_view()["tasks"][-1]["status"] == "approved"


def test_room_can_select_current_event_for_the_hand():
    room = Room(
        room_id="event_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.set_dealer_mode("human")
    event = room.select_event("double_stakes")

    assert event["id"] == "double_stakes"
    assert room.pending_event["id"] == "double_stakes"
    # The queued event is a surprise: only the dealer sees it before the reveal.
    assert room.host_view()["event"]["id"] == "none"
    assert room.view_for("dealer")["next_event"]["title"] == "Двойной кон"


def test_room_applies_selected_event_to_engine_for_the_hand():
    room = Room(
        room_id="event_engine_room",
        host_name="Host",
        host_token="host_token",
        dealer_token="dealer_token",
        player_token="player_token",
    )
    room.add_player(name="Alice", agreed_to_terms=True)
    room.add_player(name="Bob", agreed_to_terms=True)
    room.select_event("double_stakes")

    room.start_game()

    assert room.current_event["id"] == "double_stakes"
    assert room.phase == "event_reveal"
    assert room.host_view()["event"]["title"] == "Двойной кон"
    room.process_timers(time.monotonic() + 9)
    assert room.phase == "playing"
    assert room.engine is not None
    assert room.engine.small_blind == 20
    assert room.engine.big_blind == 40
    assert room.engine.legal_actions()["min_raise_to"] == 80
