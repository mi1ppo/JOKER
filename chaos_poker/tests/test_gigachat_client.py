"""GigaChatClient (section 15.3): queue, retries, token budget, JSON validation, startup check."""
import asyncio
import json
import ssl

import httpx
import pytest

from app.config import GigaChatConfig
from app.dealer import gigachat_client as gc
from app.dealer.ai import EventChoice
from app.dealer.gigachat_client import GigaChatClient, parse_json_content
from app.dealer.llm import LLMUnavailable, TokenBudget
from app.dealer.token_manager import describe_error


class FakeTokens:
    is_ready = True
    last_error = None

    async def get_token(self):
        return "token"

    async def invalidate(self, token):
        pass

    async def warm_up(self):
        pass

    async def stop(self):
        pass


def make_client(handler, model="GigaChat-2"):
    config = GigaChatConfig(auth_key="a2V5", api_base="https://api.test/v1", model=model)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def no_sleep(_):
        return None

    return GigaChatClient(config, http=http, tokens=FakeTokens(), sleep=no_sleep)


def answer(content, tokens=10):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": tokens}})


def test_payload_has_one_system_message_and_json_schema():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return answer('{"event_id": "mirror", "explanation": "ok"}')

    client = make_client(handler)
    result = asyncio.run(client.generate_json("sys", "user", {"type": "object"}, model=EventChoice))
    assert result == {"event_id": "mirror", "explanation": "ok"}
    assert [message["role"] for message in seen["messages"]] == ["system", "user"]
    assert seen["response_format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert seen["model"] == "GigaChat-2"


def test_fenced_json_is_parsed():
    assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_content("[1, 2]")


def test_invalid_json_is_retried_once_then_unavailable():
    calls = []

    def handler(request):
        calls.append(1)
        return answer("not json at all")

    client = make_client(handler)
    with pytest.raises(LLMUnavailable, match="invalid"):
        asyncio.run(client.generate_json("s", "u", {}, model=EventChoice))
    assert len(calls) == 2


def test_high_priority_retries_5xx_and_429_once_low_does_not():
    for status in (500, 503, 429):
        calls = []

        def handler(request, status=status):
            calls.append(1)
            return httpx.Response(status) if len(calls) == 1 else answer("ok")

        assert asyncio.run(make_client(handler).generate_text("s", "u", priority="high")) == "ok"
        assert len(calls) == 2

    calls = []

    def failing(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(LLMUnavailable):
        asyncio.run(make_client(failing).generate_text("s", "u", priority="low"))
    assert len(calls) == 1


def test_400_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(LLMUnavailable):
        asyncio.run(make_client(handler).generate_text("s", "u"))
    assert len(calls) == 1


def test_low_priority_is_skipped_when_queue_is_busy_high_waits():
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return answer("ok")

    async def scenario():
        client = make_client(handler)
        first = asyncio.create_task(client.generate_text("s", "u", priority="high"))
        await asyncio.sleep(0.01)
        with pytest.raises(LLMUnavailable, match="busy"):
            await client.generate_text("s", "u", priority="low")
        second = asyncio.create_task(client.generate_text("s", "u", priority="high"))
        await asyncio.sleep(0.01)
        release.set()
        return await asyncio.gather(first, second)

    assert asyncio.run(scenario()) == ["ok", "ok"]


def test_high_priority_gives_up_after_waiting_for_the_queue(monkeypatch):
    monkeypatch.setattr(gc, "HIGH_QUEUE_WAIT", 0.05)
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return answer("ok")

    async def scenario():
        client = make_client(handler)
        first = asyncio.create_task(client.generate_text("s", "u"))
        await asyncio.sleep(0.01)
        with pytest.raises(LLMUnavailable, match="busy"):
            await client.generate_text("s", "u")
        release.set()
        await first

    asyncio.run(scenario())


def test_token_budget_stops_low_first_and_high_at_120_percent():
    budget = TokenBudget(100)
    client = make_client(lambda request: answer("ok", tokens=60))
    asyncio.run(client.generate_text("s", "u", priority="low", budget=budget))
    asyncio.run(client.generate_text("s", "u", priority="low", budget=budget))
    assert budget.used == 120
    with pytest.raises(LLMUnavailable, match="budget"):
        asyncio.run(client.generate_text("s", "u", priority="low", budget=budget))
    with pytest.raises(LLMUnavailable, match="budget"):
        asyncio.run(client.generate_text("s", "u", priority="high", budget=budget))   # 120 >= 120 %
    budget = TokenBudget(100)
    budget.used = 110
    assert asyncio.run(client.generate_text("s", "u", priority="high", budget=budget)) == "ok"
    assert TokenBudget(0).allows("low")  # 0 = unlimited


def test_check_reports_missing_model():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "GigaChat"}, {"id": "GigaChat-Pro"}]})

    result = asyncio.run(make_client(handler, model="GigaChat-2").check())
    assert result["ready"] is False
    assert "GigaChat-2 недоступна" in result["message"] and "GigaChat-Pro" in result["message"]
    ok = asyncio.run(make_client(handler, model="GigaChat").check())
    assert ok["ready"] is True


def test_describe_error_labels():
    request = httpx.Request("POST", "https://x")
    assert describe_error(httpx.HTTPStatusError("x", request=request, response=httpx.Response(401, request=request))) == "auth"
    assert describe_error(httpx.HTTPStatusError("x", request=request, response=httpx.Response(429, request=request))) == "limit"
    assert describe_error(httpx.ConnectTimeout("x")) == "timeout"
    assert describe_error(httpx.ConnectError("x")) == "network"
    try:
        try:
            raise ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
        except ssl.SSLError as inner:
            raise httpx.ConnectError("boom") from inner
    except httpx.ConnectError as error:
        assert describe_error(error) == "ssl"
    assert "Минцифры" in gc.status_message("ssl")
