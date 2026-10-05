"""Bound unchanged gateway addressing dependencies without claiming rollout safety."""

from collections.abc import Mapping
from ipaddress import IPv4Interface
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel


def _static_interface(row: Mapping[str, Any]) -> IPv4Interface | None:
    ip, mask = row.get("ip"), row.get("netmask")
    if row.get("type") != "static" or not isinstance(ip, str) or not isinstance(mask, str):
        return None
    try:
        interface = IPv4Interface(f"{ip}/{mask.removeprefix('/')}")
    except ValueError:
        return None
    # Mist documents a netmask, not an inverse/wildcard mask. Reject address
    # strings carrying their own prefix and non-unicast interface addresses.
    if "/" in ip or ("." in mask and str(interface.netmask) != mask):
        return None
    address, network = interface.ip, interface.network
    if (network.prefixlen == 0 or address.is_unspecified or address.is_multicast
        or address.is_loopback or address.is_link_local or address.is_reserved):
        return None
    if network.prefixlen < 31 and address in (network.network_address, network.broadcast_address):
        return None
    return interface


def same_static_gateway_subnet(before: Any, after: Any) -> bool:
    """Only an IP change with explicit, unchanged static mode and valid mask.

    These dependencies preserve the connected prefix. Endpoint/next-hop/session
    impact still needs review, and other row fields are screened independently.
    """
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    if (before.get("type") != after.get("type")
        or before.get("netmask") != after.get("netmask") or before.get("ip") == after.get("ip")):
        return False
    previous, proposed = _static_interface(before), _static_interface(after)
    return previous is not None and proposed is not None and previous.network == proposed.network


def gateway_address_change_findings(
    baseline: Mapping[str, dict[str, Any]], proposed: Mapping[str, dict[str, Any]],
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for did in sorted(baseline.keys() & proposed.keys()):
        before, after = baseline[did].get("ip_configs") or {}, proposed[did].get("ip_configs") or {}
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            continue
        for name in sorted(before.keys() & after.keys()):
            if not same_static_gateway_subnet(before[name], after[name]):
                continue
            findings.append(Finding(
                source=FindingSource.ADAPTER,
                category=FindingCategory.OPERATIONAL,
                code="scope.gateway_address_change.requires_review",
                subject=ObjectRef("device", did),
                severity=Severity.WARNING,
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                message=(f"gateway {did} address on {name!r} changes within the same static subnet;"
                         " endpoint, next-hop and established-session impact requires review"),
                evidence={"network": name, "before": before[name]["ip"],
                          "after": after[name]["ip"], "netmask": after[name]["netmask"]},
            ))
    return tuple(findings)
