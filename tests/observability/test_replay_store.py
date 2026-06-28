import dataclasses
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


def test_ospf_neighbors_round_trip_and_default_when_absent(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site(ospf_neighbors=(
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "area": "0", "state": "Full"},)))
    raw = load_fixture_raw(path)
    # assert on `state` (redaction remaps mac/peer_ip on save; state survives unchanged)
    assert raw.ospf_neighbors and raw.ospf_neighbors[0]["state"] == "Full"
    # a fixture predating GS27 (no "ospf_neighbors" key) loads as empty, not a crash
    data = json.loads(path.read_text())
    del data["ospf_neighbors"]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(data))
    assert load_fixture_raw(legacy).ospf_neighbors == ()


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


def _site_doc(store, run_id, raw):
    # round-trip a raw_site through the store's redacted-doc writer to get a
    # valid single-site fixture doc (the exact shape load_fixture_doc consumes)
    return json.loads(store.save_raw(run_id, raw).read_text())


def test_multisite_resolve_filters_assigned_sites_and_returns_template(tmp_path):
    from digital_twin.providers.base import OrgScope, OrgTemplateContext

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    b = _site_doc(store, "b", raw_site())
    a["site"]["networktemplate_id"] = "nt1"
    a["scope"]["site_id"] = "siteA"
    b["site"]["networktemplate_id"] = "nt1"
    b["scope"]["site_id"] = "siteB"
    doc = {
        "template": {"id": "nt1", "name": "shared", "networks": {"corp": {"vlan_id": 10}}},
        "sites": {"siteA": a, "siteB": b},
    }
    path = tmp_path / "ms.json"
    path.write_text(json.dumps(doc))
    provider = FixtureProvider(path)

    resolved = provider.resolve_org_template(OrgScope("o1"), "nt1", "networktemplate")
    assert isinstance(resolved, OrgTemplateContext)
    assert set(resolved.assigned_site_ids) == {"siteA", "siteB"}
    assert resolved.template["networks"]["corp"]["vlan_id"] == 10

    fetched = provider.fetch_sites(OrgScope("o1"), ["siteA", "siteB"])
    assert all(isinstance(v, RawSiteState) for v in fetched.values())
    assert len(fetched) == 2


