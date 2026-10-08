"""AI dealer functions (section 8.2) with fallback content for every one of them (15.6).

Every public coroutine returns a usable result even when GigaChat is unavailable:
LLMUnavailable (or anything else) means "use data/ content right away".
"""
from __future__ import annotations

import json
import logging
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.dealer import prompts
from app.dealer.llm import LLMClient, LLMUnavailable, TokenBudget
from app.game.events import event_catalog, random_event_id
from app.game.tasks import pick_task

if TYPE_CHECKING:
    from app.models import Room

log = logging.getLogger("chaos.ai")
DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "fallback_comments.json"

# Extra guard on generated tasks: anything that smells dangerous falls back to the bank.
# Word stems are matched at the start of a word ("вин" must not catch "половину").
UNSAFE_STEMS = re.compile(
    r"\b(огон|огн|поджиг|зажигалк|спичк|свеч|нож|лезви|игл|бритв|балкон|окн|подоконн|высот|крыш|"
    r"улиц|подъезд|выйд|выйт|выход[а-я]* из|алкогол|водк|пив|вин[оау]?\b|выпе|выпи|рюмк|таблет|лекарств|"
    r"голы[йм]|голая|раздев|разден|парол|адрес|номер телефон)",
    re.IGNORECASE,
)


class EventChoice(BaseModel):
    event_id: str
    explanation: str = Field(max_length=300)


