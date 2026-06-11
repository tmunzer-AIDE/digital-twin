"""Shared foundation for link-walking boundary checks (native VLAN, MTU).

A link-walking check must evaluate exactly the links that form LIVE EXTERNAL
L2 boundaries: ports present, neither admin-disabled, ends not folded into one
VC node (chassis backplane), and not AP-transparent (an AP end carries no
config facts by construction). The six-round review series on
wired.l2.native_mismatch (2026-06-10) converged on one invariant: a hazard or
an uncertainty may be demoted to pre-existing ONLY if the baseline had the
very same state on the very same evaluable boundary — so the baseline-side
helper applies PRECISELY this predicate, never a subset.
"""

from __future__ import annotations

from digital_twin.ir.entities import DeviceRole, Link, Port
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR
from digital_twin.ir.provenance import Provenance


def vlan_blind(port: Port) -> bool:
    """No vlan facts and NOT a config statement (stat-ensured or unresolved
    usage): the port's carriage/attributes are UNKNOWN — same notion as the L2
    graph's assumed-carriage rule. A CONFIG port without an attribute, by
    contrast, is a real statement (e.g. 'platform default')."""
    return (
        port.meta.provenance in (Provenance.OBSERVED, Provenance.INFERRED)
        and port.native_vlan is None
        and not port.tagged_vlans
    )


class BoundaryView:
    """One IR's view of which links are evaluable external boundaries."""

    def __init__(self, ir: IR) -> None:
        self._ir = ir
        self._vc_root = vc_root_map(ir)
        self._link_ids = {lk.id for lk in ir.links}

    def pair(self, link: Link) -> tuple[Port, Port] | None:
        """(port_a, port_b) if `link` is a live external boundary in this IR —
        else None. In a BASELINE view, None means the delta is what brings the
        boundary (and any hazard/uncertainty on it) to life."""
        if link.id not in self._link_ids:
            return None
        pa, pb = self._ir.ports.get(link.a_port), self._ir.ports.get(link.b_port)
        if pa is None or pb is None or pa.disabled or pb.disabled:
            return None
        if node_for(self._vc_root, pa.device_id) == node_for(self._vc_root, pb.device_id):
            return None  # VC-internal / self: chassis backplane
        a_ap = self._ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = self._ir.devices[pb.device_id].role is DeviceRole.AP
        if a_ap != b_ap:
            return None  # AP-transparent: the AP end has no config facts
        return pa, pb
