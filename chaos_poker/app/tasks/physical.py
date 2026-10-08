"""Physical activities (section 9.3) without phone sensors. Mixed into Room.

- "task" activities (squats, standing table) are ordinary tasks: the player says "done" and the
  human dealer confirms; with the AI dealer the player confirms it himself; without a dealer — a vote.
- Contests: shake battle and freeze are judged by the human dealer; otherwise the participants
  report the result themselves. The duel (rock-paper-scissors) is resolved by the server from the
  players' choices.
- Any activity can be skipped for a small fee.
"""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from app.game.activities import (
    DUEL_CHOICES, DUEL_MAX_ROUNDS, DUEL_STAKE_BB, FREEZE_FINE_BB, SHAKE_PRIZE_BB, SKIP_FEE_BB,
    activity_info, duel_winner, task_text,
)

if TYPE_CHECKING:
    from app.models import Room

# Participants have this long after the activity itself to report the result.
REPORT_GRACE_SEC = 30
DUEL_NAMES = {"rock": "камень", "paper": "бумага", "scissors": "ножницы"}


class ActivityMixin:
    # ---- starting ----------------------------------------------------------------

    def start_activity(self: Room, activity_type: str, *, players: list[str] | None = None) -> dict[str, Any]:
        info = activity_info(activity_type)
        if self.settings.physical_activity == "off":
            raise ValueError("Physical activities are off in the lobby settings")
        if info["kind"] == "task":
            target = players[0] if players else "random"
            task = self.issue_task(
                target, task_type="physical", kind="penalty", source="custom",
                title=f"{info['icon']} {info['title']}", description=task_text(activity_type, self.settings.physical_activity),
            )
            task["activity"] = activity_type
            return task
        if self.active_activity is not None and self.active_activity["status"] == "active":
            raise ValueError("Another activity is in progress")
        participants = self._activity_players(info, players)
        intensity = self.settings.physical_activity
        bb = self.big_blind()
        activity = {
            "id": f"activity_{len(self.activity_history) + 1}",
            "type": activity_type,
            "title": info["title"],
            "icon": info["icon"],
            "description": info["description"],
            "players": participants,
            "status": "active",
            "duration_sec": info["duration_sec"],
            "judge": "dealer" if self._dealer_judges() else "players",
            "prize": SHAKE_PRIZE_BB * bb if activity_type == "shake_battle" else 0,
            "fine": FREEZE_FINE_BB * bb if activity_type == "freeze" else 0,
            "stake": DUEL_STAKE_BB.get(intensity, 1) * bb if activity_type == "duel" else 0,
            "skip_fee": SKIP_FEE_BB * bb,
            "reports": {},
            "picks": {},
            "round": 1,
            "result": None,
        }
        self.active_activity = activity
        self.activity_history.append(activity)
        self.activity_deadline_at = time.monotonic() + info["duration_sec"] + REPORT_GRACE_SEC
        self.activity_mode = intensity
        names = ", ".join(self.get_player(player_id).name for player_id in participants)
        self.announce(f"{info['icon']} {info['title']}: {names}")
        self.notify("activity_started", {"id": activity["id"], "type": activity_type, "title": info["title"],
                                         "players": participants, "duration_sec": info["duration_sec"]})
        return activity

    def _activity_players(self: Room, info: dict[str, Any], players: list[str] | None) -> list[str]:
        in_game = [player.id for player in self.players if player.stack > 0] or [player.id for player in self.players]
        if info["players"] == "all":
            chosen = players or in_game
        else:
            chosen = players or random.sample(in_game, min(info["players"], len(in_game)))
            if len(chosen) != info["players"]:
                raise ValueError(f"{info['title']} needs {info['players']} players")
        if len(set(chosen)) != len(chosen) or len(chosen) < 2:
            raise ValueError("An activity needs at least two different players")
        for player_id in chosen:
            self.get_player(player_id)
        return list(chosen)

    def _dealer_judges(self: Room) -> bool:
        return self.settings.dealer_mode == "human" and self.dealer_connected

    def _open_activity(self: Room) -> dict[str, Any]:
        activity = self.active_activity
        if activity is None or activity["status"] != "active":
            raise ValueError("No activity in progress")
        return activity

    # ---- results -------------------------------------------------------------------

    def resolve_activity(self: Room, *, winner_id: str | None = None, movers: list[str] | None = None) -> dict[str, Any]:
        """The human dealer decides: the shake battle winner or who moved in "freeze"."""
        activity = self._open_activity()
        if activity["type"] == "shake_battle":
            if winner_id not in activity["players"]:
                raise ValueError("The winner must be one of the participants")
            self._finish_shake(activity, winner_id)
        elif activity["type"] == "freeze":
            movers = list(movers or [])
            if any(player_id not in activity["players"] for player_id in movers):
                raise ValueError("Only participants can be marked")
            self._finish_freeze(activity, movers)
        else:
            raise ValueError("The duel is decided by the players' choices")
        return activity

    def report_activity(self: Room, player_id: str, *, winner_id: str | None = None,
                        moved: bool | None = None) -> dict[str, Any]:
        """Without a human dealer the participants report the result themselves."""
        activity = self._open_activity()
        if player_id not in activity["players"]:
            raise ValueError("Only participants can report")
        if activity["judge"] == "dealer":
            raise ValueError("The dealer decides this activity")
        if activity["type"] == "shake_battle":
            if winner_id not in activity["players"]:
                raise ValueError("The winner must be one of the participants")
            activity["reports"][player_id] = winner_id
            if len(activity["reports"]) == len(activity["players"]):
                verdicts = set(activity["reports"].values())
                if len(verdicts) == 1:
                    self._finish_shake(activity, verdicts.pop())
                else:
                    self._finish_activity(activity, {"draw": True}, "Шейк-баттл: участники не договорились — ничья")
        elif activity["type"] == "freeze":
            if moved is None:
                raise ValueError("Report whether you moved")
            activity["reports"][player_id] = bool(moved)
            if len(activity["reports"]) == len(activity["players"]):
                self._finish_freeze(activity, [pid for pid, value in activity["reports"].items() if value])
        else:
            raise ValueError("The duel is decided by the players' choices")
        return activity

    def duel_pick(self: Room, player_id: str, choice: str) -> dict[str, Any]:
        activity = self._open_activity()
        if activity["type"] != "duel":
            raise ValueError("No duel in progress")
        if player_id not in activity["players"]:
            raise ValueError("Only duelists can choose")
        if choice not in DUEL_CHOICES:
            raise ValueError("Unknown duel choice")
        activity["picks"][player_id] = choice
        if len(activity["picks"]) < 2:
            return activity
        first, second = activity["players"]
        outcome = duel_winner(activity["picks"][first], activity["picks"][second])
        shown = ", ".join(f"{self.get_player(pid).name} — {DUEL_NAMES[activity['picks'][pid]]}" for pid in (first, second))
        if outcome is None:
            if activity["round"] >= DUEL_MAX_ROUNDS:
                self._finish_activity(activity, {"draw": True, "last_picks": dict(activity["picks"])},
                                      f"Дуэль: {shown} — ничья после {DUEL_MAX_ROUNDS} раундов")
                return activity
            activity["last_picks"] = dict(activity["picks"])
            activity["picks"] = {}
            activity["round"] += 1
            self.announce(f"Дуэль: {shown} — ничья, раунд {activity['round']}")
            self.notify("duel_round", {"round": activity["round"], "last_picks": activity["last_picks"]})
            return activity
        winner, loser = (first, second) if outcome == 0 else (second, first)
        paid = self._transfer(loser, winner, activity["stake"])
        self._finish_activity(
            activity,
            {"winner_id": winner, "amount": paid, "last_picks": dict(activity["picks"])},
            f"Дуэль: {shown} — {self.get_player(winner).name} забирает {paid}",
        )
        return activity

    def skip_activity(self: Room, player_id: str) -> dict[str, Any]:
        """A participant leaves the activity for a small fee."""
        activity = self._open_activity()
        if player_id not in activity["players"]:
            raise ValueError("Only participants can skip")
        paid = self._charge(player_id, activity["skip_fee"])
        activity["players"].remove(player_id)
        activity["reports"].pop(player_id, None)
        activity["picks"].pop(player_id, None)
        self.announce(f"{self.get_player(player_id).name} пропускает «{activity['title']}» за {paid}")
        if len(activity["players"]) < 2:
            self._finish_activity(activity, {"cancelled": True}, f"«{activity['title']}» отменено: мало участников")
        elif activity["type"] == "freeze" and activity["judge"] == "players" \
                and len(activity["reports"]) == len(activity["players"]):
            self._finish_freeze(activity, [pid for pid, value in activity["reports"].items() if value])
        return activity

    def cancel_activity(self: Room) -> None:
        activity = self._open_activity()
        self._finish_activity(activity, {"cancelled": True}, f"«{activity['title']}» отменено")

    def _finish_shake(self: Room, activity: dict[str, Any], winner_id: str) -> None:
        # TODO: уточнить — приз создаёт новые фишки, как и награда за бонусное задание.
        self._reward(winner_id, activity["prize"])
        self._finish_activity(activity, {"winner_id": winner_id, "amount": activity["prize"]},
                              f"Шейк-баттл: побеждает {self.get_player(winner_id).name}, +{activity['prize']}")

    def _finish_freeze(self: Room, activity: dict[str, Any], movers: list[str]) -> None:
        paid = {player_id: self._charge(player_id, activity["fine"]) for player_id in movers}
        text = ("Замри: дёрнулись: " + ", ".join(f"{self.get_player(pid).name} (−{amount})" for pid, amount in paid.items())
                if paid else "Замри: все выдержали!")
        self._finish_activity(activity, {"movers": movers, "fines": paid}, text)

    def _finish_activity(self: Room, activity: dict[str, Any], result: dict[str, Any], text: str) -> None:
        activity["status"] = "cancelled" if result.get("cancelled") else "done"
        activity["result"] = result
        self.activity_deadline_at = None
        self.announce(text)
        self.notify("activity_result", {"id": activity["id"], "type": activity["type"], "text": text, **result})

    def _transfer(self: Room, from_id: str, to_id: str, amount: int) -> int:
        """Move chips between players (a duel stake); the payer keeps a chip if he is still betting."""
        paid = self._charge(from_id, amount)
        if self.engine is not None:
            self.engine.carryover -= paid
        self._reward(to_id, paid)
        return paid

    def process_activity_timer(self: Room, now: float) -> bool:
        """Close a participant-judged activity nobody finished reporting (silence = "did not move")."""
        activity = self.active_activity
        if activity is None or activity["status"] != "active" or self.activity_deadline_at is None:
            return False
        if now < self.activity_deadline_at:
            return False
        if activity["type"] == "freeze" and activity["judge"] == "players":
            self._finish_freeze(activity, [pid for pid, value in activity["reports"].items() if value])
        elif activity["judge"] == "players" or activity["type"] == "duel":
            self._finish_activity(activity, {"cancelled": True}, f"«{activity['title']}»: время вышло")
        else:
            return False  # the human dealer decides whenever he is ready
        return True

    def activity_view(self: Room, role: str, player_id: str | None) -> dict[str, Any]:
        activity = self.active_activity
        if activity is None:
            return {"type": self.activity_mode, "status": "off"}
        view = {key: value for key, value in activity.items() if key not in {"picks", "reports"}}
        # Duel choices stay secret until both players have chosen.
        view["picked"] = sorted(activity["picks"])
        view["reported"] = sorted(activity["reports"])
        if role == "player" and player_id in activity["players"]:
            view["your_pick"] = activity["picks"].get(player_id)
            view["your_report"] = activity["reports"].get(player_id)
        if activity["status"] == "active" and self.activity_deadline_at is not None:
            view["seconds_left"] = max(0, round(self.activity_deadline_at - time.monotonic() - REPORT_GRACE_SEC))
        return view
