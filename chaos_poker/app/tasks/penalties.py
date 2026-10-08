"""Penalty and bonus tasks, their confirmation and chip fines (sections 8.1, 8.3, 9.1).

Mixed into Room: uses its players, settings, engine and feed.

Task lifecycle:
    pending --player "done"--> pending_review (human dealer) | voting (no dealer / AI dealer)
    pending_review | voting --accepted--> approved
                           --rejected--> pending (one retry) --> rejected (final)
    pending --player refuses--> refused
"""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from app.game.activities import SKIP_FEE_BB
from app.game.tasks import pick_task

if TYPE_CHECKING:
    from app.models import Room

TASK_TYPES = {"text", "photo", "physical", "common"}
TASK_KINDS = {"penalty", "bonus"}
TASK_SOURCES = {"custom", "bank", "ai", "auto"}
OPEN_STATUSES = {"pending", "pending_review", "voting"}

MAX_FINE_BB = 2          # a dealer fine is at most 2 BB at a time
TASK_FINE_BB = 2         # refusing a penalty / failing it after the retry
BONUS_REWARD_BB = 2      # reward for an accepted bonus task
MAX_ATTEMPTS = 2         # first try + one retry after a rejection


class TaskMixin:
    # ---- chips -----------------------------------------------------------------

    def big_blind(self: Room) -> int:
        if self.engine is not None:
            return self.engine.big_blind
        from app.game.engine import HoldemEngine

        return HoldemEngine.BLIND_LEVELS[0][1]

    def _charge(self: Room, player_id: str, amount: int) -> int:
        """Take chips from a player into the next hand's pot."""
        if self.engine is None:
            player = self.get_player(player_id)
            paid = min(amount, player.stack)
            player.stack -= paid
            return paid
        paid = self.engine.charge(player_id, amount)
        self._sync_engine_state()
        return paid

    def _reward(self: Room, player_id: str, amount: int) -> None:
        # TODO: уточнить — награда за бонус сейчас создаёт новые фишки, а не берётся из банка.
        if self.engine is None:
            self.get_player(player_id).stack += amount
            return
        self.engine.player(player_id).stack += amount
        self._sync_engine_state()

    def fine_player(self: Room, target_id: str, amount: int, *, reason: str | None = None) -> int:
        """Dealer fine (up to 2 BB); the chips go to the next hand's main pot."""
        player = self.get_player(target_id)
        limit = MAX_FINE_BB * self.big_blind()
        if not 1 <= amount <= limit:
            raise ValueError(f"Fine must be between 1 and {limit}")
        paid = self._charge(target_id, amount)
        self.announce(f"Штраф {player.name}: {paid}" + (f" — {reason}" if reason else ""))
        self.notify("fine", {"player_id": target_id, "amount": paid})
        return paid

    # ---- issuing ---------------------------------------------------------------

    def get_task(self: Room, task_id: str) -> dict[str, Any]:
        task = next((item for item in self.task_history if item["id"] == task_id), None)
        if task is None:
            raise ValueError("Unknown task")
        return task

    def open_tasks(self: Room, player_id: str | None = None) -> list[dict[str, Any]]:
        return [
            task for task in self.task_history
            if task["status"] in OPEN_STATUSES and (player_id is None or task["player_id"] == player_id)
        ]

    def issue_task(
        self: Room,
        player_id: str,
        *,
        task_type: str = "text",
        kind: str = "penalty",
        difficulty: str = "medium",
        camera: str | None = None,
        title: str | None = None,
        description: str | None = None,
        time_limit_sec: int | None = None,
        task_id: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        if player_id == "random":
            candidates = [player.id for player in self.players if player.stack > 0] or [player.id for player in self.players]
            if not candidates:
                raise ValueError("No players")
            player_id = random.choice(candidates)
        if player_id != "all":
            self.get_player(player_id)
        if task_type not in TASK_TYPES:
            raise ValueError(f"Unknown task type: {task_type}")
        if kind not in TASK_KINDS:
            raise ValueError(f"Unknown task kind: {kind}")
        if task_type == "photo" and self.settings.dealer_mode != "human":
            # Photo tasks need a human dealer to check them (sections 8.2 and 8.3).
            raise ValueError("Photo tasks are available only with a human dealer")
        source = source or ("custom" if title else "bank")
        if source not in TASK_SOURCES:
            raise ValueError(f"Unknown task source: {source}")
        if source == "custom" and not (title or "").strip():
            raise ValueError("Custom task needs a text")
        if title is None:
            # TODO: уточнить — source == "ai" будет генерироваться GigaChat (этап 9); пока берём из банка.
            bank = pick_task(difficulty, photo=task_type == "photo")
            title = bank["title"]
            description = description or bank["description"]
            time_limit_sec = time_limit_sec or bank["time_limit_sec"]
            camera = camera or bank["camera"]
        task = {
            "id": task_id or f"task_{len(self.task_history) + 1}",
            "player_id": player_id,
            "title": title.strip(),
            "description": (description or title).strip(),
            "time_limit_sec": time_limit_sec or 60,
            "task_type": task_type,
            "kind": kind,
            "difficulty": difficulty,
            "camera": camera,
            "source": source,
            "status": "pending",
            "attempts": 0,
            "reward": BONUS_REWARD_BB * self.big_blind() if kind == "bonus" else 0,
            # A physical activity can be skipped for a small fee (section 9.3).
            "refuse_cost": (SKIP_FEE_BB if task_type == "physical" else TASK_FINE_BB) * self.big_blind()
            if kind == "penalty" else 0,
            "votes": {},
            "issued_at": time.time(),
        }
        self.active_task = task
        self.task_history.append(task)
        target = "всем" if player_id == "all" else self.get_player(player_id).name
        label = "Бонус" if kind == "bonus" else "Штраф"
        self.announce(f"{label} → {target}: {task['title']}")
        self.notify("task_assigned", {
            "task_id": task["id"],
            "target_id": player_id,
            "title": task["title"],
            "description": task["description"],
            "time_limit_sec": task["time_limit_sec"],
            "photo": task_type == "photo",
        })
        return task

    # ---- player side -------------------------------------------------------------

    def _owned_open_task(self: Room, player_id: str, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["player_id"] not in {player_id, "all"}:
            raise ValueError("This task belongs to another player")
        if task["status"] not in OPEN_STATUSES:
            raise ValueError("Task is already finished")
        return task

    def _confirmation_mode(self: Room) -> str:
        """Who confirms a finished task: "dealer", "self" (AI dealer) or "vote" (no dealer, section 8.3)."""
        if self.settings.dealer_mode == "ai":
            return "self"
        if self.settings.dealer_mode == "human" and self.dealer_connected:
            return "dealer"
        return "vote"

    def complete_task(self: Room, player_id: str, task_id: str) -> dict[str, Any]:
        """The player says the task is done; now the dealer or the table confirms it."""
        task = self._owned_open_task(player_id, task_id)
        if task["status"] != "pending":
            raise ValueError("Task is already waiting for confirmation")
        if task["task_type"] == "photo":
            raise ValueError("Photo tasks are completed by uploading a photo")
        self._send_for_confirmation(task)
        return task

    def _send_for_confirmation(self: Room, task: dict[str, Any]) -> None:
        name = self._task_owner_name(task)
        mode = self._confirmation_mode()
        if mode == "self":
            # With the AI dealer the player's own word is enough.
            task["status"] = "pending_review"
            self.announce(f"{name}: задание «{task['title']}» выполнено (подтверждено игроком)")
            self._decide(task, True)
        elif mode == "vote":
            task["status"] = "voting"
            task["votes"] = {}
            self.announce(f"{name}: задание «{task['title']}» выполнено — голосуем!")
        else:
            task["status"] = "pending_review"
            self.announce(f"{name}: задание «{task['title']}» выполнено — решает диллер")

    def refuse_task(self: Room, player_id: str, task_id: str) -> dict[str, Any]:
        """Refusing a penalty costs 2 BB; a bonus task can be declined for free."""
        task = self._owned_open_task(player_id, task_id)
        name = self._task_owner_name(task)
        task["status"] = "refused"
        if task["kind"] == "penalty" and player_id != "all":
            paid = self._charge(player_id, task["refuse_cost"])
            self.announce(f"{name} отказывается от штрафа и платит {paid}")
        else:
            self.announce(f"{name} отказывается от бонусного задания")
        return task

    def submit_photo_task(self: Room, player_id: str, submission_url: str, task_id: str | None = None) -> dict[str, Any]:
        task = self.get_task(task_id) if task_id else self.active_task
        if task is None:
            raise ValueError("No active task")
        if task["player_id"] != player_id:
            raise ValueError("The uploaded photo does not match the active task owner")
        if task["task_type"] != "photo":
            raise ValueError("Only photo tasks can be submitted")
        if task["status"] != "pending":
            raise ValueError("Task is not waiting for a photo")
        if not submission_url:
            raise ValueError("Photo URL is required")
        task["status"] = "pending_review"
        task["submission"] = {"player_id": player_id, "url": submission_url, "submitted_at": time.time()}
        return task

    # ---- confirmation ----------------------------------------------------------

    def review_task(self: Room, task_id: str, *, approved: bool = True, comment: str | None = None) -> dict[str, Any]:
        """Dealer (or host) accepts or rejects a task, optionally with a comment for the album."""
        task = self.get_task(task_id)
        if task["status"] not in OPEN_STATUSES:
            raise ValueError("Task is already finished")
        if comment and comment.strip():
            task["dealer_comment"] = comment.strip()[:200]
        self._decide(task, approved)
        return task

    def vote_task(self: Room, voter_id: str, task_id: str, value: bool) -> dict[str, Any]:
        """Majority of the other players confirms a task (mode without a dealer)."""
        task = self.get_task(task_id)
        if task["status"] != "voting":
            raise ValueError("This task is not being voted on")
        if voter_id == task["player_id"]:
            raise ValueError("You cannot vote on your own task")
        self.get_player(voter_id)
        task["votes"][voter_id] = bool(value)
        voters = self.task_voters(task)
        needed = len(voters) // 2 + 1
        yes = sum(1 for vote in task["votes"].values() if vote)
        no = len(task["votes"]) - yes
        if yes >= needed:
            self._decide(task, True)
        elif no > len(voters) - needed:
            self._decide(task, False)
        return task

    def task_voters(self: Room, task: dict[str, Any]) -> list[str]:
        return [player.id for player in self.players if player.id != task["player_id"]]

    def _task_owner_name(self: Room, task: dict[str, Any]) -> str:
        return "Все" if task["player_id"] == "all" else self.get_player(task["player_id"]).name

    def _decide(self: Room, task: dict[str, Any], approved: bool) -> None:
        name = self._task_owner_name(task)
        task["attempts"] += 1
        self._on_photo_decided(task, approved)
        if approved:
            task["status"] = "approved"
            if task["kind"] == "bonus" and task["player_id"] != "all":
                self._reward(task["player_id"], task["reward"])
                self.announce(f"{name}: бонус принят, +{task['reward']}")
            else:
                self.announce(f"{name}: задание «{task['title']}» засчитано")
        elif task["attempts"] < MAX_ATTEMPTS:
            task["status"] = "pending"
            task.pop("submission", None)
            task["votes"] = {}
            self.announce(f"{name}: задание не засчитано, есть ещё одна попытка")
        else:
            task["status"] = "rejected"
            if task["kind"] == "penalty" and task["player_id"] != "all":
                paid = self._charge(task["player_id"], task["refuse_cost"])
                self.announce(f"{name}: штраф не выполнен, списано {paid}")
            else:
                self.announce(f"{name}: бонусное задание не засчитано")
        self.notify("task_result", {
            "task_id": task["id"],
            "accepted": approved,
            "status": task["status"],
            **({"photo_url": task["submission"]["url"]} if approved and task.get("submission") else {}),
        })

    # ---- automatic penalty after a big pot --------------------------------------------

    def _maybe_auto_penalty(self: Room, contributions: dict[str, int], winnings: dict[str, int], big_blind: int) -> None:
        """The biggest loser of a pot of at least N big blinds gets a penalty task (section 9.1)."""
        if not self.settings.penalty_after_big_pot:
            return
        pot = sum(contributions.values())
        if pot < self.settings.penalty_threshold_bb * big_blind:
            return
        losses = {player_id: amount - winnings.get(player_id, 0) for player_id, amount in contributions.items()}
        loser, loss = max(losses.items(), key=lambda item: item[1], default=(None, 0))
        if loser is None or loss <= 0 or self.open_tasks(loser):
            return
        if self.settings.dealer_mode == "ai":
            # The AI writes the penalty; the bank is the fallback (handled by the server).
            self.request_ai("task", player_id=loser, task_kind="penalty", photo=False,
                            difficulty=self.settings.task_spiciness, source="auto")
            return
        self.issue_task(loser, kind="penalty", difficulty=self.settings.task_spiciness, source="auto")
