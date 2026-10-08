"""Section 15.5: TokenManager behaviour without the real network (httpx.MockTransport + a controlled clock)."""
import asyncio
import json
import logging

import httpx
import pytest

from app.config import GigaChatConfig
from app.dealer.ai import AiDealer
from app.dealer.gigachat_client import GigaChatClient
from app.dealer.llm import LLMUnavailable
from app.dealer.token_manager import TokenManager, TokenUnavailable
from app.models import LobbySettings, Room

AUTH_KEY = "c2VjcmV0LWNsaWVudDpzZWNyZXQ="
OAUTH = "https://oauth.test/api/v2/oauth"
API = "https://api.test/v1"


class Clock:
    # A realistic epoch: GigaChat returns expires_at in milliseconds (~1.7e12).
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


class Server:
    """Fake OAuth + chat API. Counts requests; tokens are numbered."""

    def __init__(self, clock, *, ttl=1800, oauth_delay=0.0, fail_oauth=False, expires_ms=False,
                 reject_tokens=()):
        self.clock, self.ttl, self.oauth_delay = clock, ttl, oauth_delay
        self.fail_oauth, self.expires_ms = fail_oauth, expires_ms
        self.reject_tokens = set(reject_tokens)
        self.oauth_calls = 0
        self.oauth_times = []
        self.chat_calls = 0
        self.rq_uids = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth"):
            self.oauth_calls += 1
            self.oauth_times.append(self.clock())
            self.rq_uids.append(request.headers["RqUID"])
            assert request.headers["Authorization"] == f"Basic {AUTH_KEY}"
            assert request.content == b"scope=GIGACHAT_API_PERS"
            if self.oauth_delay:
                await asyncio.sleep(self.oauth_delay)
            if self.fail_oauth:
                return httpx.Response(401, json={"message": "bad key"})
            expires = self.clock() + self.ttl
            return httpx.Response(200, json={"access_token": f"token-{self.oauth_calls}",
                                             "expires_at": int(expires * 1000) if self.expires_ms else int(expires)})
        if request.url.path.endswith("/chat/completions"):
            self.chat_calls += 1
            token = request.headers["Authorization"].removeprefix("Bearer ")
            if token in self.reject_tokens:
                return httpx.Response(401, json={"message": "token expired"})
            body = json.loads(request.content)
            assert [message["role"] for message in body["messages"]] == ["system", "user"]
            return httpx.Response(200, json={"choices": [{"message": {"content": "Комментарий судьи"}}],
                                             "usage": {"total_tokens": 42}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GigaChat-2"}]})
        return httpx.Response(404)


def make_manager(server, clock, **kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return TokenManager(http, OAUTH, AUTH_KEY, "GIGACHAT_API_PERS", now=clock, **kwargs)


def make_client(server, clock):
    config = GigaChatConfig(auth_key=AUTH_KEY, oauth_url=OAUTH, api_base=API)
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    tokens = TokenManager(http, OAUTH, AUTH_KEY, "GIGACHAT_API_PERS", now=clock)

    async def no_sleep(_):
        return None

    return GigaChatClient(config, http=http, tokens=tokens, sleep=no_sleep)


# 1 ---------------------------------------------------------------------------------------------
def test_first_call_fetches_token_second_call_uses_cache():
    clock = Clock()
    server = Server(clock)
    manager = make_manager(server, clock)

    async def scenario():
        first = await manager.get_token()
        second = await manager.get_token()
        return first, second

    assert asyncio.run(scenario()) == ("token-1", "token-1")
    assert server.oauth_calls == 1
    assert manager.status == "ready"


# 2 ---------------------------------------------------------------------------------------------
def test_twenty_concurrent_calls_make_exactly_one_oauth_request():
    clock = Clock()
    server = Server(clock, oauth_delay=0.05)
    manager = make_manager(server, clock)

    async def scenario():
        return await asyncio.gather(*(manager.get_token() for _ in range(20)))

    tokens = asyncio.run(scenario())
    assert set(tokens) == {"token-1"}
    assert server.oauth_calls == 1


# 3 ---------------------------------------------------------------------------------------------
def test_background_task_refreshes_before_expiry_on_fast_clock():
    clock = Clock()
    server = Server(clock, ttl=1800)

    async def fast_sleep(seconds):
        clock.now += seconds          # accelerated clock: sleeping only moves time forward
        await asyncio.sleep(0)

    manager = make_manager(server, clock, sleep=fast_sleep)

    async def scenario():
        await manager.start()
        expiries = [manager.expires_at]
        while server.oauth_calls < 4:
            await asyncio.sleep(0)
            if manager.expires_at != expiries[-1]:
                expiries.append(manager.expires_at)
        await manager.stop()
        return expiries

    expiries = asyncio.run(scenario())
    # Every next token was fetched REFRESH_MARGIN (+ up to JITTER) seconds before the previous one expired.
    for previous_expiry, fetched_at in zip(expiries, server.oauth_times[1:]):
        margin = previous_expiry - fetched_at
        assert TokenManager.REFRESH_MARGIN <= margin <= TokenManager.REFRESH_MARGIN + TokenManager.JITTER
    assert manager.is_ready


# 4 ---------------------------------------------------------------------------------------------
def test_401_from_api_refreshes_once_and_retries():
    clock = Clock()
    server = Server(clock, reject_tokens={"token-1"})
    client = make_client(server, clock)

    text = asyncio.run(client.generate_text("system", "user"))
    assert text == "Комментарий судьи"
    assert server.oauth_calls == 2      # initial token + one refresh after 401
    assert server.chat_calls == 2       # rejected request + the retry


# 5 ---------------------------------------------------------------------------------------------
def test_ten_concurrent_401_refresh_the_token_exactly_once():
    clock = Clock()
    server = Server(clock, oauth_delay=0.02)
    manager = make_manager(server, clock)

    async def scenario():
        bad = await manager.get_token()
        await asyncio.gather(*(manager.invalidate(bad) for _ in range(10)))
        return await manager.get_token()

    assert asyncio.run(scenario()) == "token-2"
    assert server.oauth_calls == 2


def test_concurrent_requests_through_the_client_after_401_refresh_once():
    clock = Clock()
    server = Server(clock, reject_tokens={"token-1"})
    client = make_client(server, clock)

    async def scenario():
        return await asyncio.gather(*(client.generate_text("s", "u") for _ in range(10)))

    assert asyncio.run(scenario()) == ["Комментарий судьи"] * 10
    assert server.oauth_calls == 2


# 6 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw_offset, expected_offset", [
    (1500, 1500),            # seconds
    (1500 * 1000, 1500),     # milliseconds -> seconds
    (-10, 25 * 60),          # in the past -> 25 minutes
    (7200, 25 * 60),         # more than an hour ahead -> 25 minutes
])
def test_expires_at_is_normalized(raw_offset, expected_offset):
    clock = Clock()
    manager = make_manager(Server(clock), clock)
    if raw_offset > 1e5:
        raw = clock.now * 1000 + raw_offset
    else:
        raw = clock.now + raw_offset
    assert manager._normalize_expiry(raw) == pytest.approx(clock.now + expected_offset)


def test_seconds_and_milliseconds_give_the_same_token_lifetime():
    results = []
    for expires_ms in (False, True):
        clock = Clock()
        manager = make_manager(Server(clock, expires_ms=expires_ms), clock)
        asyncio.run(manager.get_token())
        results.append(manager.expires_at)
    assert results[0] == pytest.approx(results[1], abs=1)


# 7 ---------------------------------------------------------------------------------------------
def test_oauth_error_fails_fast_during_cooldown_then_retries():
    clock = Clock()
    server = Server(clock, fail_oauth=True)
    manager = make_manager(server, clock)

    async def attempt():
        try:
            await manager.get_token()
        except TokenUnavailable as error:
            return str(error)
        return None

    assert asyncio.run(attempt()) == "auth"
    assert server.oauth_calls == 1
    for _ in range(5):
        clock.now += 0.5
        assert asyncio.run(attempt()) == "auth"
    assert server.oauth_calls == 1          # no network during COOLDOWN
    clock.now += TokenManager.COOLDOWN
    server.fail_oauth = False
    assert asyncio.run(attempt()) is None
    assert server.oauth_calls == 2
    assert len(set(server.rq_uids)) == 2    # a fresh RqUID for every token request


# 8 ---------------------------------------------------------------------------------------------
def test_game_goes_on_with_fallback_content_when_token_is_unavailable():
    clock = Clock()
    server = Server(clock, fail_oauth=True)
    dealer = AiDealer(make_client(server, clock))
    room = Room(room_id="ai", host_name="H", host_token="h", dealer_token="d", player_token="p",
                settings=LobbySettings(dealer_mode="ai"))
    room.add_player(name="Аня", agreed_to_terms=True)
    room.add_player(name="Боря", agreed_to_terms=True)
    hand = {"hand_number": 1, "winnings": {"player_1": 30}, "showdown": False}

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        comment = await dealer.comment(room, hand)
        event = await dealer.choose_event(room)
        task = await dealer.generate_task(room, "player_2", kind="penalty", photo=False, difficulty="medium")
        return loop.time() - started, comment, event, task

    elapsed, comment, event, task = asyncio.run(scenario())
    assert elapsed < 1.0                    # no waiting: fallback right away
    assert comment[1] == "fallback" and "Аня" in comment[0]
    assert event[2] == "fallback" and event[0]
    assert task[1] == "bank" and task[0]["title"]


# 9 ---------------------------------------------------------------------------------------------
def test_secrets_never_appear_in_logs_exceptions_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    server = Server(clock)
    client = make_client(server, clock)
    asyncio.run(client.generate_text("s", "u"))
    token = "token-1"
    server.fail_oauth = True
    clock.now += 4000                       # token expired, refresh fails
    errors = []
    try:
        asyncio.run(client.generate_text("s", "u"))
    except LLMUnavailable as error:
        errors.append(error)
    assert errors
    texts = [caplog.text, repr(client), repr(client.tokens), repr(client.config), str(errors[0]),
             repr(errors[0]), repr(errors[0].__cause__), repr(errors[0].__context__)]
    for text in texts:
        assert AUTH_KEY not in text
        assert token not in text
    assert "токен обновлён" in caplog.text
