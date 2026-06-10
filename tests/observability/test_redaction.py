from digital_twin.observability.replay.redaction import REDACTION_VERSION, redact


def test_macs_pseudonymized_stably_and_shaped():
    a = redact({"mac": "aa:bb:cc:dd:ee:01", "peer": "aa:bb:cc:dd:ee:01"})
    assert a["mac"] == a["peer"]  # same input -> same token (topology preserved)
    assert a["mac"] != "aa:bb:cc:dd:ee:01"
    assert len(a["mac"].replace(":", "")) == 12  # still MAC-shaped


def test_bare_mac_format_also_caught():
    out = redact({"mac": "aabbccddee01"})
    assert out["mac"] != "aabbccddee01" and len(out["mac"]) == 12


def test_ips_and_uuids_and_names_tokenized():
    out = redact(
        {
            "ip": "10.1.2.3",
            "site_id": "9777c1a0-6ef6-11e6-8bbf-02e208b2d34f",
            "name": "ld-cup-idf-a",
        }
    )
    assert out["ip"] != "10.1.2.3" and out["ip"].count(".") == 3  # doc-range IPv4 shape
    assert out["site_id"] != "9777c1a0-6ef6-11e6-8bbf-02e208b2d34f"
    assert out["name"].startswith("name-")


def test_secrets_stripped_not_hashed():
    out = redact({"psk": "supersecret", "radius_config": {"secret": "x", "port": 1812}})
    assert out["psk"] is None
    assert out["radius_config"]["secret"] is None
    assert out["radius_config"]["port"] == 1812


def test_structure_preserved():
    out = redact({"vlan_id": 30, "port_config": {"ge-0/0/1": {"usage": "ap"}}})
    assert out["vlan_id"] == 30
    assert out["port_config"]["ge-0/0/1"]["usage"] == "ap"


def test_version_present():
    assert isinstance(REDACTION_VERSION, str) and REDACTION_VERSION
