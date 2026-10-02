"""Version-tested mistapi 0.62 transport compatibility boundary.

Generated SDK endpoints remain the API boundary. Its private Requests session
is retained (credentials, proxy, TLS settings and cookies), and receives a
bounded adapter. SDK 429 retries are disabled to keep one effective retry
budget. No dependency internals are changed on disk or globally monkeypatched.
"""

from __future__ import annotations

import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import mistapi
from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ProxyError, SSLError, Timeout
from urllib3.util import Timeout as SocketTimeout

from .fetch_limits import FetchLimits, fetch_budget, remaining

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _retry_after(response: Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class _BoundedAdapter(HTTPAdapter):
    def __init__(self, limits: FetchLimits) -> None:
        super().__init__(max_retries=0)
        self.limits = limits
        self.failure: Exception | None = None

    def send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        try:
            return self._send(request, *args, **kwargs)
        except Exception as exc:
            self.failure = exc
            raise

    def _send(self, request: PreparedRequest, *args: Any, **kwargs: Any) -> Response:
        attempts = self.limits.max_attempts if request.method == "GET" else 1
        for attempt in range(attempts):
            seconds = remaining(self.limits)
            kwargs["timeout"] = SocketTimeout(
                total=seconds,
                connect=min(self.limits.connect_timeout, seconds),
                read=min(self.limits.read_timeout, seconds),
            )
            delay: float | None = None
            try:
                response = super().send(request, *args, **kwargs)
            except (ConnectionError, Timeout) as exc:
                if isinstance(exc, (SSLError, ProxyError)) or attempt + 1 == attempts:
                    raise
            else:
                if response.status_code not in _RETRYABLE or attempt + 1 == attempts:
                    return response
                delay = _retry_after(response)
                response.close()
            if delay is None:
                delay = random.uniform(0.0, min(0.5 * 2**attempt, self.limits.backoff_cap))
            # Honor server guidance; never shorten Retry-After and retry early.
            if delay >= remaining(self.limits):
                raise Timeout("retry delay exceeds remaining Mist fetch budget")
            time.sleep(delay)
        raise AssertionError("positive attempt limit guarantees a result or exception")


class BoundedMistSession(mistapi.APISession):  # type: ignore[misc]  # untyped vendor base
    # mistapi 0.62 uses this class attribute for its own HTTP-429 retry loop.
    _MAX_429_RETRIES = 0

    def __init__(self, *, host: str, apitoken: str, limits: FetchLimits) -> None:
        # set_api_token's default validation performs a standalone requests.get
        # during construction, outside our transport/budget. Defer validation to
        # the scoped API reads, whose 401/403 responses the provider fails closed.
        super().__init__(host=host)
        if not isinstance(self._session, Session):
            raise RuntimeError("unsupported mistapi transport; expected a Requests session")
        self._fetch_limits = limits
        self._bounded_adapter = _BoundedAdapter(limits)
        self._session.mount("https://", self._bounded_adapter)
        self.set_api_token(apitoken, validate=False)

    def _load_env(self, env_file: str | None = None) -> None:
        # The provider has already selected the host and credential explicitly.
        # Prevent ambient SDK credentials triggering eager validation or fallback.
        return

    def mist_get(self, uri: str, query: dict[str, str] | None = None) -> Any:
        with fetch_budget(self._fetch_limits):
            self._bounded_adapter.failure = None
            response = super().mist_get(uri, query)
            # SDK failures are normally swallowed into status_code=None. Preserve
            # our named transport failures for the provider's errors-as-values path.
            if response.status_code is None and self._bounded_adapter.failure is not None:
                raise self._bounded_adapter.failure
            remaining(self._fetch_limits)
            return response