def test_multisite_resolve_excludes_other_templates_and_marks_fetch_failures(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope, OrgTemplateContext

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    b = _site_doc(store, "b", raw_site())
    a["site"]["networktemplate_id"] = "nt1"
    a["scope"]["site_id"] = "siteA"
    b["site"]["networktemplate_id"] = "nt2"  # different template -> not assigned to nt1
    b["scope"]["site_id"] = "siteB"
    doc = {
        "template": {"id": "nt1", "networks": {}},
        "sites": {"siteA": a, "siteB": b},
        "fetch_failures": ["siteA"],
    }
    path = tmp_path / "ms.json"
    path.write_text(json.dumps(doc))
    provider = FixtureProvider(path)

    resolved = provider.resolve_org_template(OrgScope("o1"), "nt1", "networktemplate")
    assert isinstance(resolved, OrgTemplateContext)
    assert resolved.assigned_site_ids == ("siteA",)  # nt2 site excluded

    # siteA is a declared fetch failure -> FetchError; siteB still served
    out = provider.fetch_sites(OrgScope("o1"), ["siteA", "siteB"])
    assert isinstance(out["siteA"], FetchError)
    assert isinstance(out["siteB"], RawSiteState)


def _ms_one_site(tmp_path):
    """A minimal 1-site multi-site fixture (template nt1, site assigned to it)."""
    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    a["site"]["networktemplate_id"] = "nt1"
    a["scope"]["site_id"] = "siteA"
    doc = {"template": {"id": "nt1", "networks": {}}, "sites": {"siteA": a}}
    path = tmp_path / "ms.json"
    path.write_text(json.dumps(doc))
    return FixtureProvider(path)


def test_multisite_missing_template_is_fetch_error_not_zero_assigned(tmp_path):
    # a template_id the fixture does NOT hold must be a FetchError (-> UNKNOWN),
    # never a 0-assigned SUCCESS that would resolve SAFE
    from digital_twin.providers.base import FetchError, OrgScope

    provider = _ms_one_site(tmp_path)
    r = provider.resolve_org_template(OrgScope("o1"), "missing-template", "networktemplate")
    assert isinstance(r, FetchError)
    assert any("not found" in f.error for f in r.failures)


def test_multisite_rejects_wrong_org_on_resolve_and_fetch(tmp_path):
    # replaying a multi-site fixture against a DIFFERENT org must FetchError
    # (mirrors the single-site strict-scope guard), never silently succeed
    from digital_twin.providers.base import FetchError, OrgScope

    provider = _ms_one_site(tmp_path)
    result = provider.resolve_org_template(OrgScope("WRONG-ORG"), "nt1", "networktemplate")
    assert isinstance(result, FetchError)
    out = provider.fetch_sites(OrgScope("WRONG-ORG"), ["siteA"])
    assert isinstance(out["siteA"], FetchError)


def test_new_template_fields_round_trip(tmp_path):
    store = ReplayStore(tmp_path)
    raw = dataclasses.replace(
        raw_site(), sitetemplate={"networks": {}}, gatewaytemplate={"port_config": {}}
    )
    loaded = load_fixture_raw(store.save_raw("r", raw))
    assert loaded.sitetemplate == {"networks": {}}
    assert loaded.gatewaytemplate == {"port_config": {}}


def test_legacy_fixture_without_new_fields_loads_as_none(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("r", raw_site())
    data = json.loads(path.read_text())
    data.pop("sitetemplate", None)
    data.pop("gatewaytemplate", None)
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(data))
    assert load_fixture_raw(p).sitetemplate is None
    assert load_fixture_raw(p).gatewaytemplate is None


# ---------------------------------------------------------------------------
# T19: typed multi-template FixtureProvider
# ---------------------------------------------------------------------------


def _ms_typed_gatewaytemplate(tmp_path):
    """A multi-site fixture using the new typed 'templates' key with a gatewaytemplate."""
    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    b = _site_doc(store, "b", raw_site())
    c = _site_doc(store, "c", raw_site())
    a["site"]["gatewaytemplate_id"] = "g1"
    a["scope"]["site_id"] = "siteA"
    b["site"]["gatewaytemplate_id"] = "g1"
    b["scope"]["site_id"] = "siteB"
    c["site"]["gatewaytemplate_id"] = "g2"  # different gateway template
    c["scope"]["site_id"] = "siteC"
    # ensure org_id is consistent (raw_site produces a deterministic org_id)
    org_id = a["scope"]["org_id"]
    b["scope"]["org_id"] = org_id
    c["scope"]["org_id"] = org_id
    doc = {
        "templates": {
            "gatewaytemplate": {
                "g1": {"id": "g1", "name": "gw-template-1", "ip_config": {"type": "dhcp"}},
                "g2": {"id": "g2", "name": "gw-template-2"},
            }
        },
        "sites": {"siteA": a, "siteB": b, "siteC": c},
    }
    path = tmp_path / "ms_gw.json"
    path.write_text(json.dumps(doc))
    return FixtureProvider(path)


def test_typed_templates_gatewaytemplate_resolves_correct_sites(tmp_path):
    """New 'templates' shape: resolve_org_template with object_type='gatewaytemplate'
    returns only sites with matching gatewaytemplate_id and the correct template body."""
    from digital_twin.providers.base import OrgScope, OrgTemplateContext

    provider = _ms_typed_gatewaytemplate(tmp_path)
    resolved = provider.resolve_org_template(OrgScope("o1"), "g1", "gatewaytemplate")
    assert isinstance(resolved, OrgTemplateContext)
    assert set(resolved.assigned_site_ids) == {"siteA", "siteB"}
    assert resolved.template["ip_config"]["type"] == "dhcp"


def test_typed_templates_gatewaytemplate_excludes_other_type_sites(tmp_path):
    """Sites assigned to a different gatewaytemplate_id are excluded."""
    from digital_twin.providers.base import OrgScope, OrgTemplateContext

    provider = _ms_typed_gatewaytemplate(tmp_path)
    resolved = provider.resolve_org_template(OrgScope("o1"), "g2", "gatewaytemplate")
    assert isinstance(resolved, OrgTemplateContext)
    assert set(resolved.assigned_site_ids) == {"siteC"}


def test_typed_templates_missing_gatewaytemplate_is_fetch_error(tmp_path):
    """Requesting a gatewaytemplate_id not in the fixture -> FetchError, not 0-assigned SUCCESS."""
    from digital_twin.providers.base import FetchError, OrgScope

    provider = _ms_typed_gatewaytemplate(tmp_path)
    r = provider.resolve_org_template(OrgScope("o1"), "nope", "gatewaytemplate")
    assert isinstance(r, FetchError)
    assert any("not found" in f.error for f in r.failures)


def test_typed_templates_wrong_org_is_fetch_error(tmp_path):
    """Wrong org on a typed-templates fixture -> FetchError (unchanged strictness)."""
    from digital_twin.providers.base import FetchError, OrgScope

    provider = _ms_typed_gatewaytemplate(tmp_path)
    r = provider.resolve_org_template(OrgScope("WRONG-ORG"), "g1", "gatewaytemplate")
    assert isinstance(r, FetchError)


def test_legacy_template_key_still_resolves_as_networktemplate(tmp_path):
    """Back-compat: the legacy 'template' key is folded in as networktemplate and
    still resolves via resolve_org_template(..., '<id>', 'networktemplate')."""
    from digital_twin.providers.base import OrgScope, OrgTemplateContext

    # _ms_one_site uses the legacy 'template' key
    provider = _ms_one_site(tmp_path)
    resolved = provider.resolve_org_template(OrgScope("o1"), "nt1", "networktemplate")
    assert isinstance(resolved, OrgTemplateContext)
    assert set(resolved.assigned_site_ids) == {"siteA"}


def test_multisite_org_wlan_resolves_from_site_derived_rows(tmp_path):
    from digital_twin.providers.base import OrgScope, OrgWlanContext

    store = ReplayStore(tmp_path)
    a = _site_doc(
        store,
        "a",
        raw_site(wlans=({"id": "w1", "ssid": "corp", "enabled": True, "apply_to": "site"},)),
    )
    b = _site_doc(store, "b", raw_site(wlans=({"id": "other", "ssid": "guest"},)))
    a["scope"]["site_id"] = "siteA"
    b["scope"]["site_id"] = "siteB"
    doc = {
        "org_wlans": {"w1": {"id": "w1", "ssid": "corp", "enabled": False}},
        "sites": {"siteA": a, "siteB": b},
    }
    path = tmp_path / "ms_wlan.json"
    path.write_text(json.dumps(doc))

    resolved = FixtureProvider(path).resolve_org_wlan(OrgScope("o1"), "w1")

    assert isinstance(resolved, OrgWlanContext)
    assert resolved.wlan["enabled"] is False
    assert set(resolved.derived_rows_by_site) == {"siteA"}
    assert resolved.derived_rows_by_site["siteA"]["enabled"] is True


def test_multisite_org_wlan_missing_is_fetch_error(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    doc = {"org_wlans": {}, "sites": {"siteA": a}}
    path = tmp_path / "ms_wlan.json"
    path.write_text(json.dumps(doc))

    result = FixtureProvider(path).resolve_org_wlan(OrgScope("o1"), "missing")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlan"


def test_multisite_org_wlan_wrong_org_is_fetch_error(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site(wlans=({"id": "w1"},)))
    doc = {"org_wlans": {"w1": {"id": "w1"}}, "sites": {"siteA": a}}
    path = tmp_path / "ms_wlan.json"
    path.write_text(json.dumps(doc))

    result = FixtureProvider(path).resolve_org_wlan(OrgScope("other-org"), "w1")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "fixture"


def test_multisite_org_wlan_membership_includes_later_fetch_failure(tmp_path):
    from digital_twin.providers.base import OrgScope, OrgWlanContext

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site(wlans=({"id": "w1", "ssid": "corp"},)))
    a["scope"]["site_id"] = "siteA"
    doc = {
        "org_wlans": {"w1": {"id": "w1", "ssid": "corp"}},
        "sites": {"siteA": a},
        "fetch_failures": ["siteA"],
    }
    path = tmp_path / "ms_wlan.json"
    path.write_text(json.dumps(doc))
    provider = FixtureProvider(path)

    resolved = provider.resolve_org_wlan(OrgScope("o1"), "w1")
    fetched = provider.fetch_sites(OrgScope("o1"), ["siteA"])

    assert isinstance(resolved, OrgWlanContext)
    assert set(resolved.derived_rows_by_site) == {"siteA"}
    from digital_twin.providers.base import FetchError

    assert isinstance(fetched["siteA"], FetchError)


def test_multisite_org_wlan_template_resolves_from_site_derived_rows(tmp_path):
    from digital_twin.providers.base import OrgScope, OrgWlanTemplateContext

    store = ReplayStore(tmp_path)
    a = _site_doc(
        store,
        "a",
        raw_site(wlans=(
            {"id": "w2", "ssid": "iot", "template_id": "tmpl1"},
            {"id": "w1", "ssid": "guest", "template_id": "tmpl1"},
        )),
    )
    b = _site_doc(
        store,
        "b",
        raw_site(wlans=(
            {"id": "other", "ssid": "guest", "template_id": "other-template"},
            {"id": "site-owned", "ssid": "corp"},
            {"ssid": "missing-id", "template_id": "tmpl1"},
        )),
    )
    a["scope"]["site_id"] = "siteA"
    b["scope"]["site_id"] = "siteB"
    doc = {
        "templates": {
            "wlantemplate": {
                "tmpl1": {"id": "tmpl1", "name": "Guest template"},
            }
        },
        "sites": {"siteA": a, "siteB": b},
    }
    path = tmp_path / "ms_wlantemplate.json"
    path.write_text(json.dumps(doc))

    resolved = FixtureProvider(path).resolve_org_wlan_template(OrgScope("o1"), "tmpl1")

    assert isinstance(resolved, OrgWlanTemplateContext)
    assert resolved.template["name"] == "Guest template"
    assert set(resolved.derived_rows_by_site) == {"siteA"}
    assert [row["id"] for row in resolved.derived_rows_by_site["siteA"]] == ["w1", "w2"]


def test_multisite_org_wlan_template_missing_is_fetch_error(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site())
    doc = {"templates": {"wlantemplate": {}}, "sites": {"siteA": a}}
    path = tmp_path / "ms_wlantemplate.json"
    path.write_text(json.dumps(doc))

    result = FixtureProvider(path).resolve_org_wlan_template(OrgScope("o1"), "missing")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "wlantemplate"


def test_single_site_fixture_does_not_resolve_org_wlan_template(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope

    path = ReplayStore(tmp_path).save_raw("run1", raw_site())

    result = FixtureProvider(path).resolve_org_wlan_template(OrgScope("o1"), "tmpl1")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlantemplate"


def test_multisite_org_wlan_template_wrong_org_is_fetch_error(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site(wlans=({"id": "w1", "template_id": "tmpl1"},)))
    doc = {
        "templates": {"wlantemplate": {"tmpl1": {"id": "tmpl1"}}},
        "sites": {"siteA": a},
    }
    path = tmp_path / "ms_wlantemplate.json"
    path.write_text(json.dumps(doc))

    result = FixtureProvider(path).resolve_org_wlan_template(OrgScope("other-org"), "tmpl1")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "fixture"


def test_multisite_org_wlan_template_membership_includes_later_fetch_failure(tmp_path):
    from digital_twin.providers.base import FetchError, OrgScope, OrgWlanTemplateContext

    store = ReplayStore(tmp_path)
    a = _site_doc(store, "a", raw_site(wlans=({"id": "w1", "template_id": "tmpl1"},)))
    a["scope"]["site_id"] = "siteA"
    doc = {
        "templates": {"wlantemplate": {"tmpl1": {"id": "tmpl1"}}},
        "sites": {"siteA": a},
        "fetch_failures": ["siteA"],
    }
    path = tmp_path / "ms_wlantemplate.json"
    path.write_text(json.dumps(doc))
    provider = FixtureProvider(path)

    resolved = provider.resolve_org_wlan_template(OrgScope("o1"), "tmpl1")
    fetched = provider.fetch_sites(OrgScope("o1"), ["siteA"])

    assert isinstance(resolved, OrgWlanTemplateContext)
    assert set(resolved.derived_rows_by_site) == {"siteA"}
    assert isinstance(fetched["siteA"], FetchError)


def test_bgp_neighbors_round_trip_and_default_when_absent(tmp_path):
    from digital_twin.observability.replay.store import load_fixture_doc

    # _site_doc writes via ReplayStore (redacted) and parses back to a dict —
    # the exact shape load_fixture_doc expects.
    store = ReplayStore(tmp_path)
    doc = _site_doc(store, "bgp1", raw_site())

    # a doc WITHOUT bgp_neighbors still loads (pre-GS28 fixtures)
    doc.pop("bgp_neighbors", None)
    assert load_fixture_doc(doc).bgp_neighbors == ()

    # a doc WITH bgp_neighbors round-trips (state field is not redacted)
    doc["bgp_neighbors"] = [{"mac": "aa", "peer_ip": "10.0.0.2", "state": "Established"}]
    state = load_fixture_doc(doc)
    assert len(state.bgp_neighbors) == 1
    assert state.bgp_neighbors[0]["peer_ip"] == "10.0.0.2"


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
