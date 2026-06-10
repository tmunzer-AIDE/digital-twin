"""The four M1 wired checks. ALL_WIRED_CHECKS is the default registry payload."""

from .client_impact import ClientImpactCheck
from .l2_blackhole import L2BlackholeCheck
from .l2_loop import L2LoopCheck
from .l2_vlan_segmentation import L2VlanSegmentationCheck

ALL_WIRED_CHECKS = [
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
