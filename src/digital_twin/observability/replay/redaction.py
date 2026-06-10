"""Redaction manifest + engine — capturing an UN-redacted fixture is a defect.

- Deterministic pseudonymization (sha256-derived, same input -> same token) for
  relationship-bearing identifiers: MACs (kept MAC-shaped), IPv4/IPv6 (re-mapped
  into documentation ranges, equality preserved), UUIDs, host/device names.
- Secrets are STRIPPED to None, never hashed (manifest below).
- Structure (vlan ids, port names, dict shapes) preserved so the compiler and
  checks run identically on the fixture.
Known limitation (documented): hashing preserves equality, not prefixes —
switch_matching match_name[A:B] rules can match differently on redacted data;
the GS suite validates the fixture end-to-end.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

REDACTION_VERSION = "5"  # v5: jwt URL params + bare JWTs (eyJ*.eyJ*.sig) anywhere

# strip outright (substring match on the key, case-insensitive) — never hash
STRIP_KEY_PARTS: tuple[str, ...] = (
    "psk",
    "password",
    "passphrase",
    "secret",
    "token",
    "community",
    "private_key",
    "cert",
)
# keys whose STRING values are name-like -> "name-<h8>"
NAME_KEYS: tuple[str, ...] = ("name", "hostname", "system_name", "neighbor_system_name")

_MAC = re.compile(r"^(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$|^[0-9a-fA-F]{12}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")
_IPV6 = re.compile(r"^[0-9a-fA-F:]+:[0-9a-fA-F:]+$")

# credential material EMBEDDED in ordinary strings (the class the first fixture
# leaked): Junos config command lines carrying secrets, and URL query credentials
_CRED_CMD = re.compile(
    r"^#?\s*(set|delete)\s.*("
    r"encrypted-password|plain-text-password|pre-shared-key|authentication-key"
    r"|ssh-rsa|ssh-dss|ssh-ed25519|\bsecret\b|\bcommunity\b"
    r")",
    re.IGNORECASE,
)
# FRAGMENT match on the param name (access_token, api_key, client_secret,
# auth_token, ...) — over-redacting a benign param (e.g. keyword=) is safe,
# under-redacting a credential is not
_URL_CRED = re.compile(
    r"([?&][a-zA-Z0-9_\-]*(?:token|key|secret|password|auth|credential|signature|jwt)"
    r"[a-zA-Z0-9_\-]*=)[^&\"'\s]+",
    re.IGNORECASE,  # X-Amz-Credential, X-Amz-Security-Token, jwt=, ...
)
# bare JWTs are self-identifying — eyJ<header>.eyJ<payload>.<signature> — and can
# appear OUTSIDE query params (paths, prose); catch them anywhere
_JWT_ANY = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

# embedded (substring) forms — composite address lists, free-text notes, URLs
_MAC_ANY = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")
_UUID_ANY = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_IPV6_ANY = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]*[0-9a-fA-F]\b")
_IPV4_ANY = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")


def _h(value: str, n: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:n]


def _redact_scalar(key: str, value: str) -> str:
    if _MAC.match(value):
        return _h(value.lower().replace(":", "").replace("-", ""), 12)
    if _UUID.match(value):
        h = _h(value.lower(), 32)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    if _IPV4.match(value):
        suffix = value.partition("/")[2]
        n = int(_h(value, 8), 16)
        ip = f"198.51.{(n >> 8) % 256}.{n % 256}"  # TEST-NET-2 documentation range
        return f"{ip}/{suffix}" if suffix else ip
    if _IPV6.match(value) and ":" in value:
        return f"2001:db8::{_h(value, 8)}"  # documentation prefix
    if key in NAME_KEYS:
        return f"name-{_h(value, 8)}"
    return _sub_embedded(value)


def _ipv4_token(match: re.Match[str]) -> str:
    value = match.group()
    suffix = value.partition("/")[2]
    n = int(_h(value.partition("/")[0], 8), 16)
    ip = f"198.51.{(n >> 8) % 256}.{n % 256}"
    return f"{ip}/{suffix}" if suffix else ip


def _sub_embedded(value: str) -> str:
    """Composite strings (comma-joined address lists, free text, config command
    lines, URLs) can EMBED identifiers and credentials the exact-match rules
    cannot see — replace them in place. A config command line carrying any
    credential keyword is replaced WHOLLY (we never compile CLI cmds, so no
    structure is lost); URL query credential values are tokenized in place."""
    if _CRED_CMD.match(value):
        return f"redacted-cmd-{_h(value, 8)}"
    value = _URL_CRED.sub(lambda m: f"{m.group(1)}redacted-{_h(m.group(), 8)}", value)
    value = _JWT_ANY.sub(lambda m: f"redacted-jwt-{_h(m.group(), 8)}", value)
    value = _MAC_ANY.sub(lambda m: _h(m.group().lower().replace(":", ""), 12), value)
    value = _UUID_ANY.sub(lambda m: f"uuid-{_h(m.group().lower(), 12)}", value)
    value = _IPV6_ANY.sub(lambda m: f"2001:db8::{_h(m.group(), 8)}", value)
    value = _IPV4_ANY.sub(_ipv4_token, value)
    return value


def redact(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(part in lk for part in STRIP_KEY_PARTS):
                out[k] = None
            else:
                out[k] = redact(v, key=str(k))
        return out
    if isinstance(obj, (list, tuple)):  # RawSiteState payloads are TUPLES of dicts
        return [redact(v, key=key) for v in obj]
    if isinstance(obj, str):
        return _redact_scalar(key, obj)
    return obj
