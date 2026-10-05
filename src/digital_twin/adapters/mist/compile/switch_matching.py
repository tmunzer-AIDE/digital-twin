"""Evaluate networktemplate `switch_matching` to a per-device base port_config.

Mist rules are a PRIORITY LIST: evaluated top-to-bottom, the FIRST rule whose
`match_*` criteria all hold provides that switch's base port_config (only when
`enable` is true). A rule with no `match_*` keys is a catch-all (the reserved
`default` rule sits last). The device's own port_config overlays this base
per-port downstream (see compile_device) — so only the rule's port_config is
consumed here; its ip_config/port_mirroring/stp_config are out of M1 L2 scope.

Match criteria (data- and schema-confirmed): `match_model` (exact),
`match_model[A:B]` / `match_model[A-B]` (model slice — the schema documents BOTH
separators, e.g. `match_name[0:3]` and `match_model[0-8]`), `match_role`,
`match_name`, `match_name[A:B]`/`[A-B]`. An UNKNOWN `match_*` criterion makes the
rule not match (under-assign over mis-assign).
"""

from __future__ import annotations

import copy
import re
from typing import Any

_SLICE = re.compile(r"^match_(model|name)\[(\d+)[:-](\d+)\]$")
_EXACT = {"match_model": "model", "match_name": "name", "match_role": "role"}

JsonObj = dict[str, Any]


def _rule_matches(rule: JsonObj, device: JsonObj) -> bool:
    for key, want in rule.items():
        if not key.startswith("match_"):
            continue  # `name`, `port_config`, etc. are not match criteria
        field = _EXACT.get(key)
        if field is not None:
            if str(device.get(field) or "") != want:
                return False
            continue
        sl = _SLICE.match(key)
        if sl is None:
            return False  # unknown match_* -> conservative non-match
        value = str(device.get(sl.group(1)) or "")
        if value[int(sl.group(2)) : int(sl.group(3))] != want:
            return False
    return True


def rename_sensitive_criterion(rule: JsonObj, old_name: str, new_name: str) -> str | None:
    """The `match_*` key whose outcome renaming a device `old_name -> new_name`
    could flip, or None when every criterion of the rule provably ignores it.

    Serves the device-rename SAFE proof, so it never under-reports. It holds for
    ANY rule-selection order (each rule's own outcome is compared) and for the
    readings the OAS leaves open: case-sensitive or not, and `match_name[A:B]`
    compared to the value or to the value's own `[A:B]` slice. Shared with
    `gateway_matching`/`ap_matching`, which document the same key grammar.
    """
    for key, want in rule.items():
        if not key.startswith("match_") or key in ("match_model", "match_role"):
            continue
        sl = _SLICE.match(key)
        if sl is not None and sl.group(1) == "model":
            continue
        if key == "match_name":
            old_part, new_part, wanted = old_name, new_name, [want]
        elif sl is not None:
            start, end = int(sl.group(2)), int(sl.group(3))
            old_part, new_part = old_name[start:end], new_name[start:end]
            wanted = [want, want[start:end]] if isinstance(want, str) else [want]
        else:
            return key  # unknown criterion: never assumed name-independent
        if old_part == new_part:
            continue  # the compared part of the name did not change
        if not isinstance(want, str) or "{{" in want:
            return key  # a {{var}} value is only known after var resolution
        for value in wanted:
            for fold in (str, str.casefold):
                if (fold(old_part) == fold(value)) != (fold(new_part) == fold(value)):
                    return key
    return None


def supported_match_key(key: str) -> bool:
    return key in _EXACT or _SLICE.fullmatch(key) is not None


def select_switch_matching_rule(
    switch_matching: JsonObj | None, device: JsonObj
) -> JsonObj | None:
    """Return the entire selected rule, including uncompiled settings for gating."""
    sm = switch_matching or {}
    if not sm.get("enable"):
        return None
    for rule in sm.get("rules") or []:
        if _rule_matches(rule, device):
            return rule  # type: ignore[no-any-return]
    return None


def resolve_switch_matching(switch_matching: JsonObj | None, device: JsonObj) -> JsonObj:
    rule = select_switch_matching_rule(switch_matching, device)
    return copy.deepcopy(dict((rule or {}).get("port_config") or {}))
