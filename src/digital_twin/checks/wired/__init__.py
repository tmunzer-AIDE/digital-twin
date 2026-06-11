"""The wired checks. ALL_WIRED_CHECKS is the default registry payload."""

from digital_twin.checks.base import Check

from .client_impact import ClientImpactCheck
from .gateway_gap import GatewayGapCheck
from .l2_blackhole import L2BlackholeCheck
from .l2_isolation import L2IsolationCheck
from .l2_loop import L2LoopCheck
from .l2_vlan_segmentation import L2VlanSegmentationCheck
from .mtu_mismatch import MtuMismatchCheck
from .native_mismatch import NativeVlanMismatchCheck
from .poe_disconnect import PoeDisconnectCheck
from .stp_edge import StpEdgeOnUplinkCheck
from .stp_root import StpRootChangeCheck

ALL_WIRED_CHECKS: list[Check] = [
    L2LoopCheck(),
    L2BlackholeCheck(),
    L2IsolationCheck(),
    L2VlanSegmentationCheck(),
    NativeVlanMismatchCheck(),
    MtuMismatchCheck(),
    StpEdgeOnUplinkCheck(),
    StpRootChangeCheck(),
    GatewayGapCheck(),
    PoeDisconnectCheck(),
    ClientImpactCheck(),
]

__all__ = [
    "ALL_WIRED_CHECKS",
    "ClientImpactCheck",
    "GatewayGapCheck",
    "L2BlackholeCheck",
    "L2IsolationCheck",
    "L2LoopCheck",
    "L2VlanSegmentationCheck",
    "MtuMismatchCheck",
    "NativeVlanMismatchCheck",
    "PoeDisconnectCheck",
    "StpEdgeOnUplinkCheck",
    "StpRootChangeCheck",
]
