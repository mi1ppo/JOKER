"""Per-player statistics for the game results and the AI protocol (sections 8.2 and 13). Mixed into Room."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import Room

STAT_KEYS = ("hands_played", "hands_won", "showdowns_won", "bluffs", "biggest_pot", "chips_won",
             "folds", "all_ins", "raises", "abilities_used", "tasks_done", "side_bets_won", "busted_at_hand")
AGGRESSIVE = {"raise", "all_in"}


class StatsMixin:
    def player_stats(self: Room, player_id: str) -> dict[str, Any]:
        return self.stats.setdefault(player_id, {key: 0 for key in STAT_KEYS} | {"busted_at_hand": None})

    def _record_hand_stats(self: Room, winnings: dict[str, int]) -> None:
        engine = self.engine
        if engine is None:
            return
        actions: dict[str, list[str]] = {}
        for player_id, kind in engine.hand_actions:
            actions.setdefault(player_id, []).append(kind)
        for player in engine.players:
            if player.out:
                continue
            stats = self.player_stats(player.player_id)
            stats["hands_played"] += 1
            kinds = actions.get(player.player_id, [])
            stats["folds"] += kinds.count("fold")
            stats["all_ins"] += kinds.count("all_in")
            stats["raises"] += kinds.count("raise")
            won = winnings.get(player.player_id, 0)
            net = won - player.committed
            if net > 0:
                stats["hands_won"] += 1
                stats["chips_won"] += net
                stats["biggest_pot"] = max(stats["biggest_pot"], won)
                if engine.went_to_showdown:
                    stats["showdowns_won"] += 1
                elif AGGRESSIVE & set(kinds):
                    # Took the pot without a showdown after betting: counts as a bluff.
                    stats["bluffs"] += 1
            if player.stack == 0 and stats["busted_at_hand"] is None:
                stats["busted_at_hand"] = engine.hands_played + 1
        for player_id, amount in (engine.side_bet_results.get("payouts") or {}).items():
            if amount:
                self.player_stats(player_id)["side_bets_won"] += 1

    def results_stats(self: Room) -> list[dict[str, Any]]:
        rows = []
        for player in self.players:
            stats = dict(self.player_stats(player.id))
            stats["abilities_used"] = len(player.used_abilities)
            stats["tasks_done"] = sum(1 for task in self.task_history
                                      if task["player_id"] == player.id and task["status"] == "approved")
            rows.append({"id": player.id, "name": player.name, "stack": player.stack, **stats})
        return rows

    def stats_lines(self: Room) -> list[str]:
        """Compact public statistics for the AI protocol prompt (no cards)."""
        return [
            f"{row['name']}: стек {row['stack']}, выиграно раздач {row['hands_won']} из {row['hands_played']}, "
            f"на вскрытии {row['showdowns_won']}, блефов {row['bluffs']}, крупнейший банк {row['biggest_pot']}, "
            f"фолдов {row['folds']}, олл-инов {row['all_ins']}, способностей {row['abilities_used']}, "
            f"заданий выполнено {row['tasks_done']}"
            + (f", выбывание на раздаче {row['busted_at_hand']}" if row["busted_at_hand"] else "")
            for row in self.results_stats()
        ]

    def fallback_nominations(self: Room) -> list[dict[str, str]]:
        """Nominations computed from the statistics when the AI is unavailable."""
        rows = self.results_stats()
        if not rows:
            return []
        picks = [
            ("Главный блефер", "bluffs", "больше всех банков без вскрытия"),
            ("Самый удачливый", "hands_won", "больше всех побед в раздачах"),
            ("Король банка", "biggest_pot", "самый крупный банк вечера"),
            ("Мастер способностей", "abilities_used", "больше всех применённых способностей"),
            ("Исполнитель вечера", "tasks_done", "больше всех выполненных заданий"),
            ("Рыцарь олл-ина", "all_ins", "больше всех олл-инов"),
        ]
        nominations = []
        for title, key, reason in picks:
            best = max(rows, key=lambda row: row[key])
            if best[key] > 0:
                nominations.append({"title": title, "player": best["name"], "reason": f"{reason} ({best[key]})"})
        busted = [row for row in rows if row["busted_at_hand"]]
        if busted:
            first = min(busted, key=lambda row: row["busted_at_hand"])
            nominations.append({"title": "Главная жертва судьбы", "player": first["name"],
                                "reason": f"первое выбывание вечера (раздача №{first['busted_at_hand']})"})
        return nominations[:5] or [{"title": "Участник вечера", "player": rows[0]["name"], "reason": "был с нами"}]
