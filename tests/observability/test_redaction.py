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


def test_url_credential_param_name_variants_are_redacted():
    # compound names are the common real-world form — the fragment matches,
    # not an exact-name list
    url = (
        "https://x.example/cb?access_token=AAA&api_key=BBB&client_secret=CCC&auth_token=DDD&page=2"
    )
    out = redact({"u": url})["u"]
    for literal in ("AAA", "BBB", "CCC", "DDD"):
        assert literal not in out
    assert "page=2" in out  # benign params survive


def test_jwt_urls_and_bare_jwts_are_redacted():
    # signed JWT download URLs (?jwt=eyJ...) — and bare JWTs are
    # self-identifying (eyJ<header>.eyJ<payload>.<sig>), so catch them anywhere
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4"
    out = redact(
        {
            "image_url": f"https://x.example/img.png?jwt={jwt}",
            "note": f"download with {jwt}",
        }
    )
    assert "eyJ" not in out["image_url"]
    assert "eyJ" not in out["note"]
    assert "jwt=" in out["image_url"]  # param name survives


def test_prose_mentioning_password_survives():
    # tight manifest: only command lines / url params / credential keywords are
    # scrubbed — human prose must not be destroyed
    out = redact({"portal_text": "Enter the password to continue"})
    assert out["portal_text"] == "Enter the password to continue"


def test_high_entropy_values_are_caught_by_the_backstop():
    # the catch-all for secret SHAPES no key-name or known-pattern rule
    # anticipated: long random base64-ish blobs and long hex digests
    b64_secret = "Zk9xR3T7mQpL2vXc8JwYbN4aDsE6hUgK1iOfPrSt"  # 40 chars, mixed
    hex_secret = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928"  # 48 hex
    out = redact({"some_field": b64_secret, "note": f"value {hex_secret} inline"})
    assert b64_secret not in str(out)
    assert hex_secret not in str(out)
    assert out["some_field"].startswith("redacted-entropy-")
    # deterministic: same input -> same token
    assert redact({"some_field": b64_secret})["some_field"] == out["some_field"]


def test_entropy_backstop_spares_legitimate_content():
    benign = {
        "descr": "a fairly long descriptive note about the access network setup",
        "port_range": "ge-0/0/0-23,ge-0/0/47",
        "model": "EX4300-48MP",
        "image": "device_image1/photo.jpeg",
    }
    assert redact(benign) == benign


def test_entropy_backstop_is_idempotent_over_its_own_output():
    # the module's own tokens (redacted-*/uuid-*/name-* + <=32-char hashes)
    # must never re-trip the BACKSTOP — fixtures are redacted once at capture,
    # but a re-run must not mangle backstop output into a different token.
    # (uuid/ip pseudonyms re-hash by design — they stay shape-preserving.)
    doc = {
        "some_field": "Zk9xR3T7mQpL2vXc8JwYbN4aDsE6hUgK1iOfPrSt",
        "note": "see uuid-1f2e3d4c5b6a and name-0a1b2c3d and a9b2ad6f4919",
    }
    once = redact(doc)
    assert redact(once)["some_field"] == once["some_field"]
    assert redact(once)["note"] == once["note"]


def test_version_present():
    assert isinstance(REDACTION_VERSION, str) and REDACTION_VERSION
