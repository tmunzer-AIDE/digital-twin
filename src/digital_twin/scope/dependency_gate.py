"""Screen relevant existing fragments when modeled selectors/behavior change.

This is a bounded backstop for the production projection, not a full behavioral
compiler. It inspects both sides of changed rows and the port usage/network/AAA
dependencies they select. Unrelated opaque configuration and cosmetic edits do
not become global blockers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.adapters.mist.ingest.ports import (
    expand_port_map,
    overridable,
    resolve_port_bases,
)
from digital_twin.scope.allowlist import COSMETIC_RAW_ALLOWLIST
from digital_twin.scope.atomic_lists import atomic_list_issues
from digital_twin.scope.dhcp_screen import is_empty_scope_map
from digital_twin.scope.gateway_addressing import (
    same_static_gateway_subnet,
    valid_static_gateway_addition,
)
from digital_twin.scope.paths import LeafDelta, allowed_tokens, leaf_changes

_ROW_ROOTS = frozenset({
    "networks", "port_usages", "dhcpd_config", "other_ip_configs", "ip_configs",
    "bgp_config", "ospf_areas", "extra_routes", "extra_routes6",
})
_WHOLE_ROOTS = frozenset({
    "stp_config", "dhcp_snooping", "ospf_config", "radius_config", "mist_nac",
})
_PORT_ROOTS = ("port_config", "local_port_config", "port_config_overwrite")
_NETWORK_SCALARS = (
    "port_network", "voip_network", "guest_network", "server_fail_network", "server_reject_network",
)
_NETWORK_ARRAYS = ("networks", "dynamic_vlan_networks")
_COSMETIC = tuple(dict.fromkeys(p for ps in COSMETIC_RAW_ALLOWLIST.values() for p in ps))
_KNOWN_EMPTY = (
    ("ospf_areas", "*"), ("ospf_areas", "*", "networks"),
    ("ospf_areas", "*", "networks", "*"), ("bgp_config", "*", "neighbors"),
)


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _at(config: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _behavior_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return any(d.tokens[0] not in ("description", "note") for d in leaf_changes(before, after))


def _closure(seeds: set[str], graph: Mapping[str, set[str]]) -> set[str]:
    visited = set(seeds)
    pending = list(seeds)
    while pending:
        for target in graph.get(pending.pop(), ()):
            if target not in visited:
                visited.add(target)
                pending.append(target)
    return visited


def dependency_paths(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any], *, allowlist: tuple[str, ...],
    changes: tuple[LeafDelta, ...] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Exact opaque paths in the bounded union of relevant before/after fragments."""
    changes = tuple(
        d for d in (changes if changes is not None else leaf_changes(baseline, proposed))
        if not allowed_tokens(d.tokens, _COSMETIC)
    )
    if not changes:
        return ()
    fragments: set[tuple[str, ...]] = set()
    inline_fragments: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
    usages: set[str] = set()
    networks: set[str] = set()
    for delta in changes:
        root = delta.tokens[0]
        if root in _ROW_ROOTS and len(delta.tokens) >= 2:
            fragments.add(delta.tokens[:2])
            if root == "port_usages":
                usages.add(delta.tokens[1])
            elif root == "networks":
                networks.add(delta.tokens[1])
        elif root in _WHOLE_ROOTS:
            fragments.add((root,))
        if root == "ospf_config":
            fragments.add(("ospf_areas",))

    parents: dict[str, set[str]] = {}
    for config in (baseline, proposed):
        for name, row in _map(config.get("port_usages")).items():
            rules = _map(row).get("rules")
            if isinstance(rules, list):
                for rule in rules:
                    if isinstance(rule, Mapping) and isinstance(rule.get("usage"), str):
                        parents.setdefault(rule["usage"], set()).add(name)
    affected_usages = _closure(usages, parents)

    def depend_on_networks(row: Mapping[str, Any], config: Mapping[str, Any]) -> None:
        for key in _NETWORK_SCALARS:
            if isinstance(row.get(key), str):
                networks.add(row[key])
        for key in _NETWORK_ARRAYS:
            if isinstance(row.get(key), list):
                networks.update(v for v in row[key] if isinstance(v, str))
        if row.get("all_networks") is True:
            networks.update(_map(config.get("networks")))
        if row.get("port_auth") not in (None, "none"):
            fragments.update({("radius_config",), ("mist_nac",)})

    # Compare resolved selection bases and full inline rows. This also catches
    # range edits and local-overwrite flips without treating labels as behavior.
    base_ports = resolve_port_bases(dict(baseline))
    prop_ports = resolve_port_bases(dict(proposed))
    expanded = tuple({root: expand_port_map(config.get(root) or {}) for root in _PORT_ROOTS}
                     for config in (baseline, proposed))
    members = set(base_ports) | set(prop_ports)
    members.update(member for side in expanded for rows_by_member in side.values()
                   for member in rows_by_member)
    for member in sorted(members):
        rows = [(config, ports.get(member, {}))
                for config, ports in ((baseline, base_ports), (proposed, prop_ports))]
        selected = {str(row[k]) for _, row in rows
                    for k in ("usage", "dynamic_usage") if row.get(k)}
        referenced = bool(selected & affected_usages)
        port_changed = _behavior_changed(rows[0][1], rows[1][1])
        # Overwrite fields are applied after selection and are absent from bases.
        port_changed |= any(_behavior_changed(
            expanded[0][root].get(member, {}), expanded[1][root].get(member, {}),
        ) for root in _PORT_ROOTS)
        if not port_changed and not referenced:
            continue
        usages.update(selected)
        for index, (config, row) in enumerate(rows):
            depend_on_networks(row, config)
            pc = expanded[index]["port_config"].get(member)
            for root in _PORT_ROOTS:
                if root == "local_port_config" and not overridable(pc):
                    continue
                inline = expanded[index][root].get(member, {})
                if inline:
                    inline_fragments.append(((root, member), inline))
                depend_on_networks(inline, config)

    # Interface/service selectors join to existing network declarations too.
    # An OSPF enable can make an unchanged area/interface relevant wholesale.
    for path in tuple(fragments):
        if path[0] in ("dhcpd_config", "other_ip_configs", "ip_configs") and len(path) == 2:
            networks.add(path[1])
        for config in (baseline, proposed):
            if path[0] == "dhcp_snooping":
                depend_on_networks(_map(_at(config, path)), config)
            elif path[0] == "ospf_areas":
                areas = (_map(_at(config, path)),) if len(path) == 2 else (
                    _map(row) for row in _map(config.get("ospf_areas")).values()
                )
                for area in areas:
                    networks.update(_map(area.get("networks")))

    # Dynamic rules can select another profile. Visit the finite union of all
    # explicit rule targets, including paths the present observation did not take.
    visited: set[str] = set()
    while usages - visited:
        name = sorted(usages - visited)[0]
        visited.add(name)
        fragments.add(("port_usages", name))
        for config in (baseline, proposed):
            profile = _map(_at(config, ("port_usages", name)))
            depend_on_networks(profile, config)
            rules = profile.get("rules")
            if isinstance(rules, list):
                usages.update(rule["usage"] for rule in rules if isinstance(rule, Mapping)
                              and isinstance(rule.get("usage"), str))
    fragments.update(("networks", name) for name in networks)

    # Missing addressing fields also carry defaults. Sparse IP-only rows must
    # not evade the static-subnet proof merely by omitting type and netmask.
    issues: set[tuple[str, ...]] = {
        (*delta.tokens[:2], key)
        for delta in changes
        if len(delta.tokens) == 3 and delta.tokens[0] == "ip_configs"
        and delta.tokens[-1] == "ip"
        and not same_static_gateway_subnet(_at(baseline, delta.tokens[:2]),
                                          _at(proposed, delta.tokens[:2]))
        and not valid_static_gateway_addition(_at(baseline, delta.tokens[:2]),
                                              _at(proposed, delta.tokens[:2]))
        for key in ("type", "netmask")
    }
    values = [(path, _at(config, path)) for path in sorted(fragments)
              for config in (baseline, proposed)]
    for path, value in (*values, *inline_fragments):
        if value is None:
            continue
        for delta in leaf_changes({}, {"fragment": value}):
            tokens = (*path, *delta.tokens[1:])
            if is_empty_scope_map(tokens, baseline, proposed):
                continue
            if (len(tokens) == 3 and tokens[0] == "ip_configs"
                and tokens[-1] in ("type", "netmask")
                and same_static_gateway_subnet(_at(baseline, tokens[:2]),
                                               _at(proposed, tokens[:2]))):
                # The connected prefix remains invariant. Gateway IP changes
                # still receive an operational REVIEW finding in the pipeline.
                continue
            if (len(tokens) == 3 and tokens[0] == "port_config" and tokens[-1] == "critical"
                and type(expanded[0]["port_config"].get(tokens[1], {}).get("critical")) is bool
                and expanded[0]["port_config"].get(tokens[1], {}).get("critical")
                    == expanded[1]["port_config"].get(tokens[1], {}).get("critical")
                and type(expanded[1]["port_config"].get(tokens[1], {}).get("critical")) is bool):
                # An unchanged alarm toggle does not select or override the
                # modeled forwarding configuration. Edits remain denied.
                continue
            if delta.after == {} and any(
                len(pattern) == len(tokens) and all(p == "*" or p == t
                    for p, t in zip(pattern, tokens, strict=True)) for pattern in _KNOWN_EMPTY
            ):
                # Known absent memberships and default OSPF interface behavior;
                # an arbitrary empty unknown object still requires coverage.
                continue
            if not allowed_tokens(tokens, allowlist):
                issues.add(tokens)
            else:
                issues.update(atomic_list_issues(tokens, delta.after))
    return tuple(sorted(issues))
