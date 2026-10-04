"""The wired checks. ALL_WIRED_CHECKS is the default registry payload."""

from digital_twin.checks.base import Check

from .admin_disable import AdminDisableCheck
from .auth_change import AuthAccessChangeCheck
from .bgp_adjacency import BgpAdjacencyCheck
from .bgp_prefix_delta import BgpPrefixDeltaCheck
from .client_impact import ClientImpactCheck
from .control_plane_reachability import ControlPlaneReachabilityCheck
from .dhcp_capacity import DhcpCapacityCheck
from .dhcp_path import DhcpPathCheck
from .gateway_gap import GatewayGapCheck
from .l1_param_mismatch import L1ParamMismatchCheck
from .l2_blackhole import L2BlackholeCheck
from .l2_isolation import L2IsolationCheck
from .l2_loop import L2LoopCheck
from .l2_vlan_segmentation import L2VlanSegmentationCheck
from .lag_redundancy import LagRedundancyCheck
from .mac_limit import MacLimitExceededCheck
from .mtu_mismatch import MtuMismatchCheck
from .native_mismatch import NativeVlanMismatchCheck
from .ospf_withdrawal import OspfWithdrawalCheck
from .poe_disconnect import PoeDisconnectCheck
from .radius_missing import RadiusMissingCheck
from .scope_lint import DhcpScopeLintCheck
from .snooping import DhcpSnoopingCheck
from .static_route_reachability import StaticRouteReachabilityCheck
from .storm_control_policy import StormControlPolicyCheck
from .stp_edge import StpEdgeOnUplinkCheck
from .stp_policy import StpPolicyCheck
from .stp_root import StpRootChangeCheck
from .subnet_overlap import SubnetOverlapCheck
from .topology_coverage import TopologyCoverageCheck
from .unmodeled_change import PortUnmodeledChangeCheck
from .vlan_collision import VlanCollisionCheck
from .vrf_leak import VrfLeakCheck
from .wan_redundancy import GatewayWanRedundancyCheck
from .wlan_client_impact import WlanClientImpactCheck
from .wlan_duplicate_ssid import WlanDuplicateSsidCheck
from .wlan_open_guest import WlanOpenGuestCheck

ALL_WIRED_CHECKS: list[Check] = [
    TopologyCoverageCheck(),
    L2LoopCheck(),
    L2BlackholeCheck(),
    L2IsolationCheck(),
    L2VlanSegmentationCheck(),
    NativeVlanMismatchCheck(),
    MtuMismatchCheck(),
    L1ParamMismatchCheck(),
    StpEdgeOnUplinkCheck(),
    StpPolicyCheck(),
    StpRootChangeCheck(),
    GatewayGapCheck(),
    OspfWithdrawalCheck(),
    BgpAdjacencyCheck(),
    BgpPrefixDeltaCheck(),
    StaticRouteReachabilityCheck(),
    VrfLeakCheck(),
    DhcpPathCheck(),
    DhcpScopeLintCheck(),
    DhcpCapacityCheck(),
    DhcpSnoopingCheck(),
    PoeDisconnectCheck(),
    AdminDisableCheck(),
    AuthAccessChangeCheck(),
    RadiusMissingCheck(),
    GatewayWanRedundancyCheck(),
    LagRedundancyCheck(),
    ControlPlaneReachabilityCheck(),
    StormControlPolicyCheck(),
    ClientImpactCheck(),
    WlanClientImpactCheck(),
    WlanOpenGuestCheck(),
    WlanDuplicateSsidCheck(),
    SubnetOverlapCheck(),
    VlanCollisionCheck(),
    MacLimitExceededCheck(),
    PortUnmodeledChangeCheck(),
]

__all__ = [
    "ALL_WIRED_CHECKS",
    "AdminDisableCheck",
    "MacLimitExceededCheck",
    "AuthAccessChangeCheck",
    "BgpAdjacencyCheck",
    "BgpPrefixDeltaCheck",
    "ClientImpactCheck",
    "SubnetOverlapCheck",
    "TopologyCoverageCheck",
    "VlanCollisionCheck",
    "DhcpPathCheck",
    "DhcpCapacityCheck",
    "DhcpScopeLintCheck",
    "DhcpSnoopingCheck",
    "GatewayGapCheck",
    "L1ParamMismatchCheck",
    "L2BlackholeCheck",
    "L2IsolationCheck",
    "L2LoopCheck",
    "L2VlanSegmentationCheck",
    "MtuMismatchCheck",
    "NativeVlanMismatchCheck",
    "OspfWithdrawalCheck",
    "PoeDisconnectCheck",
    "RadiusMissingCheck",
    "StpEdgeOnUplinkCheck",
    "StpPolicyCheck",
    "StpRootChangeCheck",
    "WlanDuplicateSsidCheck",
    "WlanClientImpactCheck",
    "WlanOpenGuestCheck",
    "GatewayWanRedundancyCheck",
    "StaticRouteReachabilityCheck",
    "VrfLeakCheck",
    "LagRedundancyCheck",
    "ControlPlaneReachabilityCheck",
    "StormControlPolicyCheck",
    "PortUnmodeledChangeCheck",
]
