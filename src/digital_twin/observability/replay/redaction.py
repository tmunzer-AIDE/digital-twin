"""Back-compat re-export. The redaction engine moved to digital_twin.redaction —
now shared by replay fixtures AND result config-diff rendering. Import from there;
this shim keeps existing replay imports working."""

from digital_twin.redaction import (
    NAME_KEY_PARTS,
    NAME_KEYS,
    REDACTED,
    REDACTION_VERSION,
    STRIP_KEY_PARTS,
    redact,
    redact_leaf,
)

__all__ = [
    "NAME_KEY_PARTS",
    "NAME_KEYS",
    "REDACTED",
    "REDACTION_VERSION",
    "STRIP_KEY_PARTS",
    "redact",
    "redact_leaf",
]
