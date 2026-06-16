"""Row-level DHCP-relevance screen for the derived gate. Pure row-local rule via
the same predicates the ingest uses (_dhcp_active / _dhcp_serves_scope) — never a
check's output (the derived gate runs before checks). Complete rejection set
(UNKNOWN if ANY): (1) inert servers on a row serving on BOTH sides (S->S);
(2) participation/target — both sides active and the relay-target identity differs
(exactly one active relay -> dhcp_mode_transition; both active relays, differing
servers -> dhcp_relay_target); (3) inert range/gateway while both sides non-serving
-> dhcp_scope_field. See the 3x3 matrix in the design spec."""

from __future__ import annotations

from typing import Any

from digital_twin.adapters.mist.ingest.switch import _dhcp_active, _dhcp_serves_scope
from digital_twin.contracts import Rejection

JsonObj = dict[str, Any]
_SCOPE_FIELDS = ("ip_start", "ip_end", "gateway")


def _is_active_relay(row: JsonObj) -> bool:
    return _dhcp_active(row) and str((row or {}).get("type") or "local") == "relay"


def dhcp_row_rejection(base: JsonObj, prop: JsonObj) -> Rejection | None:
    base, prop = base or {}, prop or {}
    serves_b, serves_p = _dhcp_serves_scope(base), _dhcp_serves_scope(prop)
    active_b, active_p = _dhcp_active(base), _dhcp_active(prop)

    # (1) inert servers — serving on BOTH sides, servers changed
    if serves_b and serves_p and base.get("servers") != prop.get("servers"):
        return Rejection(stage="dhcp_inert_servers",
                         reasons=("servers changed on a serving row (inert)",))

    # (2) participation/relay-target — both active, target identity differs
    if active_b and active_p:
        ar_b, ar_p = _is_active_relay(base), _is_active_relay(prop)
        if ar_b != ar_p:
            return Rejection(stage="dhcp_mode_transition",
                             reasons=("serving<->active-relay; relay target unmodeled",))
        if ar_b and ar_p and base.get("servers") != prop.get("servers"):
            return Rejection(stage="dhcp_relay_target",
                             reasons=("active relay target changed (unmodeled)",))

    # (3) inert scope-fact — both sides non-serving, range/gateway changed
    if not serves_b and not serves_p and any(
        base.get(f) != prop.get(f) for f in _SCOPE_FIELDS
    ):
        return Rejection(stage="dhcp_scope_field",
                         reasons=("range/gateway changed on a non-serving row (inert)",))
    return None
