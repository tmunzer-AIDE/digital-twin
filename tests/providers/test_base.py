from collections.abc import Sequence
from datetime import UTC, datetime

from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    NacFetch,
    OrgScope,
    OrgTemplateContext,
    OrgWlanContext,
    OrgWlanTemplateContext,
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
    class Fake:
        def fetch_site(
            self, scope: SiteScope, *, include_derived: bool = False
        ) -> RawSiteState | FetchError:
            raise NotImplementedError

        def fetch_sites(
            self,
            scope: OrgScope,
            site_ids: Sequence[str] | None = None,
            *,
            include_derived: bool = False,
        ) -> dict[str, RawSiteState | FetchError]:
            raise NotImplementedError

        def resolve_org_template(
            self, scope: OrgScope, template_id: str, object_type: str
        ) -> OrgTemplateContext | FetchError:
            raise NotImplementedError

        def resolve_org_wlan(self, scope: OrgScope, wlan_id: str) -> OrgWlanContext | FetchError:
            raise NotImplementedError

        def resolve_org_wlan_template(
            self, scope: OrgScope, template_id: str
        ) -> OrgWlanTemplateContext | FetchError:
            raise NotImplementedError

        def resolve_org_nac(self, scope: OrgScope) -> NacFetch | FetchError:
            raise NotImplementedError

    provider: StateProvider = Fake()
    assert provider is not None


def test_org_scope_construct():
    assert OrgScope(org_id="o1").org_id == "o1"


def test_rawsitestate_has_new_template_fields():
    fields = RawSiteState.__dataclass_fields__
    assert "sitetemplate" in fields and "gatewaytemplate" in fields


def test_org_template_context_and_orgscope_fetch_error():
    from datetime import UTC, datetime

    from digital_twin.providers.base import FetchError, OrgScope, OrgTemplateContext
    ctx = OrgTemplateContext(template={"id": "nt1"}, assigned_site_ids=("s1", "s2"))
    assert ctx.assigned_site_ids == ("s1", "s2")
    err = FetchError(
        scope=OrgScope(org_id="o1"), failures=(), acquired_at=datetime.now(UTC), host="h"
    )
    assert err.scope.org_id == "o1"


def test_org_wlan_context():
    ctx = OrgWlanContext(
        wlan={"id": "w1", "enabled": False},
        derived_rows_by_site={"s1": {"id": "w1", "enabled": True}},
    )
    assert ctx.wlan["id"] == "w1"
    assert ctx.derived_rows_by_site["s1"]["enabled"] is True


def test_org_wlan_template_context():
    ctx = OrgWlanTemplateContext(
        template={"id": "tmpl1", "name": "Guest template"},
        derived_rows_by_site={
            "s1": (
                {"id": "w1", "ssid": "guest", "template_id": "tmpl1"},
                {"id": "w2", "ssid": "iot", "template_id": "tmpl1"},
            )
        },
    )
    assert ctx.template["id"] == "tmpl1"
    assert [row["id"] for row in ctx.derived_rows_by_site["s1"]] == ["w1", "w2"]
