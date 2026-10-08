"""AI dealer: functions, fallbacks, privacy of prompts, bribes and the room integration (section 8.2)."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.dealer import prompts
from app.dealer.ai import AiDealer, is_safe
from app.dealer.llm import LLMUnavailable
from app.main import app, rooms
from app.models import LobbySettings, Room


class FakeLLM:
    """Records prompts; answers from a script (dict kind -> value or exception)."""

    def __init__(self, text="Отличная раздача!", json_answers=None):
        self.text = text
        self.json_answers = list(json_answers or [])
        self.prompts = []

    async def generate_text(self, system, user, *, priority="high", max_tokens=200, budget=None, **_):
        self.prompts.append(system + "\n" + user)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    async def generate_json(self, system, user, schema, *, priority="high", max_tokens=400, budget=None, model=None, **_):
        self.prompts.append(system + "\n" + user)
        answer = self.json_answers.pop(0) if self.json_answers else LLMUnavailable("no answer")
        if isinstance(answer, Exception):
            raise answer
        return model.model_validate(answer).model_dump() if model else answer


def make_room(count=3, mode="ai", chaos="medium"):
    room = Room(room_id="air", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(dealer_mode=mode, chaos_level=chaos))
    for name in ("Аня", "Боря", "Вика")[:count]:
        room.add_player(name=name, agreed_to_terms=True)
    room.start_game()
    while room.phase != "playing":
        room.process_timers(time.monotonic() + 100)
    return room


def finish_hand(room):
    while room.phase == "playing":
        room.apply_player_action(room.engine.players[room.engine.current_index].player_id, "fold")


# ---- privacy ----------------------------------------------------------------------------------


def test_prompts_never_contain_hole_cards():
    room = make_room()
    llm = FakeLLM(json_answers=[{"event_id": "mirror", "explanation": "Пора перевернуть всё!"}])
    dealer = AiDealer(llm)
    hole = {card for player in room.players for card in player.hole_cards}
    finish_hand(room)
    asyncio.run(dealer.comment(room, room.last_hand))
    asyncio.run(dealer.choose_event(room))
    for prompt in llm.prompts:
        assert not any(card in prompt for card in hole), prompt
    assert "Аня" in llm.prompts[0] and "Стеки" in llm.prompts[0]
    assert prompts.SAFETY in llm.prompts[0]


# ---- functions -----------------------------------------------------------------------------------


def test_event_only_from_catalog_with_explanation():
    room = make_room()
    dealer = AiDealer(FakeLLM(json_answers=[{"event_id": "mirror", "explanation": "Перевернём!"}]))
    assert asyncio.run(dealer.choose_event(room)) == ("mirror", "Перевернём!", "ai")
    dealer = AiDealer(FakeLLM(json_answers=[{"event_id": "teleport", "explanation": "Новая механика"}]))
    event_id, explanation, source = asyncio.run(dealer.choose_event(room))
    assert source == "fallback" and event_id != "teleport" and explanation is None


def test_generated_task_passes_safety_filter_or_falls_back():
    room = make_room()
    good = {"title": "Покажи кота", "description": "Изобрази кота 10 секунд", "time_limit_sec": 60,
            "camera": "back", "difficulty": 1}
    bad = {**good, "title": "Выйди на балкон", "description": "И крикни что-нибудь"}
    dealer = AiDealer(FakeLLM(json_answers=[good, bad]))
    task, source = asyncio.run(dealer.generate_task(room, "player_1", kind="penalty", photo=False, difficulty="mild"))
    assert source == "ai" and task["title"] == "Покажи кота"
    task, source = asyncio.run(dealer.generate_task(room, "player_1", kind="penalty", photo=False, difficulty="mild"))
    assert source == "bank" and task["title"] != "Выйди на балкон"
    assert not is_safe("Зажги свечу") and is_safe("Сфотографируй половину стола")


def test_comment_low_priority_falls_back_with_player_name():
    room = make_room()
    finish_hand(room)
    text, source = asyncio.run(AiDealer(FakeLLM(text=LLMUnavailable("busy"))).comment(room, room.last_hand))
    winner = room.get_player(next(iter(room.last_hand["winnings"]))).name
    assert source == "fallback" and winner in text


def test_protocol_from_ai_or_from_statistics():
    room = make_room()
    finish_hand(room)
    room.end_game()
    answer = {"summary": "Весёлый вечер", "nominations": [
        {"title": "Главный блефер", "player": "Аня", "reason": "блефовала"},
        {"title": "Призрак", "player": "Никто", "reason": "его не было"},
    ]}
    protocol, source = asyncio.run(AiDealer(FakeLLM(json_answers=[answer])).protocol(room))
    assert source == "ai" and [item["player"] for item in protocol["nominations"]] == ["Аня"]
    protocol, source = asyncio.run(AiDealer(None).protocol(room))
    assert source == "fallback" and protocol["nominations"]
    assert {item["player"] for item in protocol["nominations"]} <= {"Аня", "Боря", "Вика"}


# ---- room hooks ------------------------------------------------------------------------------------


def test_ai_mode_queues_comment_and_next_event_after_a_hand():
    room = make_room(chaos="full")
    room.pop_ai_requests()
    finish_hand(room)
    requests = room.pop_ai_requests()
    assert [request["kind"] for request in requests] == ["comment", "event"]
    assert requests[1]["for_hand"] == 2
    assert room.apply_ai_event("spy", "Посмотрим, кто что прячет", 2) is True
    room.start_next_hand()
    assert room.current_event["id"] == "spy"
    assert room.current_event["ai_explanation"] == "Посмотрим, кто что прячет"
    revealed = [payload for kind, payload in room.pop_notices() if kind == "event_revealed"]
    assert revealed[-1]["explanation"] == "Посмотрим, кто что прячет"


def test_late_ai_event_is_ignored():
    room = make_room(chaos="full")
    finish_hand(room)
    room.start_next_hand()
    assert room.apply_ai_event("spy", None, 2) is False


def test_human_mode_does_not_use_ai():
    room = make_room(mode="human")
    finish_hand(room)
    assert room.pop_ai_requests() == []


def test_end_of_game_requests_protocol_and_has_statistics():
    room = make_room()
    finish_hand(room)
    room.end_game()
    assert {"kind": "protocol"} in room.pop_ai_requests()
    stats = {row["name"]: row for row in room.results["stats"]}
    assert sum(row["hands_played"] for row in stats.values()) == 3
    assert sum(row["hands_won"] for row in stats.values()) == 1
    room.set_protocol({"summary": "s", "nominations": []}, "ai")
    assert room.results["protocol"]["source"] == "ai"


# ---- bribes ----------------------------------------------------------------------------------------


def test_bribe_chips_go_to_next_pot_and_one_per_hand():
    room = make_room()
    before = room.get_player("player_1").stack
    bribe = room.start_bribe("player_1", 100, "hint")
    assert room.get_player("player_1").stack == before - 100
    assert room.engine.carryover == 100
    with pytest.raises(ValueError, match="One bribe"):
        room.start_bribe("player_1", 100, "hint")
    with pytest.raises(ValueError, match="starts at"):
        room.start_bribe("player_2", 5, "hint")
    assert room.pop_ai_requests()[-1] == {"kind": "bribe", "bribe_id": bribe["id"]}


def test_accepted_hint_is_private_and_reveals_only_a_suit():
    room = make_room()
    bribe = room.start_bribe("player_1", 100, "hint")
    room.pop_direct_notices()
    room.resolve_bribe(bribe["id"], {"accepted": True, "comment": "Так и быть"})
    notices = room.pop_direct_notices()
    assert len(notices) == 1 and notices[0][0] == "player_1"
    hint = notices[0][2]["bribe"]["hint"]
    assert "масти" in hint
    hole = {card for player in room.players for card in player.hole_cards}
    assert not any(card in hint for card in hole)


def test_accepted_change_event_replaces_next_event():
    room = make_room()
    room.select_event("mirror")
    bribe = room.start_bribe("player_2", 200, "change_event")
    room.resolve_bribe(bribe["id"], {"accepted": True, "comment": ""})
    assert room.pending_event["id"] != "mirror"
    assert room.pending_event["bribed_by"] == "player_2"


def test_rejected_bribe_keeps_chips_in_the_pot():
    room = make_room()
    bribe = room.start_bribe("player_3", 40, "change_event")
    room.resolve_bribe(bribe["id"], {"accepted": False, "comment": "Неподкупен"})
    assert bribe["status"] == "rejected" and room.engine.carryover == 40
    assert "Неподкупен" in room.feed[-1]


def test_bribes_only_with_ai_dealer():
    room = make_room(mode="human")
    with pytest.raises(ValueError, match="AI dealer"):
        room.start_bribe("player_1", 100, "hint")


# ---- server integration -------------------------------------------------------------------------------


def test_server_runs_ai_requests_in_background(monkeypatch):
    llm = FakeLLM(text="Аня жжёт!", json_answers=[{"accepted": True, "effect": "hint", "comment": "Ладно"}])
    monkeypatch.setattr(main, "ai", AiDealer(llm))
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    client.post(f"/api/rooms/{created['room_id']}/settings?host_token={created['host_token']}",
                json={"dealer_mode": "ai", "chaos_level": "light"})
    players = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("Аня", "Боря")
    ]
    client.post(f"/api/rooms/{created['room_id']}/start", json={"host_token": created["host_token"]})
    room = rooms.get_room(created["room_id"])
    current = next(item for item in players
                   if item["player_id"] == room.engine.players[room.engine.current_index].player_id)

    def wait_for(ws, predicate):
        for _ in range(30):
            message = ws.receive_json()
            if predicate(message):
                return message
        raise AssertionError("message not received")

    with client.websocket_connect(f"/ws?token={current['session_token']}") as ws:
        wait_for(ws, lambda m: m["type"] == "state")
        ws.send_json({"type": "bribe", "payload": {"amount": 100, "wish": "hint"}})
        notice = wait_for(ws, lambda m: m["type"] == "private_notice")
        assert notice["payload"]["bribe"]["accepted"] is True
        ws.send_json({"type": "action", "payload": {"kind": "fold"}})
        comment = wait_for(ws, lambda m: m["type"] == "dealer_comment")
    assert comment["payload"]["text"] == "Аня жжёт!"
    status = client.get("/api/ai/status", params={"host_token": created["host_token"]}).json()
    assert "usage" in status
    assert room.view_for("host")["ai"]["usage"]["limit"] == 0
    assert "ai" not in room.view_for("player", "player_1")
