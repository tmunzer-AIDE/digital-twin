from digital_twin.adapters.base import VendorAdapter
from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
from digital_twin.contracts import ChangeOp
from digital_twin.providers.base import RawSiteState
from tests.adapters.mist.fixtures import raw_site


def test_mist_adapter_satisfies_protocol():
    adapter: VendorAdapter = MistAdapter()
    assert adapter is not None


def test_ingest_compiles_and_builds_ir():
    out = MistAdapter().ingest(raw_site())
    assert isinstance(out, IngestOutcome)
    assert out.report.ok
    assert out.ir is not None and len(out.ir.devices) >= 2  # SWITCH_A + AP_1
    assert "networks" in out.site_effective
    # device_effective keyed by canonical device id (mac-derived)
    assert any(k.startswith("aa0000000001") for k in out.device_effective)


def test_validate_delegates_to_l0():
    res = MistAdapter().validate(
        ChangeOp(
            action="update",
            order=0,
            object_type="site_setting",
            object_id="s1",
            payload={"networks": "bad"},
        )
    )
    assert res.findings  # L0 caught the type violation


def test_apply_delegates_to_apply_plan():
    raw = raw_site()
    out = MistAdapter().apply(
        raw,
        (
            ChangeOp(
                action="update",
                order=0,
                object_type="device",
                object_id="dev-a",
                payload={"name": "via-facade"},
            ),
        ),
    )
    assert isinstance(out, RawSiteState)
