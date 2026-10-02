"""Exercise the locked SDK's real request path without contacting Mist."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ReadTimeout, Timeout

from digital_twin.engine.pipeline import simulate
from digital_twin.providers import fetch_limits, mist_transport
from digital_twin.providers.base import FetchError, SiteScope
from digital_twin.providers.fetch_limits import FetchDeadlineExceeded, FetchLimits, fetch_budget
from digital_twin.providers.mist_api import MistApiError, MistApiProvider
from digital_twin.verdict.decision import Decision
from tests.engine.test_pipeline import _op, _plan

SCOPE = SiteScope("o1", "s1")


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(fetch_limits.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mist_transport.time, "sleep", clock.sleep)
    monkeypatch.setattr(mist_transport.random, "uniform", lambda low, high: high)
    return clock


def _response(request, status=200, data=None, headers=None):
    response = Response()
    response.status_code = status
    response.url = request.url
    response.request = request
    response._content = json.dumps({"id": "s1"} if data is None else data).encode()
    response._content_consumed = True
    response.headers.update({"Content-Type": "application/json", **(headers or {})})
    return response


def _provider(**limits):
    return MistApiProvider(
        host="api.mist.com", apitoken="synthetic-test-token", fetch_limits=FetchLimits(**limits),
    )


def test_construction_is_offline_and_uses_only_selected_credentials(monkeypatch):
    def unexpected_request(*args, **kwargs):
        raise AssertionError("provider construction must not make a request")

    monkeypatch.setattr(HTTPAdapter, "send", unexpected_request)
    monkeypatch.setenv("MIST_HOST", "api.eu.mist.com")
    monkeypatch.setenv("MIST_APITOKEN", "unrelated-ambient-token")
    provider = _provider()
    assert provider._session._session.headers["Authorization"] == "Token synthetic-test-token"
    assert provider._host == "api.mist.com"


def test_cli_recording_wrapper_preserves_custom_fetch_budget(monkeypatch, clock):
    from digital_twin.drivers.cli import _RecordingProvider

    timeouts = []

    def send(adapter, request, **kwargs):
        timeouts.append(kwargs["timeout"].total)
        raise ReadTimeout("synthetic timeout")

    monkeypatch.setattr(HTTPAdapter, "send", send)
    provider = _RecordingProvider(_provider(total_timeout=7, max_attempts=1))
    verdict = simulate(_plan([_op(payload={})]), provider=provider)
    assert verdict.decision is Decision.UNKNOWN
    assert timeouts == [7.0, 7.0]


def test_sdk_get_has_socket_timeouts(monkeypatch, clock):
    observed = []

    def send(adapter, request, **kwargs):
        observed.append(kwargs["timeout"])
        return _response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert _provider()._site(SCOPE) == {"id": "s1"}
    assert len(observed) == 1
    assert observed[0].connect_timeout == 5.0
    assert observed[0].read_timeout == 20.0
    assert observed[0].total == 120.0


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_retries_then_recovers(monkeypatch, clock, status):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        return _response(request, status if len(calls) < 3 else 200)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert _provider()._site(SCOPE) == {"id": "s1"}
    assert len(calls) == 3
    assert clock.sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_status_does_not_retry(monkeypatch, clock, status):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        return _response(request, status)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(MistApiError, match=f"HTTP {status}"):
        _provider()._site(SCOPE)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_sdk_does_not_multiply_429_retries(monkeypatch, clock):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        return _response(request, 429)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(MistApiError, match="HTTP 429"):
        _provider(max_attempts=2)._site(SCOPE)
    assert len(calls) == 2
    assert clock.sleeps == [0.5]


def test_connection_failure_retries_then_recovers(monkeypatch, clock):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise ConnectionError("synthetic connection failure")
        return _response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert _provider()._site(SCOPE) == {"id": "s1"}
    assert len(calls) == 2


def test_read_timeout_is_a_named_fetch_failure(monkeypatch, clock):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        raise ReadTimeout("synthetic read timeout")

    monkeypatch.setattr(HTTPAdapter, "send", send)
    provider = _provider()
    monkeypatch.setattr(provider, "_setting", lambda scope: {})
    fetched = provider.fetch_site(SCOPE)
    assert isinstance(fetched, FetchError)
    assert fetched.failures[0].object == "site"
    assert "read timeout" in fetched.failures[0].error
    assert len(calls) == 3


def test_timeout_reaches_pipeline_as_unknown(monkeypatch, clock):
    monkeypatch.setattr(
        HTTPAdapter, "send", lambda *args, **kwargs: (_ for _ in ()).throw(ReadTimeout("timeout")),
    )
    verdict = simulate(_plan([_op(payload={"notes": "cosmetic"})]), provider=_provider())
    assert verdict.decision is Decision.UNKNOWN
    assert verdict.state_meta.fetch_failures
    assert any("timeout" in error for _, error in verdict.state_meta.fetch_failures)


def test_retry_after_is_honored(monkeypatch, clock):
    calls = []

    def send(adapter, request, **kwargs):
        calls.append(request)
        return _response(request, 429, headers={"Retry-After": "2"}) if len(calls) == 1 else (
            _response(request)
        )

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert _provider()._site(SCOPE) == {"id": "s1"}
    assert clock.sleeps == [2.0]


def test_retry_after_beyond_budget_fails_without_waiting(monkeypatch, clock):
    monkeypatch.setattr(HTTPAdapter, "send", lambda adapter, request, **kwargs: (
        _response(request, 429, headers={"Retry-After": "999999"})
    ))
    with pytest.raises(Timeout, match="remaining Mist fetch budget"):
        _provider()._site(SCOPE)
    assert clock.sleeps == []


def test_http_date_retry_after(monkeypatch):
    now = datetime(2026, 10, 2, tzinfo=UTC)

    class FixedDateTime:
        @staticmethod
        def now(tz):
            return now

    monkeypatch.setattr(mist_transport, "datetime", FixedDateTime)
    response = Response()
    response.headers["Retry-After"] = format_datetime(now + timedelta(seconds=3), usegmt=True)
    assert mist_transport._retry_after(response) == 3.0


def test_nested_sdk_requests_share_the_remaining_budget(monkeypatch, clock):
    timeouts = []

    def send(adapter, request, **kwargs):
        timeouts.append(kwargs["timeout"].total)
        clock.now += 3
        return _response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    provider = _provider(total_timeout=10)
    with fetch_budget(provider._fetch_limits):
        provider._site(SCOPE)
        provider._setting(SCOPE)
    assert timeouts == [10.0, 7.0]
    # A new run receives its own budget; the old execution context is reset.
    provider._site(SCOPE)
    assert timeouts[-1] == 10.0


def test_deadline_does_not_accept_a_late_success(monkeypatch, clock):
    def send(adapter, request, **kwargs):
        clock.now += 11
        return _response(request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(FetchDeadlineExceeded, match="deadline exceeded"):
        _provider(total_timeout=10)._site(SCOPE)


def test_expired_budget_stops_new_requests(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(HTTPAdapter, "send", lambda adapter, request, **kwargs: (
        calls.append(request) or _response(request)
    ))
    provider = _provider(total_timeout=10)
    with fetch_budget(provider._fetch_limits):
        clock.now += 11
        with pytest.raises(FetchDeadlineExceeded):
            provider._site(SCOPE)
    assert calls == []


@pytest.mark.parametrize("kwargs", [
    {"connect_timeout": 0}, {"read_timeout": float("inf")}, {"total_timeout": -1},
    {"total_timeout": float("nan")}, {"max_attempts": 0}, {"max_pages": True}, {"max_rows": 1.5},
])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        FetchLimits(**kwargs)
