"""Row-level DHCP-relevance screen for the derived gate. Pure row-local rule via
the same predicates the ingest uses (_dhcp_active / _dhcp_serves_scope) — never a
check's output (the derived gate runs before checks). Complete rejection set
(UNKNOWN if ANY): (1) inert servers on a row serving on BOTH sides (S->S);
(2) participation/target — both sides active and the relay-target identity differs
(exactly one active relay -> dhcp_mode_transition; both active relays, differing
servers -> dhcp_relay_target); (3) inert range/gateway while both sides non-serving
-> dhcp_scope_field. See the 3x3 matrix in the design spec."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.adapters.mist.ingest.switch import _dhcp_active, _dhcp_serves_scope
from digital_twin.contracts import Rejection

JsonObj = dict[str, Any]
_SCOPE_FIELDS = ("ip_start", "ip_end", "gateway")


# Scope-row maps whose EMPTY form is a no-op: no reservation, no option. Mist
# writes `fixed_bindings: {}` on every scope and agents copy `options: {}`.
_EMPTY_NOOP_ROW_MAPS = ("fixed_bindings", "options")


def is_empty_scope_map(
    path: str | tuple[str, ...], before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """`dhcpd_config.<scope>.fixed_bindings` / `.options` holding nothing on either
    side (absent, null or `{}`) reserves nothing and sends no option, so it is not
    an unmodeled change. Only exactly-empty maps qualify (adding or dropping
    entries surfaces as entry leaves, which the allowlist judges), and only on a
    row that carries another setting on each side where it exists: a row holding
    nothing else could switch a scope on with defaults, and only this path would
    show it."""
    segments = tuple(path.split(".")) if isinstance(path, str) else path
    if len(segments) != 3 or segments[0] != "dhcpd_config":
        return False
    key = segments[2]
    if key not in _EMPTY_NOOP_ROW_MAPS:
        return False
    rows = [row for row in (_scope_row(before, segments[1]), _scope_row(after, segments[1]))
            if row is not None]
    return bool(rows) and all(
        row.get(key) in (None, {})
        and any(value is not None for name, value in row.items() if name != key)
        for row in rows
    )


def _scope_row(config: Mapping[str, Any], name: str) -> JsonObj | None:
    rows = config.get("dhcpd_config")
    row = rows.get(name) if isinstance(rows, Mapping) else None
    return row if isinstance(row, dict) else None


def _is_active_relay(row: JsonObj) -> bool:
    return _dhcp_active(row) and str((row or {}).get("type") or "local") == "relay"


def dhcp_row_rejection(base: JsonObj, prop: JsonObj) -> Rejection | None:
    # a non-dict value (the `dhcpd_config.enabled` boolean flag Mist stores
    # alongside the per-network scope dicts) is not a scope row -> nothing to screen
    base = base if isinstance(base, dict) else {}
    prop = prop if isinstance(prop, dict) else {}
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
