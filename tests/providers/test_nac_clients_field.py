from datetime import UTC, datetime

from digital_twin.observability.replay.store import load_fixture_doc
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _meta() -> StateMeta:
    return StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=())


def test_raw_site_state_defaults_nac_clients_empty():
    raw = RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=(), wired_clients=(), derived_setting=None, meta=_meta(),
    )
    assert raw.nac_clients == ()


def test_load_fixture_doc_carries_nac_clients_and_tolerates_absence():
    base = {
        "scope": {"org_id": "o1", "site_id": "s1"}, "site": {}, "setting": {},
        "networktemplate": None, "devices": [], "device_stats": [], "port_stats": [],
        "wireless_clients": [], "wired_clients": [], "derived_setting": None,
        "meta": {"acquired_at": datetime.now(UTC).isoformat(), "host": "t",
                 "fetched": [], "failures": []},
    }
    assert load_fixture_doc(base).nac_clients == ()  # pre-feature fixtures
    withnac = {**base, "nac_clients": [{"mac": "aa", "last_family": "Printer"}]}
    assert load_fixture_doc(withnac).nac_clients == ({"mac": "aa", "last_family": "Printer"},)