class TaskText(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    description: str = Field(min_length=3, max_length=300)
    time_limit_sec: int = Field(ge=20, le=300)
    camera: Literal["front", "back"] = "back"
    difficulty: int = Field(ge=1, le=3)


class BribeDecision(BaseModel):
    accepted: bool
    effect: str = ""
    comment: str = Field(default="", max_length=300)


class Nomination(BaseModel):
    title: str = Field(max_length=60)
    player: str
    reason: str = Field(max_length=200)


class Protocol(BaseModel):
    summary: str = Field(max_length=600)
    nominations: list[Nomination] = Field(min_length=1, max_length=6)


@lru_cache(maxsize=1)
def _fallback() -> dict[str, list[str]]:
    with DATA_FILE.open(encoding="utf-8") as file:
        return {key: value for key, value in json.load(file).items() if not key.startswith("_")}


def is_safe(text: str) -> bool:
    return UNSAFE_STEMS.search(text) is None


class AiDealer:
    def __init__(self, client: LLMClient | None, budget_limit: int = 0) -> None:
        self.client = client
        self.budget_limit = budget_limit
        self._budgets: dict[str, TokenBudget] = {}
        self.status: dict[str, Any] = {"enabled": client is not None, "ready": False,
                                       "reason": None if client else "no_key", "message": ""}

    def budget(self, room: Room) -> TokenBudget:
        """Token budget per game (room)."""
        return self._budgets.setdefault(room.room_id, TokenBudget(self.budget_limit))

    async def _json(self, room: Room, user: str, schema: dict, model: type[BaseModel], *, priority: str,
                    max_tokens: int) -> dict:
        if self.client is None:
            raise LLMUnavailable("no_key")
        return await self.client.generate_json(prompts.system_prompt(room), user, schema, priority=priority,
                                               max_tokens=max_tokens, budget=self.budget(room), model=model)

    # ---- comments ------------------------------------------------------------------------

    async def comment(self, room: Room, hand: dict[str, Any]) -> tuple[str, str]:
        """1–2 sentences after a hand (low priority: skipped when the queue is busy). Returns (text, source)."""
        if self.client is not None:
            try:
                text = await self.client.generate_text(prompts.system_prompt(room), prompts.comment_prompt(room, hand),
                                                       priority="low", max_tokens=120, budget=self.budget(room))
                return text[:400], "ai"
            except Exception as error:  # LLMUnavailable or anything unexpected: never break the game
                log.info("AI comment fallback (%s)", error)
        return self.fallback_comment(room, hand), "fallback"

    def fallback_comment(self, room: Room, hand: dict[str, Any]) -> str:
        names = {player.id: player.name for player in room.players}
        if not hand.get("winnings"):
            return random.choice(_fallback()["generic"])
        winner, amount = max(hand["winnings"].items(), key=lambda item: item[1])
        template = random.choice(_fallback()["showdown" if hand.get("showdown") else "no_showdown"])
        return template.format(winner=names.get(winner, winner), amount=amount, hand=hand.get("hand_number", ""))

    # ---- events ----------------------------------------------------------------------------

    async def choose_event(self, room: Room) -> tuple[str, str | None, str]:
        """An event ONLY from the catalog plus the AI's explanation. Returns (event_id, explanation, source)."""
        catalog = event_catalog()
        try:
            choice = await self._json(room, prompts.event_prompt(catalog), prompts.EVENT_SCHEMA, EventChoice,
                                      priority="high", max_tokens=150)
            if choice["event_id"] in catalog:
                return choice["event_id"], choice["explanation"].strip() or None, "ai"
            log.info("AI picked an event outside the catalog, using a random one")
        except Exception as error:
            log.info("AI event fallback (%s)", error)
        return random_event_id(), None, "fallback"

    # ---- tasks ---------------------------------------------------------------------------------

    async def generate_task(self, room: Room, player_id: str, *, kind: str, photo: bool,
                            difficulty: str) -> tuple[dict[str, Any], str]:
        """Penalty / bonus / photo task text. Returns (task fields, source)."""
        player = room.get_player(player_id)
        try:
            task = await self._json(room, prompts.task_prompt(room, player.name, kind, photo, difficulty),
                                    prompts.TASK_SCHEMA, TaskText, priority="high", max_tokens=300)
            if is_safe(task["title"] + " " + task["description"]):
                return {"title": task["title"], "description": task["description"],
                        "time_limit_sec": task["time_limit_sec"], "camera": task["camera"] if photo else None}, "ai"
            log.info("AI task rejected by the safety filter")
        except Exception as error:
            log.info("AI task fallback (%s)", error)
        return pick_task(difficulty, photo=photo), "bank"

    # ---- bribes ----------------------------------------------------------------------------------

    async def bribe(self, room: Room, player_id: str, amount: int, wish: str) -> tuple[dict[str, Any], str]:
        """The AI does not have to accept. Returns ({accepted, effect, comment}, source)."""
        player = room.get_player(player_id)
        big_blind = room.big_blind()
        try:
            decision = await self._json(room, prompts.bribe_prompt(player.name, amount, big_blind, wish),
                                        prompts.BRIBE_SCHEMA, BribeDecision, priority="high", max_tokens=150)
            return {"accepted": decision["accepted"], "effect": wish, "comment": decision["comment"]}, "ai"
        except Exception as error:
            log.info("AI bribe fallback (%s)", error)
        # Fallback: a generous bribe (5+ big blinds) works half of the time.
        accepted = amount >= 5 * big_blind and random.random() < 0.5
        comment = random.choice(_fallback()["bribe_accepted" if accepted else "bribe_rejected"])
        return {"accepted": accepted, "effect": wish, "comment": comment}, "fallback"

    # ---- final protocol ------------------------------------------------------------------------------

    async def protocol(self, room: Room) -> tuple[dict[str, Any], str]:
        names = {player.name for player in room.players}
        try:
            result = await self._json(room, prompts.protocol_prompt(room.stats_lines()), prompts.PROTOCOL_SCHEMA,
                                      Protocol, priority="high", max_tokens=600)
            nominations = [item for item in result["nominations"] if item["player"] in names]
            if nominations:
                return {"summary": result["summary"], "nominations": nominations}, "ai"
        except Exception as error:
            log.info("AI protocol fallback (%s)", error)
        return {"summary": "Вечер окончен! Судья подвёл итоги по статистике.", "nominations": room.fallback_nominations()}, "fallback"
