from digital_twin.redaction import REDACTED, STRIP_KEY_PARTS, redact_leaf


def test_strips_secret_leaf_key():
    assert redact_leaf("psk", "topsecret") == REDACTED


def test_strips_secret_under_sensitive_ancestor():
    # generic leaf key under a sensitive PARENT must still be masked (P1)
    assert redact_leaf("private_key.value", "abc") == REDACTED
    assert redact_leaf("radius.secret.value", "abc") == REDACTED


def test_benign_scalar_passes_through():
    assert redact_leaf("order", 5) == 5


def test_ip_is_pseudonymized():
    out = redact_leaf("gateway", "10.1.2.3")
    assert out != "10.1.2.3" and out.startswith("198.51.")


def test_none_stays_none():
    assert redact_leaf("anything", None) is None


def test_schema_uses_shared_strip_key_parts():
    # parity: L0 secret-suppression and config-diff redaction share ONE source (P2)
    from digital_twin.adapters.mist.validate import schema
    assert schema.STRIP_KEY_PARTS is STRIP_KEY_PARTS


def test_back_compat_import_path_still_works():
    from digital_twin.observability.replay.redaction import REDACTION_VERSION, redact
    assert isinstance(REDACTION_VERSION, str)
    assert redact({"psk": "x"})["psk"] is None
