from datetime import UTC, datetime

from digital_twin.providers.base import (
    FetchFailure,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
)


def test_scope_and_meta_construct():
    scope = SiteScope(org_id="o1", site_id="s1")
    meta = StateMeta(
        acquired_at=datetime(2026, 6, 9, tzinfo=UTC),
        host="api.eu.mist.com",
        fetched=("site", "setting"),
        failures=(FetchFailure(object="derived", error="404"),),
    )
    assert scope.site_id == "s1"
    assert meta.failures[0].object == "derived"
    assert meta.is_complete is False


def test_meta_complete_when_no_failures():
    meta = StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site",), failures=())
    assert meta.is_complete is True


def test_raw_site_state_holds_vendor_payloads():
    raw = RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": "nt1"},
        setting={"networks": {}},
        networktemplate={"name": "NT"},
        devices=({"mac": "aa", "type": "switch"},),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=(), failures=()),
    )
    assert raw.site["networktemplate_id"] == "nt1"
    assert raw.derived_setting is None


def test_total_fetch_failure_is_a_value_not_an_exception():
    from digital_twin.providers.base import FetchError

    err = FetchError(
        scope=SiteScope(org_id="o1", site_id="s1"),
        failures=(FetchFailure(object="setting", error="503"),),
        acquired_at=datetime.now(UTC),
        host="api.eu.mist.com",
    )
    assert err.failures[0].object == "setting"


def test_state_provider_is_a_protocol():
    from digital_twin.providers.base import FetchError

    class Fake:
        def fetch_site(
            self, scope: SiteScope, *, include_derived: bool = False
        ) -> RawSiteState | FetchError:
            raise NotImplementedError

    provider: StateProvider = Fake()
    assert provider is not None
