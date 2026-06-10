"""WlanIngester: site WLAN config -> per-AP required VLANs in the IR.

Records resolvable requirements (so a delta that severs them is caught) and
unresolvable ones (so the check can note a coverage gap). Earns WLAN_CONFIG only
when the WLAN fetch actually succeeded.
"""

from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.adapters.mist.ingest.wlan import WlanIngester
from digital_twin.ir import IRBuilder, IRCapability, device_id
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site

AP_ID = device_id("cc0000000001")  # AP_1 in the default fixture devices


def _ingest(wlans=(), fetched=None):
    kwargs = {"wlans": tuple(wlans)}
    if fetched is not None:
        kwargs["fetched"] = fetched
    ctx = IngestContext(
        raw=raw_site(**kwargs),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    earned = WlanIngester().ingest(ctx)
    return ctx.builder.build(), earned


def _wlan(**kw):
    base = {"ssid": "w", "enabled": True, "vlan_enabled": True, "interface": "all"}
    return {**base, **kw}


def test_enabled_wlan_records_ap_required_vlan_and_earns_capability():
    ir, earned = _ingest(wlans=[_wlan(ssid="corp", apply_to="site", vlan_id=99)])
    assert ir.ap_wlan_vlans.get(AP_ID) == frozenset({99})
    assert 99 in ir.vlans  # a Vlan entity is ensured so the per-vlan graph sees it
    assert IRCapability.WLAN_CONFIG in earned


def test_unresolved_wlan_recorded_for_coverage_note():
    ir, _ = _ingest(wlans=[_wlan(ssid="guest", apply_to="wxtags", wxtag_ids=["t1"], vlan_id=50)])
    assert AP_ID in ir.ap_wlan_unresolved
    assert any("guest" in r for r in ir.ap_wlan_unresolved[AP_ID])


def test_no_wlan_fetch_earns_nothing_and_records_nothing():
    ir, earned = _ingest(wlans=[], fetched=("site", "setting", "devices"))  # 'wlans' absent
    assert earned == frozenset()
    assert dict(ir.ap_wlan_vlans) == {}


def test_produces_capability():
    assert IRCapability.WLAN_CONFIG in WlanIngester().produces()
