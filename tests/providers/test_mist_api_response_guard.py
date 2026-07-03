"""Fail-closed guards for mistapi responses.

The mistapi SDK NEVER raises for HTTP failures: non-429 errors, connection
drops and proxy failures all come back as a normal-looking APIResponse whose
.data is {} (or the error body) and .status_code is the error code or None.
Unchecked `.data` access therefore compiles an empty-but-accepted baseline —
the one "confident wrong answer" path in the provider (review finding C1).
`_checked()` turns those into exceptions the existing attempt()/guardrail
machinery already handles (StateMeta failure or FetchError -> UNKNOWN), and
`_pages()` replaces mistapi.get_all, which silently truncates when a later
page fails (the failed page has data={} and next=None, ending the loop).
"""

from __future__ import annotations

from typing import Any

import pytest

from digital_twin.providers.base import FetchError, OrgScope, SiteScope
from digital_twin.providers.mist_api import MistApiError, MistApiProvider, _checked


class FakeResp:
    """Shape-compatible stand-in for mistapi.APIResponse."""

    def __init__(
        self,
        *,
        status_code: int | None = 200,
        data: Any = None,
        next: str | None = None,
        proxy_error: bool = False,
        url: str = "https://api.mist.test/api/v1/x",
    ) -> None:
        self.status_code = status_code
        self.data = {} if data is None else data
        self.next = next
        self.proxy_error = proxy_error
        self.url = url


class FakeSession:
    """Serves _pages' next-page fetches (mistapi.get_next -> session.mist_get)."""

    def __init__(self, pages: dict[str, FakeResp] | None = None) -> None:
        self._pages = pages or {}

    def mist_get(self, url: str) -> FakeResp:
        return self._pages[url]


def _provider(session: FakeSession | None = None) -> MistApiProvider:
    p = object.__new__(MistApiProvider)
    p._host = "test"
    p._session = session or FakeSession()  # type: ignore[assignment]
    return p


# -- _checked ----------------------------------------------------------------


def test_checked_passes_a_2xx_response_through() -> None:
    resp = FakeResp(status_code=200, data={"id": "s1"})
    assert _checked(resp) is resp


def test_checked_raises_on_http_error_status() -> None:
    with pytest.raises(MistApiError, match="502"):
        _checked(FakeResp(status_code=502, data={"detail": "bad gateway"}))


def test_checked_raises_on_connection_failure() -> None:
    # requests-level failure: the SDK returns APIResponse(response=None) with
    # status_code None and data {} — the exact silent-empty-baseline shape
    with pytest.raises(MistApiError, match="connection"):
        _checked(FakeResp(status_code=None))


def test_checked_raises_on_proxy_error() -> None:
    with pytest.raises(MistApiError, match="proxy"):
        _checked(FakeResp(status_code=None, proxy_error=True))


def test_checked_raises_on_error_body_despite_2xx() -> None:
    with pytest.raises(MistApiError, match="quota"):
        _checked(FakeResp(status_code=200, data={"error": "quota exceeded"}))


def test_checked_error_names_url_but_never_a_token() -> None:
    resp = FakeResp(status_code=403, url="https://api.mist.test/api/v1/sites/s1")
    with pytest.raises(MistApiError, match="api/v1/sites/s1"):
        _checked(resp)


# -- _pages ------------------------------------------------------------------


def test_pages_returns_a_single_list_page() -> None:
    p = _provider()
    resp = FakeResp(data=[{"id": "a"}, {"id": "b"}])
    assert p._pages(resp) == [{"id": "a"}, {"id": "b"}]


def test_pages_returns_a_results_dict_page() -> None:
    p = _provider()
    resp = FakeResp(data={"results": [{"mac": "aa"}], "total": 1})
    assert p._pages(resp) == [{"mac": "aa"}]


def test_pages_follows_next_and_concatenates() -> None:
    p = _provider(FakeSession({"/page2": FakeResp(data=[{"id": "b"}])}))
    first = FakeResp(data=[{"id": "a"}], next="/page2")
    assert p._pages(first) == [{"id": "a"}, {"id": "b"}]


def test_pages_raises_when_the_first_page_failed() -> None:
    p = _provider()
    with pytest.raises(MistApiError):
        p._pages(FakeResp(status_code=500))


def test_pages_raises_when_a_later_page_fails_instead_of_truncating() -> None:
    # mistapi.get_all would return just page 1 here (silent truncation)
    p = _provider(FakeSession({"/page2": FakeResp(status_code=None)}))
    first = FakeResp(data=[{"id": "a"}], next="/page2")
    with pytest.raises(MistApiError, match="connection"):
        p._pages(first)


def test_pages_raises_on_an_unexpected_page_shape() -> None:
    # a dict without "results" is not a list page — fail closed, never guess
    p = _provider()
    with pytest.raises(MistApiError, match="shape"):
        p._pages(FakeResp(data={"detail": "weird"}))


# -- endpoint wiring ----------------------------------------------------------


def test_site_endpoint_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import mistapi

    monkeypatch.setattr(
        mistapi.api.v1.sites.sites,
        "getSiteInfo",
        lambda session, site_id: FakeResp(status_code=502),
    )
    with pytest.raises(MistApiError, match="502"):
        _provider()._site(SiteScope(org_id="o1", site_id="s1"))


def test_port_stats_endpoint_raises_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mistapi

    monkeypatch.setattr(
        mistapi.api.v1.sites.stats,
        "searchSiteSwOrGwPorts",
        lambda session, site_id: FakeResp(status_code=None),
    )
    with pytest.raises(MistApiError, match="connection"):
        _provider()._port_stats(SiteScope(org_id="o1", site_id="s1"))


def test_org_nac_rules_failure_is_fetch_error_not_empty_ruleset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pre-guard behavior: a failed listOrgNacRules yielded data={} -> rules=()
    # accepted as a successful empty ruleset (false-SAFE for org-NAC verdicts)
    import mistapi

    monkeypatch.setattr(
        mistapi.api.v1.orgs.nacrules,
        "listOrgNacRules",
        lambda session, org_id: FakeResp(status_code=503),
    )
    result = _provider().resolve_org_nac(OrgScope(org_id="o1"))
    assert isinstance(result, FetchError)
    assert result.failures[0].object == "nacrules"


def test_org_nac_rules_are_paginated(monkeypatch: pytest.MonkeyPatch) -> None:
    # pre-guard behavior: .data read the FIRST page only
    import mistapi

    monkeypatch.setattr(
        mistapi.api.v1.orgs.nacrules,
        "listOrgNacRules",
        lambda session, org_id: FakeResp(data=[{"id": "r1"}], next="/nac2"),
    )
    monkeypatch.setattr(
        mistapi.api.v1.orgs.nactags,
        "listOrgNacTags",
        lambda session, org_id: FakeResp(data=[]),
    )
    p = _provider(FakeSession({"/nac2": FakeResp(data=[{"id": "r2"}])}))
    result = p.resolve_org_nac(OrgScope(org_id="o1"))
    assert not isinstance(result, FetchError)
    assert [r["id"] for r in result.rules] == ["r1", "r2"]
