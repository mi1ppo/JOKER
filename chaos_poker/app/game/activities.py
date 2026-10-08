"""Physical activities catalog (section 9.3) and pure rules. No phone sensors are used:
results are confirmed by people (dealer, the players themselves or a vote)."""
from __future__ import annotations

from typing import Any

INTENSITIES = ("off", "light", "medium", "hard")

# kind "task": one player does it and it is confirmed like any task;
# kind "contest": several players at once, resolved by the dealer or by the participants.
_ACTIVITIES: dict[str, dict[str, Any]] = {
    "shake_battle": {
        "title": "Шейк-баттл",
        "icon": "📳",
        "kind": "contest",
        "players": 2,
        "duration_sec": 10,
        "description": "Двое 10 секунд изо всех сил трясут телефоном. Победителя выбирает судья, приз — бонусные фишки.",
    },
    "freeze": {
        "title": "Замри",
        "icon": "🧊",
        "kind": "contest",
        "players": "all",
        "duration_sec": 5,
        "description": "По сигналу все замирают на 5 секунд. Кто дёрнулся — платит 1 BB.",
    },
    "duel": {
        "title": "Дуэль",
        "icon": "✊",
        "kind": "contest",
        "players": 2,
        "duration_sec": 30,
        "description": "Камень-ножницы-бумага на фишки: выберите жест на телефоне. Победитель забирает ставку.",
    },
    "squats": {
        "title": "Приседания",
        "icon": "🏋️",
        "kind": "task",
        "description": "Штраф: сделай {reps} приседаний (или {pushups} отжиманий).",
    },
    "standing_table": {
        "title": "Стоячий стол",
        "icon": "🧍",
        "kind": "task",
        "description": "Сыграй следующую раздачу стоя.",
    },
}

# Intensity -> numbers for the activities.
SQUAT_REPS = {"light": 10, "medium": 20, "hard": 30}
PUSHUP_REPS = {"light": 5, "medium": 10, "hard": 15}
DUEL_STAKE_BB = {"light": 1, "medium": 1, "hard": 2}
SHAKE_PRIZE_BB = 2
FREEZE_FINE_BB = 1
SKIP_FEE_BB = 1  # any activity can be skipped for a small fee

DUEL_CHOICES = ("rock", "paper", "scissors")
DUEL_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
DUEL_MAX_ROUNDS = 3


def activity_catalog() -> dict[str, dict[str, Any]]:
    return {activity_id: dict(entry) for activity_id, entry in _ACTIVITIES.items()}


def activity_info(activity_type: str) -> dict[str, Any]:
    if activity_type not in _ACTIVITIES:
        raise ValueError(f"Unknown activity type: {activity_type}")
    return dict(_ACTIVITIES[activity_type])


def task_text(activity_type: str, intensity: str) -> str:
    info = activity_info(activity_type)
    return info["description"].format(reps=SQUAT_REPS.get(intensity, 20), pushups=PUSHUP_REPS.get(intensity, 10))


def duel_winner(first: str, second: str) -> int | None:
    """0 if the first choice wins, 1 if the second wins, None for a draw."""
    if first not in DUEL_CHOICES or second not in DUEL_CHOICES:
        raise ValueError("Unknown duel choice")
    if first == second:
        return None
    return 0 if DUEL_BEATS[first] == second else 1
