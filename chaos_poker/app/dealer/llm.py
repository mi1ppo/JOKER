"""LLM interface the rest of the app depends on (section 15.1)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.config import GigaChatConfig


class LLMUnavailable(RuntimeError):
    """No answer right now (no key, busy, budget, network...). Callers must fall back to data/ content."""


class TokenBudget:
    """Token budget for one game (GIGACHAT_TOKEN_BUDGET, 0 = unlimited).

    Over the limit "low" requests (comments) stop; "high" ones continue up to 120 %.
    """

    HIGH_OVERDRAFT = 1.2

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.used = 0

    def allows(self, priority: str) -> bool:
        if self.limit == 0:
            return True
        if priority == "low":
            return self.used < self.limit
        return self.used < self.limit * self.HIGH_OVERDRAFT

    def add(self, tokens: int) -> None:
        self.used += max(0, int(tokens))

    def view(self) -> dict[str, Any]:
        return {"used": self.used, "limit": self.limit}


class LLMClient(Protocol):
    async def generate_json(self, system: str, user: str, schema: dict, *,
                            priority: str = "high", max_tokens: int = 400, **kwargs: Any) -> dict: ...
    async def generate_text(self, system: str, user: str, *,
                            priority: str = "high", max_tokens: int = 200, **kwargs: Any) -> str: ...


def build_llm_client(config: GigaChatConfig | None = None) -> LLMClient | None:
    """GigaChat client when the key is configured, otherwise None (AI functions are off)."""
    from app.config import GigaChatConfig

    config = config or GigaChatConfig.from_env()
    if not config.enabled:
        return None
    from app.dealer.gigachat_client import GigaChatClient

    return GigaChatClient(config)
