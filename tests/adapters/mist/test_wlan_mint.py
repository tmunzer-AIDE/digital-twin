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


def test_site_owned_missing_scope_defaults_to_site():
    w = _mint_wlan({"id": "w4", "ssid": "guest", "enabled": True, "for_site": True})
    assert w.apply_to == "site"


def test_missing_scope_without_site_ownership_remains_unresolved():
    w = _mint_wlan({"id": "w5", "ssid": "guest", "enabled": True})
    assert w.apply_to is None


def test_ambiguous_ownership_is_inherited_fail_closed():
    assert _mint_wlan({"id": "w3", "ssid": "x"}).inherited is True
