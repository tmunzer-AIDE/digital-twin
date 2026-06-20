# tests/adapters/mist/test_client_enrichment_join.py
from digital_twin.adapters.mist.ingest.client_enrichment import build_client_enrichment


def test_wired_only_oui_manufacturer():
    out = build_client_enrichment(
        wired=({"mac": "AA:BB:CC:00:00:01", "last_hostname": "r2d2",
                "manufacture": "Raspberry Pi Trading Ltd"},),
        wireless=(), nac=(),
    )
    ce = out["aabbcc000001"]                      # client_id-normalized key
    assert ce.hostname == "r2d2"
    assert ce.mfg == "Raspberry Pi Trading Ltd"
    assert ce.family is None


def test_nac_overlay_wins_but_unknown_does_not_clobber_base_mfg():
    out = build_client_enrichment(
        wired=({"mac": "aabbcc000002", "manufacture": "HP"},),
        wireless=(),
        nac=({"mac": "aabbcc000002", "last_family": "Printer", "last_mfg": "Unknown",
              "auth_type": "mab", "last_nacrule_name": "printer_mab", "last_status": "permitted"},),
    )
    ce = out["aabbcc000002"]
    assert ce.mfg == "HP"          # NAC "Unknown" cleaned to None -> base survives
    assert ce.family == "Printer"  # NAC adds the useful field
    assert ce.auth_type == "mab" and ce.nacrule == "printer_mab" and ce.status == "permitted"


def test_unknown_blank_whitespace_collapse_to_none():
    out = build_client_enrichment(
        wired=(), wireless=(),
        nac=({"mac": "aabbcc000003", "last_family": " Unknown ", "last_model": "",
              "last_os": "unknown", "last_hostname": "LiveDemo-CD51"},),
    )
    ce = out["aabbcc000003"]
    assert ce.family is None and ce.model is None and ce.os is None
    assert ce.hostname == "LiveDemo-CD51"


def test_cross_separator_mac_join_and_empty_record_omitted():
    out = build_client_enrichment(
        wired=({"mac": "AA-BB-CC-00-00-04", "manufacture": "Intel Corporate"},),
        wireless=(),
        nac=({"mac": "aabbcc000004", "auth_type": "eap-tls"},),  # same device, other separators
    )
    assert out["aabbcc000004"].mfg == "Intel Corporate"
    assert out["aabbcc000004"].auth_type == "eap-tls"
    out2 = build_client_enrichment(wired=(), wireless=(),
                                   nac=({"mac": "deadbeef", "last_os": "Unknown"},))
    assert "deadbeef" not in out2


def test_malformed_row_is_skipped_not_fatal():
    out = build_client_enrichment(
        wired=({"no_mac": True}, {"mac": "aabbcc000005", "last_hostname": "ok"}),
        wireless=(), nac=(),
    )
    assert out["aabbcc000005"].hostname == "ok"
    assert len(out) == 1
