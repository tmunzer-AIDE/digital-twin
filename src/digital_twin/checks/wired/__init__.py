"""The four M1 wired checks. ALL_WIRED_CHECKS is the default registry payload."""

from digital_twin.checks.base import Check

from .client_impact import ClientImpactCheck
from .l2_blackhole import L2BlackholeCheck
from .l2_loop import L2LoopCheck
from .l2_vlan_segmentation import L2VlanSegmentationCheck

ALL_WIRED_CHECKS: list[Check] = [
    L2LoopCheck(),
    L2BlackholeCheck(),
    L2VlanSegmentationCheck(),
    ClientImpactCheck(),
]

__all__ = [
    "ALL_WIRED_CHECKS",
    "ClientImpactCheck",
    "L2BlackholeCheck",
    "L2LoopCheck",
    "L2VlanSegmentationCheck",
]
