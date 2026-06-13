"""Family-aware IP equality with honest unknowns (GS22-GW shared helper).

IR-layer-neutral: the ingester consumes it (the non-winning-row gateway
conflict rule) and so do the gateway_gap/scope_lint checks — the layering
is adapters -> {ir, contracts} with checks downstream, so this cannot live
under checks/.
"""

from __future__ import annotations

import ipaddress


def same_ip(a: str | None, b: str | None) -> bool | None:
    """True/False = a definitive verdict over two parseable addresses
    (mismatched families are simply NOT equal); None = comparison UNKNOWN
    (either side absent or unparseable) — never a guessed (in)equality.
    Tolerates a /prefix suffix on either side (10.0.0.1 == 10.0.0.1/24).
    IPv6 zone-ids (fe80::1%eth0) are NOT stripped — differently-scoped
    addresses compare unequal; stripping would GUESS equality across
    scopes. Gateway IPs in M1 are IPv4; revisit if that changes."""
    if a is None or b is None:
        return None
    try:
        pa = ipaddress.ip_address(str(a).split("/", 1)[0])
        pb = ipaddress.ip_address(str(b).split("/", 1)[0])
    except ValueError:
        return None
    if pa.version != pb.version:
        return False
    return pa == pb


def same_subnet(a: str | None, b: str | None) -> bool | None:
    """True/False = a definitive verdict over two parseable CIDR subnets;
    None = comparison UNKNOWN (either side absent or unparseable) — never a
    guessed (in)equality. Normalizes with strict=False so host bits set
    (10.0.10.5/24) compare equal to the network (10.0.10.0/24); a bare host
    becomes /32 (or /128). FAMILY-AWARE: mismatched versions are NOT equal
    (the GS25 lesson — never int-compare across families)."""
    if a is None or b is None:
        return None
    try:
        na = ipaddress.ip_network(str(a), strict=False)
        nb = ipaddress.ip_network(str(b), strict=False)
    except ValueError:
        return None
    if na.version != nb.version:
        return False
    return na == nb
