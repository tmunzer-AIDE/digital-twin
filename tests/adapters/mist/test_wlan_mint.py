from digital_twin.adapters.mist.ingest.wlan import _mint_wlan


def test_mints_disabled_open_unisolated_inherited():
    w = _mint_wlan({"id": "w1", "ssid": "guest", "enabled": False,
                    "auth": {"type": "open"}, "l2_isolation": True,
                    "apply_to": "site", "for_site": False, "template_id": "t1"})
    assert w.ssid == "guest" and w.enabled is False
    assert w.auth_type == "open" and w.isolation is True   # via l2_isolation
    assert w.apply_to == "site" and w.inherited is True     # template-owned


def test_site_owned_and_scope_normalization():
    w = _mint_wlan({"id": "w2", "ssid": "corp", "enabled": True, "for_site": True,
                    "apply_to": "aps", "ap_ids": ["b", "a", "a"]})
    assert w.inherited is False                            # positively site-owned
    assert w.ap_ids == ("a", "b")                          # sorted+deduped
    assert w.isolation is False and w.auth_type is None     # absent -> defaults


def test_ambiguous_ownership_is_inherited_fail_closed():
    assert _mint_wlan({"id": "w3", "ssid": "x"}).inherited is True
