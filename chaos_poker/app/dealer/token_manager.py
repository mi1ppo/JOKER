"""GigaChat access token: proactive refresh, single-flight, reactive refresh on 401 (section 15.2).

Neither the authorization key nor the token ever appear in logs, exception texts or repr.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import ssl
import time
import uuid
from typing import Any, Callable

import httpx

log = logging.getLogger("chaos.ai")

OAUTH_TIMEOUT = httpx.Timeout(10.0, connect=4.0)


class TokenUnavailable(Exception):
    """Token cannot be obtained right now (message contains no secrets)."""


def describe_error(error: BaseException) -> str:
    """Short secret-free label for status and logs: ssl | auth | timeout | network | limit | error."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return "ssl"
        if isinstance(current, httpx.HTTPStatusError):
            status = current.response.status_code
            if status in (401, 403):
                return "auth"
            if status == 429:
                return "limit"
            return "error"
        if isinstance(current, httpx.TimeoutException):
            return "timeout"
        if isinstance(current, httpx.ConnectError):
            # ConnectError wraps SSL failures as well: look at the cause first.
            cause = current.__cause__ or current.__context__
            if cause is not None and describe_error(cause) == "ssl":
                return "ssl"
            return "network"
        if isinstance(current, (httpx.NetworkError, OSError)):
            return "network"
        current = current.__cause__ or current.__context__
    return "error"


class TokenManager:
    REFRESH_MARGIN = 120   # start proactive refresh this many seconds before expiry
    HARD_MARGIN = 30       # below this the token is treated as expired
    COOLDOWN = 5           # after a failed refresh, fail fast for this many seconds
    JITTER = 10            # random spread of the proactive refresh moment
    IDLE_AFTER = 15 * 60   # skip background refreshes when AI was not used this long
    BACKOFF = (5, 10, 20, 40, 60)

    def __init__(self, http: httpx.AsyncClient, oauth_url: str, auth_key: str, scope: str, *,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], Any] = asyncio.sleep,
                 in_use: Callable[[], bool] | None = None) -> None:
        self._http = http
        self._url = oauth_url
        self._key = auth_key
        self._scope = scope
        self._now = now
        self._sleep = sleep
        # Returns True while some room needs AI functions (idle detection, optional).
        self._in_use = in_use
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._last_attempt = 0.0
        self._last_used = now()
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.refresh_count = 0

    def __repr__(self) -> str:
        return f"TokenManager(ready={self.is_ready})"   # never print the token or the key

    # ---- state ---------------------------------------------------------------------

    def _remaining(self) -> float:
        return self._expires_at - self._now()

    @property
    def is_ready(self) -> bool:
        return self._token is not None and self._remaining() > self.HARD_MARGIN

    @property
    def status(self) -> str:
        return "ready" if self.is_ready else "unavailable"

    @property
    def expires_at(self) -> float:
        return self._expires_at

    # ---- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        try:
            await self.refresh(force=True)
        except TokenUnavailable:
            pass                               # the app still starts, status shows the reason
        self._task = asyncio.create_task(self._refresher())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def warm_up(self) -> None:
        """Called when AI is about to be used again (AI dealer chosen, "check AI" button)."""
        self._last_used = self._now()
        try:
            await self.refresh()
        except TokenUnavailable:
            pass

    # ---- tokens ------------------------------------------------------------------------

    async def get_token(self) -> str:
        self._last_used = self._now()
        if self.is_ready:                      # fast path: no network, no lock
            return self._token  # type: ignore[return-value]
        await self.refresh()
        if not self.is_ready:
            raise TokenUnavailable(self.last_error or "no token")
        return self._token  # type: ignore[return-value]

    async def invalidate(self, bad_token: str) -> None:
        """The API answered 401 for `bad_token`: refresh once, unless somebody already did."""
        if self._token == bad_token:           # only if it is still the current one
            self._expires_at = 0.0
        await self.refresh()

    async def refresh(self, force: bool = False) -> None:
        seen = self._token
        async with self._lock:                 # single-flight
            if self._token != seen and self.is_ready:
                return                         # somebody refreshed while we were waiting
            # +JITTER: the background task wakes up REFRESH_MARGIN + jitter before expiry
            if not force and self.is_ready and self._remaining() > self.REFRESH_MARGIN + self.JITTER:
                return
            if self.last_error and self._now() - self._last_attempt < self.COOLDOWN:
                raise TokenUnavailable(self.last_error)          # fail fast
            self._last_attempt = self._now()
            try:
                self._token, self._expires_at = await self._fetch()
                self.last_error = None
                self.refresh_count += 1
                until = dt.datetime.fromtimestamp(self._expires_at).strftime("%H:%M")
                log.info("GigaChat: токен обновлён, действует до %s", until)
            except Exception as error:
                self.last_error = describe_error(error)
                log.warning("GigaChat: не удалось получить токен (%s)", self.last_error)
                raise TokenUnavailable(self.last_error) from None

    async def _fetch(self) -> tuple[str, float]:
        response = await self._http.post(
            self._url,
            headers={"Authorization": f"Basic {self._key}",
                     "RqUID": str(uuid.uuid4()),
                     "Accept": "application/json"},
            data={"scope": self._scope},       # httpx sets application/x-www-form-urlencoded
            timeout=OAUTH_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["access_token"], self._normalize_expiry(payload.get("expires_at"))

    def _normalize_expiry(self, raw: Any) -> float:
        now = self._now()
        try:
            expires = float(raw)
        except (TypeError, ValueError):
            return now + 25 * 60
        if expires > 1e11:
            expires /= 1000.0                  # milliseconds -> seconds
        if not now < expires < now + 3600:
            expires = now + 25 * 60            # sanity fallback
        return expires

    # ---- background refresh ---------------------------------------------------------------

    def _idle(self) -> bool:
        if self._in_use is None:
            return False
        return not self._in_use() and self._now() - self._last_used > self.IDLE_AFTER

    async def _refresher(self) -> None:
        failures = 0
        while True:
            try:
                if self.is_ready:
                    delay = self._remaining() - self.REFRESH_MARGIN - random.uniform(0, self.JITTER)
                    await self._sleep(max(delay, 1))
                if self._idle():
                    await self._sleep(60)      # nobody needs AI: do not refresh in vain
                    continue
                await self.refresh()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # The old token (if any) keeps working while we retry.
                await self._sleep(self.BACKOFF[min(failures, len(self.BACKOFF) - 1)])
                failures += 1
