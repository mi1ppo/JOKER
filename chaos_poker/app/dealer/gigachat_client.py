"""Thin async GigaChat client implementing LLMClient (section 15.3).

One shared httpx.AsyncClient (keep-alive) with the Russian Ministry CA bundle for GigaChat only;
a single-slot queue (the free tier generates one answer at a time); retries; per-game token budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ValidationError

from app.config import GigaChatConfig
from app.dealer.llm import LLMUnavailable, TokenBudget
from app.dealer.token_manager import TokenManager, TokenUnavailable, describe_error

log = logging.getLogger("chaos.ai")

REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
HIGH_QUEUE_WAIT = 8.0
RETRY_DELAY = 1.0

# User-facing explanations for the host lobby (section 15.4).
STATUS_MESSAGES = {
    "ready": "готов",
    "no_key": "Ключ GigaChat не задан: добавьте GIGACHAT_AUTH_KEY в .env (см. README). Играем на резервном контенте",
    "ssl": "Нет сертификата Минцифры: положите файл в certs/russian_trusted_root_ca.pem (см. README)",
    "auth": "Неверный ключ авторизации или scope",
    "timeout": "Нет соединения с GigaChat, играем на резервном контенте",
    "network": "Нет соединения с GigaChat, играем на резервном контенте",
    "limit": "Лимит запросов, ИИ временно на паузе",
    "error": "GigaChat недоступен, играем на резервном контенте",
}


def status_message(reason: str) -> str:
    return STATUS_MESSAGES.get(reason, STATUS_MESSAGES["error"])


def _retryable(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or status >= 500
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError))


def parse_json_content(content: Any) -> dict:
    """GigaChat returns a JSON string (sometimes wrapped in ``` fences)."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("no content")
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON object expected")
    return data


class GigaChatClient:
    def __init__(self, config: GigaChatConfig, *, http: httpx.AsyncClient | None = None,
                 tokens: TokenManager | None = None, sleep: Callable[[float], Any] = asyncio.sleep,
                 in_use: Callable[[], bool] | None = None) -> None:
        if not config.enabled:
            raise LLMUnavailable("no_key")
        self.config = config
        self.base = config.api_base.rstrip("/")
        self.http = http or httpx.AsyncClient(verify=config.verify, timeout=REQUEST_TIMEOUT)
        self.tokens = tokens or TokenManager(self.http, config.oauth_url, config.auth_key, config.scope, in_use=in_use)
        self._sleep = sleep
        self._queue = asyncio.Semaphore(1)
        self.usage_total = 0
        self.last_error: str | None = None

    def __repr__(self) -> str:
        return f"GigaChatClient(model={self.config.model!r}, tokens={self.tokens!r})"

    async def aclose(self) -> None:
        await self.tokens.stop()
        await self.http.aclose()

    # ---- public API (LLMClient) ------------------------------------------------------------

    async def generate_text(self, system: str, user: str, *, priority: str = "high", max_tokens: int = 200,
                            budget: TokenBudget | None = None, temperature: float = 0.8) -> str:
        data = await self._complete(self._payload(system, user, max_tokens, temperature), priority, budget)
        text = self._content(data)
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        text = str(text or "").strip()
        if not text:
            raise LLMUnavailable("empty")
        return text

    async def generate_json(self, system: str, user: str, schema: dict, *, priority: str = "high",
                            max_tokens: int = 400, budget: TokenBudget | None = None,
                            model: type[BaseModel] | None = None, temperature: float = 0.7) -> dict:
        """JSON answer validated by `model` (pydantic); one retry on an invalid answer, then LLMUnavailable."""
        payload = self._payload(system, user, max_tokens, temperature)
        payload["response_format"] = {"type": "json_schema", "schema": schema}
        for attempt in (1, 2):
            data = await self._complete(payload, priority, budget)
            try:
                parsed = parse_json_content(self._content(data))
                return model.model_validate(parsed).model_dump() if model is not None else parsed
            except (ValueError, ValidationError):
                log.info("GigaChat: невалидный JSON-ответ (попытка %d)", attempt)
        raise LLMUnavailable("invalid")

    async def list_models(self) -> list[str]:
        token = await self.tokens.get_token()
        response = await self.http.get(f"{self.base}/models", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return [item.get("id") for item in response.json().get("data", []) if item.get("id")]

    async def check(self) -> dict[str, Any]:
        """Startup / "check AI" button: token + GET /models + the configured model is available."""
        try:
            await self.tokens.warm_up()
            if not self.tokens.is_ready:
                reason = self.tokens.last_error or "error"
                return {"ready": False, "reason": reason, "message": status_message(reason)}
            models = await self.list_models()
        except Exception as error:
            reason = describe_error(error)
            return {"ready": False, "reason": reason, "message": status_message(reason)}
        if self.config.model not in models:
            return {"ready": False, "reason": "model", "models": models,
                    "message": f"Модель {self.config.model} недоступна, доступны: {', '.join(models) or '—'}"}
        return {"ready": True, "reason": None, "models": models, "message": status_message("ready")}

    # ---- internals ---------------------------------------------------------------------------

    def _payload(self, system: str, user: str, max_tokens: int, temperature: float) -> dict[str, Any]:
        # Exactly one system message first, then the user message (15.3).
        return {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    @staticmethod
    def _content(data: dict) -> Any:
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMUnavailable("invalid") from None

    async def _acquire(self, priority: str) -> None:
        if priority == "low":
            if self._queue.locked():
                raise LLMUnavailable("busy")       # comments are skipped when the queue is busy
            await self._queue.acquire()
            return
        try:
            await asyncio.wait_for(self._queue.acquire(), HIGH_QUEUE_WAIT)
        except asyncio.TimeoutError:
            raise LLMUnavailable("busy") from None

    async def _complete(self, payload: dict, priority: str, budget: TokenBudget | None) -> dict:
        if budget is not None and not budget.allows(priority):
            raise LLMUnavailable("budget")
        await self._acquire(priority)
        try:
            attempts = 2 if priority == "high" else 1
            for attempt in range(1, attempts + 1):
                try:
                    data = await asyncio.wait_for(self._chat_request(payload), REQUEST_TIMEOUT.read or 8.0)
                    break
                except TokenUnavailable as error:
                    self.last_error = str(error)
                    raise LLMUnavailable(self.last_error) from None
                except asyncio.TimeoutError:
                    self.last_error = "timeout"
                    if attempt == attempts:
                        raise LLMUnavailable("timeout") from None
                except Exception as error:
                    self.last_error = describe_error(error)
                    if attempt == attempts or not _retryable(error):
                        raise LLMUnavailable(self.last_error) from None
                await self._sleep(RETRY_DELAY)
        finally:
            self._queue.release()
        self.last_error = None
        tokens = (data.get("usage") or {}).get("total_tokens")
        if isinstance(tokens, (int, float)):
            self.usage_total += int(tokens)
            if budget is not None:
                budget.add(int(tokens))
        return data

    async def _chat_request(self, payload: dict) -> dict:
        for attempt in (1, 2):
            token = await self.tokens.get_token()          # TokenUnavailable -> fallback content
            response = await self.http.post(f"{self.base}/chat/completions", json=payload,
                                            headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401 and attempt == 1:
                await self.tokens.invalidate(token)        # one refresh (single-flight), then retry
                continue
            response.raise_for_status()
            return response.json()
        raise LLMUnavailable("auth")
