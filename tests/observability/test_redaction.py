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


def test_embedded_identifiers_in_composite_strings_are_redacted():
    # real Mist payloads carry comma-joined address lists and free text — the
    # leak class the fixture-hygiene CI caught on first capture
    out = redact(
        {
            "ips": {"vlan1": "10.100.1.75/23,fe80:0:0:0:23e:73ff:fe01:2345"},
            "note": "uplink to aa:bb:cc:dd:ee:ff at 192.168.1.1",
        }
    )
    blob = str(out)
    assert "10.100.1.75" not in blob
    assert "fe80" not in blob
    assert "aa:bb:cc:dd:ee:ff" not in blob
    assert "192.168.1.1" not in blob


def test_credential_command_lines_are_redacted():
    # Junos CLI lines (additional_config_cmds) can EMBED credentials in plain
    # strings — the class the first committed fixture leaked
    out = redact(
        {
            "additional_config_cmds": [
                'set system login user helpdesk authentication encrypted-password "$6$abc"',
                "#set system login user netadmin authentication encrypted-password $6$def",
                "set interfaces ge-0/0/0 description uplink",  # harmless line survives
            ]
        }
    )
    cmds = out["additional_config_cmds"]
    assert "encrypted-password" not in cmds[0] and "$6$abc" not in cmds[0]
    assert "$6$def" not in cmds[1]
    assert cmds[2] == "set interfaces ge-0/0/0 description uplink"


def test_url_query_credentials_are_redacted():
    out = redact({"blacklist_url": "https://x.example/occupancy?token=OTc4yZS&x=1"})
    assert "OTc4yZS" not in out["blacklist_url"]
    assert "token=" in out["blacklist_url"]  # param name survives, value tokenized


def test_prose_mentioning_password_survives():
    # tight manifest: only command lines / url params / credential keywords are
    # scrubbed — human prose must not be destroyed
    out = redact({"portal_text": "Enter the password to continue"})
    assert out["portal_text"] == "Enter the password to continue"


def test_version_present():
    assert isinstance(REDACTION_VERSION, str) and REDACTION_VERSION
