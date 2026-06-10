"""Evaluate networktemplate `switch_matching` to a per-device base port_config.

Mist rules are a PRIORITY LIST: evaluated top-to-bottom, the FIRST rule whose
`match_*` criteria all hold provides that switch's base port_config (only when
`enable` is true). A rule with no `match_*` keys is a catch-all (the reserved
`default` rule sits last). The device's own port_config overlays this base
per-port downstream (see compile_device) — so only the rule's port_config is
consumed here; its ip_config/port_mirroring/stp_config are out of M1 L2 scope.

Match criteria (data- and schema-confirmed): `match_model` (exact),
`match_model[A:B]` (model slice), `match_role`, `match_name`, `match_name[A:B]`.
An UNKNOWN `match_*` criterion makes the rule not match (under-assign over
mis-assign).
"""

from __future__ import annotations

import copy
import re
from typing import Any

_SLICE = re.compile(r"^match_(model|name)\[(\d+):(\d+)\]$")
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


def resolve_switch_matching(switch_matching: JsonObj | None, device: JsonObj) -> JsonObj:
    sm = switch_matching or {}
    if not sm.get("enable"):
        return {}
    for rule in sm.get("rules") or []:
        if _rule_matches(rule, device):
            return copy.deepcopy(dict(rule.get("port_config") or {}))
    return {}
