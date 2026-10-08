"""Prompts for the AI dealer (section 15.6).

One system message per request: the role, safety limits (9.2), spiciness from the lobby and a short
public game context. Hole cards and photos are never put into prompts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import Room

SPICINESS = {
    "mild": "мягкая: добрые, безобидные шутки и задания",
    "medium": "средняя: можно подкалывать, но без грубости",
    "spicy": "острая: дерзко и смешно, но без оскорблений и пошлости",
}

ROLE = (
    "Ты — весёлый судья-комментатор за покерным столом в компании друзей (игра Chaos Poker, "
    "техасский холдем с картами событий). Пиши по-русски, коротко и живо. "
    "Ты не меняешь правила и не придумываешь новые механики: выбираешь только из того, что тебе дали."
)

SAFETY = (
    "Ограничения для любых заданий: только то, что выполнимо в обычной квартире; "
    "никаких опасных действий (высота, огонь, острые предметы, алкоголь, еда на скорость), "
    "не выходить из квартиры, не просить чужих личных данных, не унижать и не сексуализировать. "
    "Обращайся к игрокам по имени в именительном падеже, не угадывай их пол."
)


def system_prompt(room: Room) -> str:
    level = SPICINESS.get(room.settings.task_spiciness, SPICINESS["medium"])
    return f"{ROLE}\n{SAFETY}\nОстрота: {level}.\n\nКонтекст игры:\n{game_context(room)}"


def game_context(room: Room) -> str:
    """Public facts only: stacks, blinds, recent results, event, decree. No hole cards."""
    lines = []
    engine = room.engine
    if engine is not None:
        lines.append(f"Раздача №{engine.hands_played + 1}, блайнды {engine.small_blind}/{engine.big_blind}.")
    stacks = ", ".join(f"{player.name}: {player.stack}" for player in room.players)
    lines.append(f"Стеки: {stacks}.")
    names = {player.id: player.name for player in room.players}
    for hand in room.hand_log[-3:]:
        winners = ", ".join(f"{names.get(pid, pid)} +{amount}" for pid, amount in hand["winnings"].items())
        how = "на вскрытии" if hand["showdown"] else "без вскрытия"
        event = f", событие «{hand['event']}»" if hand.get("event") else ""
        lines.append(f"Раздача №{hand['hand_number']}: {winners} ({how}{event}).")
    if room.current_event:
        lines.append(f"Текущее событие: {room.current_event['title']}.")
    if room.active_decree:
        lines.append(f"Действует указ: {room.active_decree['title']}.")
    return "\n".join(lines)


def comment_prompt(room: Room, hand: dict[str, Any]) -> str:
    names = {player.id: player.name for player in room.players}
    winners = ", ".join(f"{names.get(pid, pid)} (+{amount})" for pid, amount in hand["winnings"].items())
    how = "на шоудауне" if hand["showdown"] else "без вскрытия: остальные сбросили"
    return (f"Только что закончилась раздача №{hand['hand_number']}: выиграл(и) {winners} {how}. "
            "Прокомментируй в 1–2 предложениях как спортивный комментатор, слегка подколи игроков. "
            "Без кавычек и без хэштегов.")


EVENT_SCHEMA = {
    "type": "object",
    "properties": {"event_id": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["event_id", "explanation"],
}


def event_prompt(catalog: dict[str, dict[str, Any]]) -> str:
    options = "\n".join(f"- {event_id}: {entry['title']} — {entry['description']}" for event_id, entry in catalog.items())
    return ("Выбери Карту События для следующей раздачи СТРОГО из списка (поле event_id — идентификатор из списка) "
            "и добавь короткое пояснение (1 предложение), почему именно оно подходит ситуации за столом.\n"
            f"{options}\nОтветь JSON: {{\"event_id\": ..., \"explanation\": ...}}")


TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "time_limit_sec": {"type": "integer"},
        "camera": {"type": "string", "enum": ["front", "back"]},
        "difficulty": {"type": "integer", "enum": [1, 2, 3]},
    },
    "required": ["title", "description", "time_limit_sec", "camera", "difficulty"],
}


def task_prompt(room: Room, player_name: str, kind: str, photo: bool, difficulty: str) -> str:
    what = "штраф" if kind == "penalty" else "бонусное задание за награду в фишках"
    if photo:
        body = ("Придумай фото-задание: найти предмет или сделать действие в квартире и сфотографировать это. "
                "camera: front для селфи, back для обычного снимка.")
    else:
        body = "Придумай короткое текстовое задание (сказать, изобразить, сделать что-то за столом). camera: back."
    return (f"Игрок {player_name} получает {what}. {body} Сложность: {difficulty}. "
            "title — до 60 символов, description — 1–2 предложения, time_limit_sec от 30 до 180.\n"
            "Ответь JSON: {\"title\", \"description\", \"time_limit_sec\", \"camera\", \"difficulty\"}.")


BRIBE_SCHEMA = {
    "type": "object",
    "properties": {"accepted": {"type": "boolean"}, "effect": {"type": "string"}, "comment": {"type": "string"}},
    "required": ["accepted", "effect", "comment"],
}

WISHES = {"hint": "подсказку о текущей раздаче", "change_event": "замену события на следующую раздачу"}


def bribe_prompt(player_name: str, amount: int, big_blind: int, wish: str) -> str:
    return (f"Игрок {player_name} предлагает тебе взятку {amount} фишек (это {amount / big_blind:.1f} большого блайнда) "
            f"и хочет {WISHES.get(wish, wish)}. Ты не обязан соглашаться: мелкие взятки обычно отвергай, "
            "щедрые можешь принять. Фишки в любом случае уйдут в банк следующей раздачи. "
            "Ответь JSON: {\"accepted\": true|false, \"effect\": \"" + wish + "\", \"comment\": одно остроумное предложение}.")


PROTOCOL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "nominations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "player": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["title", "player", "reason"],
            },
        },
    },
    "required": ["summary", "nominations"],
}


def protocol_prompt(stats_lines: list[str]) -> str:
    return ("Игра окончена. Составь итоговый протокол вечера: summary — 2–3 предложения, и 3–5 шуточных номинаций "
            "(например «Главный блефер», «Самый удачливый», «Главная жертва судьбы»). "
            "player — имя игрока точно как в статистике. Статистика:\n" + "\n".join(stats_lines) +
            "\nОтветь JSON: {\"summary\", \"nominations\": [{\"title\", \"player\", \"reason\"}]}.")
