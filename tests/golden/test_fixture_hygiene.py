"""An un-redacted field in a committed fixture is a DEFECT (spec) — fail CI."""

import json
import re
from pathlib import Path

FIXTURES = sorted(Path(__file__).parent.glob("fixtures/*.json"))
_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_PRIVATE_IP = re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
_SECRET_KEYS = ("psk", "password", "secret", "token", "community", "passphrase")
# credential material EMBEDDED in ordinary strings (cmd lines, URLs, key blobs);
# the URL rule FRAGMENT-matches param names (access_token, api_key, ...) and
# mirrors redaction._URL_CRED
_EMBEDDED_CRED = re.compile(
    r"encrypted-password|plain-text-password|pre-shared-key|ssh-rsa|ssh-ed25519"
    r"|BEGIN (?:RSA |EC )?PRIVATE KEY"
    r"|[?&][a-zA-Z0-9_\-]*(?:token|secret|password|apikey|api_key|credential|signature)"
    r"[a-zA-Z0-9_\-]*=(?!redacted-)",
    re.IGNORECASE,
)


def test_fixtures_exist():
    assert FIXTURES, "no golden fixture captured — run tools/capture_replay.py"


def test_no_unredacted_identifiers_or_secrets():
    for path in FIXTURES:
        blob = path.read_text()
        assert not _MAC.search(blob), f"{path}: colon-MAC survived redaction"
        assert not _PRIVATE_IP.search(blob), f"{path}: private IP survived redaction"
        leak = _EMBEDDED_CRED.search(blob)
        assert not leak, f"{path}: embedded credential survived redaction: {leak.group()[:40]}"
        data = json.loads(blob)

        def walk(node, path=path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if any(s in str(k).lower() for s in _SECRET_KEYS):
                        assert v is None, f"{path}: secret key {k!r} not stripped"
                    walk(v, path)
            elif isinstance(node, list):
                for v in node:
                    walk(v, path)

        walk(data)


def test_pseudonymization_is_stable_within_fixture():
    # the same original mac must map to ONE token: device 'mac' fields that the
    # port_stats reference must still join (topology preserved)
    for path in FIXTURES:
        data = json.loads(path.read_text())
        device_macs = {d.get("mac") for d in data["devices"] if d.get("mac")}
        stat_macs = {p.get("mac") for p in data["port_stats"] if p.get("mac")}
        assert stat_macs & device_macs, f"{path}: stats no longer join to devices"
