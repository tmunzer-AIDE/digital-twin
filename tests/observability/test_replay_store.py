import json

from digital_twin.observability.replay.store import (
    FixtureProvider,
    ReplayStore,
    load_fixture_raw,
)
from digital_twin.providers.base import RawSiteState, SiteScope
from tests.adapters.mist.fixtures import raw_site


def test_save_writes_redacted_fixture(tmp_path):
    store = ReplayStore(tmp_path)
    raw = raw_site(
        devices=(
            {
                "mac": "aa:bb:cc:dd:ee:01",
                "id": "d1",
                "type": "switch",
                "name": "real-name",
                "port_config": {},
            },
        )
    )
    path = store.save_raw("run1", raw)
    data = json.loads(path.read_text())
    blob = json.dumps(data)
    assert "aa:bb:cc:dd:ee:01" not in blob and "real-name" not in blob  # redacted
    from digital_twin.observability.replay.redaction import REDACTION_VERSION

    assert data["redaction_version"] == REDACTION_VERSION
    assert data["scope"]["org_id"]  # structure intact


def test_load_round_trips_to_raw_site_state(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site())
    raw = load_fixture_raw(path)
    assert isinstance(raw, RawSiteState)
    assert isinstance(raw.scope, SiteScope)
    assert raw.devices and raw.setting  # payloads intact (values redacted)


def test_wlans_round_trip_and_default_when_absent(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site(wlans=({"ssid": "corp", "vlan_id": 10},)))
    raw = load_fixture_raw(path)
    assert raw.wlans and raw.wlans[0]["vlan_id"] == 10
    # a fixture predating WLAN support (no "wlans" key) loads as empty, not a crash
    data = json.loads(path.read_text())
    del data["wlans"]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(data))
    assert load_fixture_raw(legacy).wlans == ()


def test_fixture_provider_serves_matching_scope(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site())
    provider = FixtureProvider(path)
    fixture_scope = provider.fixture_scope
    raw = provider.fetch_site(fixture_scope)
    assert isinstance(raw, RawSiteState)


def test_fixture_provider_rejects_mismatched_scope(tmp_path):
    # scope is explicit in the contract: simulating site A against fixture B
    # must be a FetchError (-> UNKNOWN), never a silently-wrong verdict
    from digital_twin.providers.base import FetchError

    path = ReplayStore(tmp_path).save_raw("run1", raw_site())
    provider = FixtureProvider(path)
    err = provider.fetch_site(SiteScope("other-org", "other-site"))
    assert isinstance(err, FetchError)
    assert any("fixture" in f.error for f in err.failures)


def test_fixture_provider_strict_false_escape_hatch(tmp_path):
    path = ReplayStore(tmp_path).save_raw("run1", raw_site())
    provider = FixtureProvider(path, strict=False)
    raw = provider.fetch_site(SiteScope("ignored", "ignored"))
    assert isinstance(raw, RawSiteState)


def test_fetch_sites_applies_the_same_strict_scope_guard(tmp_path):
    # the batched path must not keep a silent wrong-scope hole open
    from digital_twin.providers.base import FetchError, OrgScope

    path = ReplayStore(tmp_path).save_raw("run1", raw_site())
    provider = FixtureProvider(path)
    fixture_scope = provider.fixture_scope

    # wrong org -> every requested site is a FetchError
    out = provider.fetch_sites(OrgScope("other-org"), [fixture_scope.site_id])
    assert all(isinstance(v, FetchError) for v in out.values())

    # right org, wrong site -> FetchError for that site
    out = provider.fetch_sites(OrgScope(fixture_scope.org_id), ["ghost-site"])
    assert isinstance(out["ghost-site"], FetchError)

    # right org, fixture site -> served
    out = provider.fetch_sites(OrgScope(fixture_scope.org_id), [fixture_scope.site_id])
    assert isinstance(out[fixture_scope.site_id], RawSiteState)


def test_save_run_includes_plan_verdict_and_trace(tmp_path):
    from digital_twin.observability.trace import Trace

    store = ReplayStore(tmp_path)
    path = store.save_run(
        "run2",
        raw=raw_site(),
        plan={"source": "mist"},
        verdict_doc={"decision": "safe"},
        trace=Trace(run_id="run2"),
    )
    data = json.loads(path.read_text())
    assert data["plan"]["source"] == "mist"
    assert data["verdict"]["decision"] == "safe"
    assert data["trace"]["run_id"] == "run2"
