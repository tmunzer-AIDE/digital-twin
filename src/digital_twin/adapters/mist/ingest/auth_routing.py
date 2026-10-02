"""Literal authentication/routing intent; no runtime availability assumptions."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from typing import Any

from digital_twin.ir import StaticRoute


def _token(value: Any) -> str:
    # Match the config-diff contract: a null object member is absent, including
    # inside server arrays. Full-object payloads commonly omit GET-only nulls.
    def normalize(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {k: normalize(v) for k, v in node.items() if v is not None}
        if isinstance(node, list):
            return [normalize(v) for v in node]
        return node

    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True).encode()).hexdigest()


def authenticator_state(effective: Mapping[str, Any]) -> tuple[int, bool, str]:
    radius = effective.get("radius_config")
    nac = effective.get("mist_nac")
    unresolved = radius is not None and not isinstance(radius, Mapping)
    unresolved |= nac is not None and not isinstance(nac, Mapping)
    servers = radius.get("auth_servers", []) if isinstance(radius, Mapping) else []
    enabled = nac.get("enabled", False) if isinstance(nac, Mapping) else False
    if servers is None:
        servers = []
    if enabled is None:
        enabled = False
    count = int(enabled is True)
    unresolved |= enabled is not False and enabled is not True and enabled is not None
    if isinstance(servers, list):
        for server in servers:
            host = server.get("host") if isinstance(server, Mapping) else None
            if isinstance(host, str) and host.strip() and "{{" not in host:
                count += 1
            else:
                unresolved = True
    else:
        unresolved = True
    # Include all authentication-server attributes: rotating a secret or port
    # must not disappear just because the number of backends stayed the same.
    digest = _token({"servers": servers, "enabled": enabled, "unresolved": unresolved})
    return count, unresolved, digest


def static_routes(effective: Mapping[str, Any], did: str) -> tuple[StaticRoute, ...]:
    routes: list[StaticRoute] = []
    for root, version in (("extra_routes", 4), ("extra_routes6", 6)):
        table = effective.get(root)
        if table is None:
            continue
        if not isinstance(table, Mapping):
            routes.append(
                StaticRoute(
                    did,
                    f"unresolved:{root}",
                    unresolved=True,
                    unresolved_token=_token(table),
                )
            )
            continue
        for destination, raw in table.items():
            unresolved = False
            try:
                network = ipaddress.ip_network(str(destination), strict=False)
                prefix = str(network)
                unresolved |= network.version != version
            except ValueError:
                prefix = str(destination)
                unresolved = True
            row = raw if isinstance(raw, Mapping) else {}
            unresolved |= not isinstance(raw, Mapping)
            discard = row.get("discard") is True
            flag = row.get("discard")
            unresolved |= flag is not None and not isinstance(flag, bool)
            via = row.get("via")
            values = via if isinstance(via, list) else ([] if via is None else [via])
            qualified = row.get("next_qualified")
            if isinstance(qualified, Mapping):
                values = [*values, *qualified]
            elif qualified is not None:
                unresolved = True
            hops: set[str] = set()
            for value in values:
                try:
                    ip = ipaddress.ip_address(str(value))
                    if ip.version != version:
                        unresolved = True
                    else:
                        hops.add(str(ip))
                except ValueError:
                    unresolved = True
            if not discard and not hops:
                unresolved = True
            routes.append(
                StaticRoute(
                    did,
                    prefix,
                    tuple(sorted(hops)),
                    discard,
                    unresolved,
                    _token(raw) if unresolved else None,
                )
            )
    return tuple(routes)
