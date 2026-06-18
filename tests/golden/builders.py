"""GS builders: derive each golden scenario from the REDACTED real-org fixture.

The real site's topology is a TREE around one hub switch with no inter-switch
vlan carriage in config (uplink ports are stat-ensured), so the spec scenarios
that need specific preconditions use a documented AUGMENTED variant of the
fixture: an isolated vlan-999 world (network + usages + IRB + a parallel link
on synthetic spare ports + one wired and one wireless client) layered onto the
real topology. The pipeline, gates and checks run UNMODIFIED on it — only the
baseline data is staged. GS5/GS8 run on the untouched fixture.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "site.json"

GS_NET = "gs_net"
GS_VLAN = 999

# anchors discovered in the captured fixture (redacted, stable tokens):
HUB = "f88a379d2cbb"  # core switch (holds the IRB; both HIGH uplinks land here)
EDGE = "a52c4045e19f"  # edge switch (member side), HIGH two-sided link to HUB
EDGE_UPLINK_PORT = "ge-0/0/46"  # real link: EDGE:ge-0/0/46 <-> HUB:mge-0/0/2
HUB_UPLINK_PORT = "mge-0/0/2"
# synthetic spare ports for the augmented PARALLEL link (names unused on site)
EDGE_PAR_PORT = "ge-0/0/98"
HUB_PAR_PORT = "mge-0/0/98"
EDGE_ACCESS_PORT = "ge-0/0/97"  # augmented member access port on EDGE
WIRED_CLIENT_MAC = "ddccbbaa0001"
WIRELESS_CLIENT_MAC = "ddccbbaa0002"


def fixture_doc() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _device(doc: dict[str, Any], mac: str) -> dict[str, Any]:
    return next(d for d in doc["devices"] if str(d.get("mac")) == mac)


def _drop_nones(obj: Any) -> Any:
    """Payloads derived from the REDACTED fixture omit nulled secrets — null and
    absent are the same statement (PUT semantics; the field gate agrees)."""
    if isinstance(obj, dict):
        return {k: _drop_nones(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_nones(v) for v in obj]
    return obj


def augmented_doc(
    *, parallel_carries_gs: bool, with_wireless_client: bool = True
) -> dict[str, Any]:
    """The vlan-999 world. parallel_carries_gs=True -> redundant carriage (GS2);
    False -> single carrier, the parallel link rides an empty trunk (GS1/GS3)."""
    doc = fixture_doc()
    doc["setting"]["networks"][GS_NET] = {"vlan_id": GS_VLAN}
    doc["setting"]["port_usages"]["gs_trunk"] = {"mode": "trunk", "networks": [GS_NET]}
    doc["setting"]["port_usages"]["gs_empty_trunk"] = {"mode": "trunk", "networks": []}
    doc["setting"]["port_usages"]["gs_access"] = {"mode": "access", "port_network": GS_NET}

    edge, hub = _device(doc, EDGE), _device(doc, HUB)
    par_usage = "gs_trunk" if parallel_carries_gs else "gs_empty_trunk"
    edge.setdefault("port_config", {})[EDGE_UPLINK_PORT] = {"usage": "gs_trunk"}
    edge["port_config"][EDGE_PAR_PORT] = {"usage": par_usage}
    edge["port_config"][EDGE_ACCESS_PORT] = {"usage": "gs_access"}
    hub.setdefault("port_config", {})[HUB_UPLINK_PORT] = {"usage": "gs_trunk"}
    hub["port_config"][HUB_PAR_PORT] = {"usage": "gs_trunk"}
    hub.setdefault("other_ip_configs", {})[GS_NET] = {
        "type": "static",
        "ip": "198.51.99.1",
        "netmask": "255.255.255.0",
    }

    # the augmented PARALLEL physical link (two-sided LLDP -> HIGH)
    doc["port_stats"] = list(doc["port_stats"]) + [
        {
            "mac": EDGE,
            "port_id": EDGE_PAR_PORT,
            "up": True,
            "neighbor_mac": HUB,
            "neighbor_port_desc": HUB_PAR_PORT,
        },
        {
            "mac": HUB,
            "port_id": HUB_PAR_PORT,
            "up": True,
            "neighbor_mac": EDGE,
            "neighbor_port_desc": EDGE_PAR_PORT,
        },
    ]

    # one wired client on the augmented access port (client.impact material)
    doc["wired_clients"] = list(doc["wired_clients"]) + [
        {"mac": WIRED_CLIENT_MAC, "device_mac": EDGE, "port_id": EDGE_ACCESS_PORT, "vlan": GS_VLAN}
    ]
    if with_wireless_client:
        # one observed wireless client on vlan 999 via an AP uplinked to the
        # EDGE; the AP's switch port must OFFER vlan 999 in the baseline so the
        # AP-transparent edge carries it (and the delta can take it away)
        ap_mac, ap_port = ap_uplink_on(doc, EDGE)
        doc["setting"]["port_usages"]["gs_ap_trunk"] = {"mode": "trunk", "networks": [GS_NET]}
        edge["port_config"][ap_port] = {"usage": "gs_ap_trunk"}
        doc["wireless_clients"] = list(doc["wireless_clients"]) + [
            {"mac": WIRELESS_CLIENT_MAC, "ap_mac": ap_mac, "vlan_id": GS_VLAN}
        ]
    return doc


GS_WLAN_VLAN = 3001  # a tagged WLAN data vlan with NO IRB (no modeled exit)
GS_MGMT_VLAN = 1  # the AP-mgmt / access-target vlan; given a local exit


def ap_devlan_doc() -> tuple[dict[str, Any], dict[str, Any]]:
    """An AP uplinked to EDGE on a trunk carrying an EXIT-LESS WLAN vlan (3001),
    with NO observed clients. Returns (doc, op): the op flips that uplink port
    trunk -> access on the mgmt vlan, dropping 3001. The mgmt vlan HAS a local
    exit, so the ONLY signal is the AP severed from the exit-less WLAN vlan —
    the real-world 'AP port trunk->access blackholes its WLANs' case.
    """
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    ap_port = ap_uplink_on(doc, EDGE)[1]
    edge = _device(doc, EDGE)
    doc["setting"]["networks"]["gs_wlan"] = {"vlan_id": GS_WLAN_VLAN}
    doc["setting"]["networks"]["gs_mgmt"] = {"vlan_id": GS_MGMT_VLAN}
    doc["setting"]["port_usages"]["gs_ap_trunk"] = {
        "mode": "trunk", "networks": ["gs_wlan"], "port_network": "gs_mgmt"
    }
    doc["setting"]["port_usages"]["gs_mgmt_access"] = {"mode": "access", "port_network": "gs_mgmt"}
    edge.setdefault("other_ip_configs", {})["gs_mgmt"] = {
        "type": "static", "ip": "192.0.2.1", "netmask": "255.255.255.0"
    }
    edge.setdefault("port_config", {})[ap_port] = {"usage": "gs_ap_trunk"}
    doc["wireless_clients"] = []  # still 'fetched' -> clients_active EARNED as empty
    doc["wired_clients"] = []
    dev = copy.deepcopy(edge)
    dev["port_config"][ap_port] = {"usage": "gs_mgmt_access"}  # trunk -> access, drops 3001
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    return doc, op


def ap_wlan_doc(*, wlan_vlan: int, exit_for_wlan: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """An AP uplinked to EDGE on a trunk carrying a tagged WLAN vlan, an enabled
    site WLAN (apply_to=aps) needing that vlan, and NO observed clients. Returns
    (doc, op): the op flips the uplink trunk -> access on the mgmt vlan, dropping
    the WLAN vlan. With `exit_for_wlan` the WLAN vlan has a local IRB on EDGE
    (severance -> exit_lost -> UNSAFE); without, it is exit-less (-> REVIEW).
    """
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    ap_mac, ap_port = ap_uplink_on(doc, EDGE)
    ap_id = str(_device(doc, ap_mac)["id"])
    edge = _device(doc, EDGE)
    doc["setting"]["networks"]["gs_wlan"] = {"vlan_id": wlan_vlan}
    doc["setting"]["networks"]["gs_mgmt"] = {"vlan_id": 1}
    doc["setting"]["port_usages"]["gs_ap_trunk"] = {
        "mode": "trunk", "networks": ["gs_wlan"], "port_network": "gs_mgmt"
    }
    doc["setting"]["port_usages"]["gs_mgmt_access"] = {"mode": "access", "port_network": "gs_mgmt"}
    edge.setdefault("other_ip_configs", {})["gs_mgmt"] = {
        "type": "static", "ip": "192.0.2.1", "netmask": "255.255.255.0"
    }
    if exit_for_wlan:
        edge["other_ip_configs"]["gs_wlan"] = {
            "type": "static", "ip": "198.51.100.1", "netmask": "255.255.255.0"
        }
    edge.setdefault("port_config", {})[ap_port] = {"usage": "gs_ap_trunk"}
    doc["wireless_clients"] = []
    doc["wired_clients"] = []
    doc["wlans"] = [
        {
            "ssid": "corp",
            "enabled": True,
            "vlan_enabled": True,
            "interface": "all",
            "apply_to": "aps",
            "ap_ids": [ap_id],
            "vlan_id": wlan_vlan,
        }
    ]
    doc["meta"]["fetched"] = [*doc["meta"]["fetched"], "wlans"]
    dev = copy.deepcopy(edge)
    dev["port_config"][ap_port] = {"usage": "gs_mgmt_access"}  # trunk -> access, drops WLAN vlan
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    return doc, op


def dynamic_ap_wlan_doc(*, with_stats_row: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ap_wlan world, but the AP-feeding port gets usage `gs_ap_trunk` at
    RUNTIME via a dynamic profile rule (lldp_system_name 'AP_*'). The op
    redefines gs_ap_trunk trunk->access at device level — affecting the port
    only through its RESOLVED runtime usage. With the port-stats row the twin
    resolves the profile (-> precise verdict, no blanket gate); without it the
    runtime usage is unknowable (-> the unresolved-dynamic gate, REVIEW).
    """
    doc, _ = ap_wlan_doc(wlan_vlan=3100, exit_for_wlan=True)
    edge = _device(doc, EDGE)
    ap_port = ap_uplink_on(doc, EDGE)[1]
    # isolate the behavior under test: the REAL fixture device (and its
    # template's switch_matching rules) carry other, genuinely unresolvable
    # dynamic ports that would honestly trip the gate — strip them so the only
    # dynamic port in this world is the test subject
    edge["port_config"] = {
        k: v
        for k, v in edge["port_config"].items()
        if not (isinstance(v, dict) and v.get("dynamic_usage"))
    }
    if isinstance(doc.get("networktemplate"), dict):
        doc["networktemplate"].pop("switch_matching", None)
    edge["port_config"][ap_port] = {"usage": "default", "dynamic_usage": "gs_dyn"}
    doc["setting"]["port_usages"]["gs_dyn"] = {
        "mode": "dynamic",
        "rules": [
            {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_",
             "usage": "gs_ap_trunk"}
        ],
    }
    # drop the fixture's own rows for this port (a real row with a rule-missing
    # neighbor would CONCLUSIVELY keep the static usage — a different world)
    doc["port_stats"] = [
        r
        for r in doc["port_stats"]
        if not (r.get("mac") == EDGE and r.get("port_id") == ap_port)
    ]
    if with_stats_row:
        # 'AP_GS14' matches the rule but no site device name -> no link side-effects
        doc["port_stats"] = list(doc["port_stats"]) + [
            {"mac": EDGE, "port_id": ap_port, "up": True, "neighbor_system_name": "AP_GS14"}
        ]
    usages = {
        **_drop_nones(edge.get("port_usages") or {}),
        "gs_ap_trunk": {"mode": "access", "port_network": "gs_mgmt"},
    }
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(edge["id"]),
        "payload": {"type": "switch", "port_usages": usages},
    }
    return doc, op


def ap_unresolved_wlan_doc() -> tuple[dict[str, Any], dict[str, Any]]:
    """An AP whose only WLAN is wxtag-scoped (AP membership unresolvable) and
    whose uplink trunk carries vlan 999 (exit on HUB); the op flips it
    trunk->access (drops 999). The twin can't verify the wxtag WLAN's needs ->
    coverage note (REVIEW), never a false SAFE."""
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    ap_port = ap_uplink_on(doc, EDGE)[1]
    edge = _device(doc, EDGE)
    doc["setting"]["port_usages"]["gs_ap_trunk"] = {"mode": "trunk", "networks": [GS_NET]}
    edge.setdefault("port_config", {})[ap_port] = {"usage": "gs_ap_trunk"}
    doc["wireless_clients"] = []
    doc["wired_clients"] = []
    doc["wlans"] = [
        {
            "ssid": "guest",
            "enabled": True,
            "vlan_enabled": True,
            "interface": "all",
            "apply_to": "wxtags",
            "wxtag_ids": ["t1"],
            "vlan_id": 999,
        }
    ]
    doc["meta"]["fetched"] = [*doc["meta"]["fetched"], "wlans"]
    dev = copy.deepcopy(edge)
    dev["port_config"][ap_port] = {"usage": "gs_empty_trunk"}  # drops vlan 999
    op = {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }
    return doc, op


# --- CA: cause-attribution motivating scenario (one op, two trunk ports) ----
#
# The spec's motivating case: ONE device op re-profiles TWO trunk ports, each
# the sole carrier of a distinct client vlan (each with an IRB exit on HUB and a
# member access port + observed client on EDGE). Dropping both vlans partitions
# them (segmentation .split) AND strands their members from the exit (blackhole
# .exit_lost) — and each resulting finding must name the ONE port that carried
# that vlan. The fixture also already holds PRE-EXISTING (delta-untouched)
# blackholes (vlan 2 / vlan 22 from the real captured config), so the goldens
# can pin that those preexisting* rows carry NO cause.

CA_VLAN_A = 991
CA_VLAN_B = 992
CA_EDGE_PORT_A = "ge-0/0/90"  # EDGE trunk carrying ONLY vlan 991 (-> HUB)
CA_EDGE_PORT_B = "ge-0/0/91"  # EDGE trunk carrying ONLY vlan 992 (-> HUB)
CA_HUB_PORT_A = "mge-0/0/90"
CA_HUB_PORT_B = "mge-0/0/91"
CA_ACCESS_PORT_A = "ge-0/0/92"  # EDGE member access port on vlan 991
CA_ACCESS_PORT_B = "ge-0/0/93"  # EDGE member access port on vlan 992
CA_CLIENT_A = "ddccbbaa0091"
CA_CLIENT_B = "ddccbbaa0092"


def multi_port_cut_doc() -> tuple[dict[str, Any], dict[str, Any]]:
    """(doc, op): one EDGE device op flips BOTH CA_EDGE_PORT_A and
    CA_EDGE_PORT_B (each the lone carrier of one vlan) trunk -> empty-trunk,
    dropping vlan 991 and vlan 992. Each vlan strands its member from its HUB
    IRB exit -> two blackhole.exit_lost (ERROR) AND two vlan_segmentation.split
    (WARNING), each attributable to the ONE port that carried it."""
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    edge, hub = _device(doc, EDGE), _device(doc, HUB)
    doc["setting"]["networks"]["mp_a"] = {"vlan_id": CA_VLAN_A}
    doc["setting"]["networks"]["mp_b"] = {"vlan_id": CA_VLAN_B}
    doc["setting"]["port_usages"]["mp_trunk_a"] = {"mode": "trunk", "networks": ["mp_a"]}
    doc["setting"]["port_usages"]["mp_trunk_b"] = {"mode": "trunk", "networks": ["mp_b"]}
    doc["setting"]["port_usages"]["mp_access_a"] = {"mode": "access", "port_network": "mp_a"}
    doc["setting"]["port_usages"]["mp_access_b"] = {"mode": "access", "port_network": "mp_b"}
    edge["port_config"][CA_EDGE_PORT_A] = {"usage": "mp_trunk_a"}
    edge["port_config"][CA_EDGE_PORT_B] = {"usage": "mp_trunk_b"}
    edge["port_config"][CA_ACCESS_PORT_A] = {"usage": "mp_access_a"}
    edge["port_config"][CA_ACCESS_PORT_B] = {"usage": "mp_access_b"}
    hub["port_config"][CA_HUB_PORT_A] = {"usage": "mp_trunk_a"}
    hub["port_config"][CA_HUB_PORT_B] = {"usage": "mp_trunk_b"}
    hub["other_ip_configs"]["mp_a"] = {
        "type": "static", "ip": "198.51.91.1", "netmask": "255.255.255.0"
    }
    hub["other_ip_configs"]["mp_b"] = {
        "type": "static", "ip": "198.51.92.1", "netmask": "255.255.255.0"
    }
    # the two augmented physical links EDGE<->HUB (two-sided LLDP -> HIGH)
    doc["port_stats"] = list(doc["port_stats"]) + [
        {"mac": EDGE, "port_id": CA_EDGE_PORT_A, "up": True,
         "neighbor_mac": HUB, "neighbor_port_desc": CA_HUB_PORT_A},
        {"mac": HUB, "port_id": CA_HUB_PORT_A, "up": True,
         "neighbor_mac": EDGE, "neighbor_port_desc": CA_EDGE_PORT_A},
        {"mac": EDGE, "port_id": CA_EDGE_PORT_B, "up": True,
         "neighbor_mac": HUB, "neighbor_port_desc": CA_HUB_PORT_B},
        {"mac": HUB, "port_id": CA_HUB_PORT_B, "up": True,
         "neighbor_mac": EDGE, "neighbor_port_desc": CA_EDGE_PORT_B},
    ]
    doc["wired_clients"] = list(doc["wired_clients"]) + [
        {"mac": CA_CLIENT_A, "device_mac": EDGE, "port_id": CA_ACCESS_PORT_A, "vlan": CA_VLAN_A},
        {"mac": CA_CLIENT_B, "device_mac": EDGE, "port_id": CA_ACCESS_PORT_B, "vlan": CA_VLAN_B},
    ]
    op = device_op(
        doc, EDGE,
        **{CA_EDGE_PORT_A.replace("/", "__"): "gs_empty_trunk",
           CA_EDGE_PORT_B.replace("/", "__"): "gs_empty_trunk"},
    )
    return doc, op


def ap_uplink_on(doc: dict[str, Any], switch_mac: str) -> tuple[str, str]:
    """(ap_mac, switch_port) of an AP whose lldp_stat names the given switch."""
    for stat in doc["device_stats"]:
        if stat.get("type") != "ap" or not stat.get("mac"):
            continue
        lldp = stat.get("lldp_stat") or {}
        if str(lldp.get("chassis_id", "")).replace(":", "") == switch_mac and lldp.get("port_id"):
            return str(stat["mac"]), str(lldp["port_id"])
    raise AssertionError(f"no AP uplinked to {switch_mac} in fixture")


def write_doc(doc: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(doc))
    return path


def plan_for(doc: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    scope = doc["scope"]
    return {
        "source": "mist",
        "scope": {"org_id": scope["org_id"], "site_id": scope["site_id"]},
        "ops": ops,
    }


def device_op(doc: dict[str, Any], mac: str, order: int = 0, **port_usages: str) -> dict[str, Any]:
    """A device op re-assigning the given ports' usages (full-object payload)."""
    dev = copy.deepcopy(_device(doc, mac))
    for port, usage in port_usages.items():
        dev.setdefault("port_config", {})[port.replace("__", "/")] = {"usage": usage}
    return {
        "action": "update",
        "order": order,
        "object_type": "device",
        "object_id": str(dev["id"]),
        "payload": _drop_nones(dev),
    }


OSPF_NETS = {  # name -> (vlan_id, subnet)
    "ospf_transit": (970, "198.51.70.0/24"),
    "ospf_corp": (971, "198.51.71.0/24"),
}


def ospf_doc(
    entries: dict[str, dict[str, Any]], *, client_vlan: int | None = None
) -> dict[str, Any]:
    """HUB switch running OSPF. `entries` maps a name from OSPF_NETS to its
    ospf_areas network entry ({} = active, {"passive": True} = stub). Each named
    net gets a Vlan (with subnet) + an IRB on HUB (a routed segment). Optionally
    place one observed wired client on `client_vlan`."""
    doc = fixture_doc()
    hub = _device(doc, HUB)
    # the redacted fixture left the HUB's remote_syslog full of blanked tokens
    # (time_format=""), an enum violation L0 would surface on the rendered HUB —
    # noise unrelated to OSPF that has no bearing on the withdrawal verdict.
    hub.pop("remote_syslog", None)
    hub["ospf_config"] = {"enabled": True}
    networks_block: dict[str, Any] = {}
    for name, entry in entries.items():
        vid, subnet = OSPF_NETS[name]
        doc["setting"]["networks"][name] = {"vlan_id": vid, "subnet": subnet}
        hub.setdefault("other_ip_configs", {})[name] = {
            "type": "static", "ip": subnet.replace(".0/24", ".1"), "netmask": "255.255.255.0",
        }
        networks_block[name] = entry
    hub["ospf_areas"] = {"0": {"networks": networks_block}}
    if client_vlan is not None:
        hub_port = "ge-0/0/40"
        port_net = next(n for n, (v, _) in OSPF_NETS.items() if v == client_vlan)
        doc["setting"]["port_usages"]["ospf_access"] = {
            "mode": "access", "port_network": port_net
        }
        hub.setdefault("port_config", {})[hub_port] = {"usage": "ospf_access"}
        doc["wired_clients"] = list(doc["wired_clients"]) + [
            {"mac": WIRED_CLIENT_MAC, "device_mac": HUB, "port_id": hub_port, "vlan": client_vlan}
        ]
    return doc


# --- MS: org networktemplate (multi-site) goldens -------------------------
#
# Two sites share ONE networktemplate `nt1`. The template defines a `corp`
# network (vlan 950) and the port usages that carry it; the per-site pipeline
# OVERRIDES each site's networktemplate with the (baseline | proposed) snapshot,
# so the only delta is the template edit.
#
# Site A: a switch (EDGE) with a member access port + observed wired client on
# corp, whose ONLY exit is the IRB on HUB, reached over the EDGE->HUB uplink
# trunk (`ms_trunk`, carries corp). Removing corp from `ms_trunk` strands the
# member from the IRB -> blackhole.exit_lost (UNSAFE). Site B uses none of it.

MS_TEMPLATE_ID = "nt1"
MS_NET = "ms_corp"
MS_VLAN = 950
MS_SUBNET = "198.51.95.0/24"
MS_SITE_A = "siteA"
MS_SITE_B = "siteB"


def _strip_dynamic_ports(doc: dict[str, Any]) -> None:
    """Remove every switch port whose runtime usage comes from a dynamic profile.
    The shared `nt1` drops the (unobservable-rule) `dynamic` usage + switch_matching
    to keep the scenario about the corp edit; a lingering dynamic_usage reference
    would resolve to 'no definition' -> the dynamic-ports honesty gate (REVIEW),
    noise unrelated to the template change under test."""
    for dev in doc["devices"]:
        if dev.get("type") != "switch":
            continue
        pc = dev.get("port_config")
        if isinstance(pc, dict):
            dev["port_config"] = {
                k: v
                for k, v in pc.items()
                if not (isinstance(v, dict) and v.get("dynamic_usage"))
            }


def _ms_template(base_nt: dict[str, Any]) -> dict[str, Any]:
    """The shared `nt1`: the fixture's real template (so every real device port
    reference still resolves after the override) PLUS the corp network and the
    usages that carry it — minus the unobservable-rule dynamic machinery (see
    _strip_dynamic_ports). Built once and shared by both sites."""
    nt = copy.deepcopy(base_nt)
    nt["id"] = MS_TEMPLATE_ID
    nt.pop("switch_matching", None)
    (nt.get("port_usages") or {}).pop("dynamic", None)
    nt.setdefault("networks", {})[MS_NET] = {"vlan_id": MS_VLAN, "subnet": MS_SUBNET}
    pu = nt.setdefault("port_usages", {})
    pu["ms_trunk"] = {"mode": "trunk", "networks": [MS_NET]}
    pu["ms_access"] = {"mode": "access", "port_network": MS_NET}
    return nt


def _ms_site_a() -> dict[str, Any]:
    """Site A doc: corp lives in the TEMPLATE (not setting), the EDGE->HUB uplink
    rides `ms_trunk`, the IRB on HUB exits corp, and a wired client sits on a
    corp access port on EDGE."""
    doc = augmented_doc(parallel_carries_gs=False, with_wireless_client=False)
    # corp + its usages belong to the template here, NOT to the site setting:
    # strip the augmented setting-level copies so the template is the sole owner
    doc["setting"]["networks"].pop(GS_NET, None)
    for u in ("gs_trunk", "gs_empty_trunk", "gs_access"):
        doc["setting"]["port_usages"].pop(u, None)
    # repoint the EDGE/HUB ports + access port onto the template-owned usages,
    # and the HUB IRB + wired client onto corp's vlan
    edge, hub = _device(doc, EDGE), _device(doc, HUB)
    edge["port_config"][EDGE_UPLINK_PORT] = {"usage": "ms_trunk"}
    edge["port_config"][EDGE_ACCESS_PORT] = {"usage": "ms_access"}
    hub["port_config"][HUB_UPLINK_PORT] = {"usage": "ms_trunk"}
    hub["other_ip_configs"] = {
        MS_NET: {"type": "static", "ip": "198.51.95.1", "netmask": "255.255.255.0"}
    }
    doc["wired_clients"] = [
        {"mac": WIRED_CLIENT_MAC, "device_mac": EDGE, "port_id": EDGE_ACCESS_PORT, "vlan": MS_VLAN}
    ]
    doc["wireless_clients"] = []
    doc["site"]["networktemplate_id"] = MS_TEMPLATE_ID
    doc["scope"]["site_id"] = MS_SITE_A
    return doc


def _ms_site_b(*, networktemplate_id: str = MS_TEMPLATE_ID) -> dict[str, Any]:
    """Site B doc: the untouched real fixture, assigned to `networktemplate_id`.
    It carries NO corp member and NO corp IRB, so a corp edit cannot break it."""
    doc = fixture_doc()
    doc["site"]["networktemplate_id"] = networktemplate_id
    doc["scope"]["site_id"] = MS_SITE_B
    return doc


def _to_site_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """A golden `doc` (already the saved-fixture shape) IS a single-site fixture
    doc — the multi-site FixtureProvider consumes it directly. The per-site
    networktemplate is kept (load_fixture_doc reads it) but is overridden at
    runtime by override_template, which pins each site to the shared snapshot."""
    return doc


def multisite_doc(
    *,
    site_a_template_id: str = MS_TEMPLATE_ID,
    site_b_template_id: str = MS_TEMPLATE_ID,
    fetch_failures: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The 2-site multi-site fixture sharing `nt1`. `*_template_id` let a scenario
    detach a site from the edited template (MS-d); `fetch_failures` marks a site
    as a provider FetchError (MS-b)."""
    site_a = _ms_site_a()
    site_a["site"]["networktemplate_id"] = site_a_template_id
    template = _ms_template(site_a["networktemplate"])
    site_b = _ms_site_b(networktemplate_id=site_b_template_id)
    _strip_dynamic_ports(site_a)
    _strip_dynamic_ports(site_b)
    return {
        "template": template,
        "sites": {MS_SITE_A: _to_site_doc(site_a), MS_SITE_B: _to_site_doc(site_b)},
        "fetch_failures": list(fetch_failures),
    }


def _ms_plan(template: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    org_id = template.get("org_id") or "o1"
    return {
        "source": "mist",
        "scope": {"org_id": org_id},  # NO site_id -> org mode
        "ops": [{
            "action": "update", "order": 0, "object_type": "networktemplate",
            "object_id": MS_TEMPLATE_ID, "payload": payload,
        }],
    }


def _ms_port_usages_payload(template: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A full `port_usages` root-replace payload (Mist replaces present roots
    wholesale — a partial map would delete every other usage and trip the field
    gate). Carries each modeled usage's networks/port_network so the diff is
    EXACTLY the overridden usage(s)."""
    pu = {k: dict(v) for k, v in (template.get("port_usages") or {}).items()
          if isinstance(v, dict)}
    for name, networks in overrides.items():
        pu[name] = {**pu.get(name, {}), "networks": list(networks)}
    return {"port_usages": pu}


def multisite_remove_corp() -> tuple[dict[str, Any], dict[str, Any]]:
    """MS-a: the template drops corp from `ms_trunk` -> site A's EDGE uplink stops
    carrying corp -> the corp member strands from the HUB IRB (exit_lost, UNSAFE);
    site B never used corp (SAFE)."""
    doc = multisite_doc()
    payload = _ms_port_usages_payload(doc["template"], ms_trunk=[])  # corp removed
    return doc, _ms_plan(doc["template"], payload)


def multisite_with_failed_site() -> tuple[dict[str, Any], dict[str, Any]]:
    """MS-b: same corp removal, but site B's fetch fails at the provider -> the
    org rollup is UNKNOWN with siteB in site_failures."""
    doc = multisite_doc(fetch_failures=(MS_SITE_B,))
    payload = _ms_port_usages_payload(doc["template"], ms_trunk=[])
    return doc, _ms_plan(doc["template"], payload)


def multisite_add_unused_vlan() -> tuple[dict[str, Any], dict[str, Any]]:
    """MS-c: a cosmetic template edit — add a brand-new vlan to the corp-less
    `ms_access` usage's... no: add a new network nothing uses. Neither site has a
    member/IRB on it -> SAFE. The edit adds the network via a networks payload."""
    doc = multisite_doc()
    nets = {k: dict(v) for k, v in (doc["template"].get("networks") or {}).items()
            if isinstance(v, dict)}
    nets["ms_unused"] = {"vlan_id": 951}  # modeled leaf only (vlan_id), nothing uses it
    payload = {"networks": nets}
    return doc, _ms_plan(doc["template"], payload)


def multisite_template_with_no_assigned_sites() -> tuple[dict[str, Any], dict[str, Any]]:
    """MS-d: the fixture's two sites are assigned to a DIFFERENT template, so
    resolve_org_template returns 0 assigned sites for nt1 -> SAFE (valid template,
    no impact simulated)."""
    doc = multisite_doc(site_a_template_id="nt_other", site_b_template_id="nt_other")
    payload = _ms_port_usages_payload(doc["template"], ms_trunk=[])
    return doc, _ms_plan(doc["template"], payload)


# ---------------------------------------------------------------------------
# GT / ST: gatewaytemplate + sitetemplate org-template goldens (Task 20)
#
# Two sites share ONE gatewaytemplate `g1`. The template defines ip_configs for
# a `gt_corp` network (vlan 960, subnet 198.51.96.0/24) and the port_config
# that carries it. A gateway device on each site inherits from the template.
#
# The `gt_corp` vlan carries a declared default `gateway` pointing at the
# template's ip_configs address (198.51.96.1), which the ingest resolves to a
# gateway L3Intf (CONFIG/HIGH) via org_networks. Moving that address to an
# address no modeled interface owns breaks the known gateway -> UNSAFE.
#
# Site A: has the gateway device AND a wired switch environment. Changing the
#   template's ip_configs.gt_corp.ip breaks the gateway_gap.gateway_unowned
#   check -> UNSAFE.
# Site B: a minimal site with NO gateway device; a template edit that would
#   change the gateway L3Intf is harmless here (no gateway to affect).
# ---------------------------------------------------------------------------

GT_TEMPLATE_ID = "g1"
GT_NET = "gt_corp"
GT_VLAN = 960
GT_SUBNET = "198.51.96.0/24"
GT_GW_IP = "198.51.96.1"         # baseline: gateway L3Intf + vlan's declared GW
GT_GW_IP_ALT = "198.51.96.99"    # proposed: different IP, unowned by any interface
GT_SITE_A = "gtSiteA"
GT_SITE_B = "gtSiteB"
GT_GW_MAC = "aa0000000099"
GT_GW_ID = "gw-gt1"
# A minimal switch on Site A so the switch pipeline has something to ingest
GT_SW_MAC = "aa0000000001"
GT_SW_ID = "sw-gt1"

# The org_id shared by both GT sites (borrowed from the real fixture to keep
# the FixtureProvider strict-scope guard happy when replay uses a unique id).
_GT_ORG_ID = "org-gt-tests"


def _gt_org_networks() -> list[dict[str, Any]]:
    """org_networks row for gt_corp so the gateway ip_configs resolves."""
    return [{"name": GT_NET, "vlan_id": GT_VLAN, "subnet": GT_SUBNET}]


def _gt_base_template() -> dict[str, Any]:
    """The shared gatewaytemplate g1: ip_configs for gt_corp + a LAN port."""
    return {
        "id": GT_TEMPLATE_ID,
        "name": "gt-shared",
        "ip_configs": {GT_NET: {"ip": GT_GW_IP}},
        "port_config": {"ge-0/0/0": {"networks": [GT_NET]}},
    }


def _gt_site_a_doc() -> dict[str, Any]:
    """Site A: a gateway device inheriting from the template + a switch.

    The site_setting carries gt_corp (vlan 960, subnet, gateway 198.51.96.1).
    org_networks is fetched and contains gt_corp so gateway ip_configs resolves.
    The switch has one member port on vlan 960.
    """
    return {
        "redaction_version": 6,
        "scope": {"org_id": _GT_ORG_ID, "site_id": GT_SITE_A},
        "meta": {
            "acquired_at": "2026-06-15T00:00:00+00:00",
            "host": "api.mist.com",
            "fetched": [
                "site", "setting", "networktemplate", "devices",
                "device_stats", "port_stats", "wireless_clients",
                "wired_clients", "org_networks",
            ],
            "failures": [],
        },
        "site": {
            "id": GT_SITE_A,
            "org_id": _GT_ORG_ID,
            "networktemplate_id": None,
            "gatewaytemplate_id": GT_TEMPLATE_ID,
            "sitetemplate_id": None,
        },
        "setting": {
            "networks": {GT_NET: {"vlan_id": GT_VLAN, "subnet": GT_SUBNET,
                                  "gateway": GT_GW_IP}},
            "port_usages": {"gt_access": {"mode": "access", "port_network": GT_NET}},
        },
        "networktemplate": None,
        "gatewaytemplate": _gt_base_template(),
        "sitetemplate": None,
        "derived_setting": None,
        "devices": [
            {
                "mac": GT_GW_MAC,
                "id": GT_GW_ID,
                "type": "gateway",
                "model": "SRX300",
                "ip_configs": {},  # template inherits via compile_gateway_device
                "port_config": {},
            },
            {
                "mac": GT_SW_MAC,
                "id": GT_SW_ID,
                "type": "switch",
                "model": "EX2300-24P",
                "port_config": {"ge-0/0/0": {"usage": "gt_access"}},
            },
        ],
        "device_stats": [],
        "port_stats": [],
        "wireless_clients": [],
        "wired_clients": [],
        "wlans": [],
        "org_networks": _gt_org_networks(),
    }


def _gt_site_b_doc() -> dict[str, Any]:
    """Site B: a minimal site with NO gateway device.

    Even if the template's ip_configs changes, there is no gateway L3Intf to
    lose here — the site is unaffected.
    """
    return {
        "redaction_version": 6,
        "scope": {"org_id": _GT_ORG_ID, "site_id": GT_SITE_B},
        "meta": {
            "acquired_at": "2026-06-15T00:00:00+00:00",
            "host": "api.mist.com",
            "fetched": [
                "site", "setting", "networktemplate", "devices",
                "device_stats", "port_stats", "wireless_clients",
                "wired_clients", "org_networks",
            ],
            "failures": [],
        },
        "site": {
            "id": GT_SITE_B,
            "org_id": _GT_ORG_ID,
            "networktemplate_id": None,
            "gatewaytemplate_id": GT_TEMPLATE_ID,
            "sitetemplate_id": None,
        },
        "setting": {
            "networks": {GT_NET: {"vlan_id": GT_VLAN}},
            "port_usages": {},
        },
        "networktemplate": None,
        "gatewaytemplate": _gt_base_template(),
        "sitetemplate": None,
        "derived_setting": None,
        "devices": [],
        "device_stats": [],
        "port_stats": [],
        "wireless_clients": [],
        "wired_clients": [],
        "wlans": [],
        "org_networks": _gt_org_networks(),
    }


def gt_multisite_doc(
    *,
    site_a_template_id: str = GT_TEMPLATE_ID,
    site_b_template_id: str = GT_TEMPLATE_ID,
    fetch_failures: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The 2-site gatewaytemplate fixture using the typed 'templates' shape."""
    site_a = _gt_site_a_doc()
    site_b = _gt_site_b_doc()
    site_a["site"]["gatewaytemplate_id"] = site_a_template_id
    site_b["site"]["gatewaytemplate_id"] = site_b_template_id
    return {
        "templates": {
            "gatewaytemplate": {
                GT_TEMPLATE_ID: _gt_base_template(),
            }
        },
        "sites": {GT_SITE_A: site_a, GT_SITE_B: site_b},
        "fetch_failures": list(fetch_failures),
    }


def _gt_plan(payload: dict[str, Any], *, template_id: str = GT_TEMPLATE_ID) -> dict[str, Any]:
    return {
        "source": "mist",
        "scope": {"org_id": _GT_ORG_ID},  # NO site_id -> org mode
        "ops": [{
            "action": "update", "order": 0, "object_type": "gatewaytemplate",
            "object_id": template_id, "payload": payload,
        }],
    }


def gt_break_gateway_ip() -> tuple[dict[str, Any], dict[str, Any]]:
    """GT-a: the template changes ip_configs.gt_corp.ip to an address that does
    NOT match the vlan's declared gateway (198.51.96.1). The baseline L3Intf is
    the KNOWN owner of the declared gateway; breaking it -> gateway_unowned
    (ERROR/HIGH) -> org UNSAFE. Site B has no gateway -> unaffected (SAFE)."""
    doc = gt_multisite_doc()
    # Full ip_configs payload (root-replace semantics: omitting a net-name
    # removes it; provide ALL intended names to avoid false deletions).
    payload = {"ip_configs": {GT_NET: {"ip": GT_GW_IP_ALT}}}
    return doc, _gt_plan(payload)


def gt_edit_unmodeled_field() -> tuple[dict[str, Any], dict[str, Any]]:
    """GT-b: edit routing_policies (NOT in the gatewaytemplate allowlist) ->
    raw field gate fires -> org UNKNOWN."""
    doc = gt_multisite_doc()
    payload = {"routing_policies": {"my-policy": {"action": "permit"}}}
    return doc, _gt_plan(payload)


def gt_edit_networks() -> tuple[dict[str, Any], dict[str, Any]]:
    """GT-c: edit gatewaytemplate.networks (NOT in the gatewaytemplate allowlist
    — gateway networks come from org_networks, not the device's own networks
    field) -> raw field gate fires -> org UNKNOWN."""
    doc = gt_multisite_doc()
    payload = {"networks": {GT_NET: {"vlan_id": GT_VLAN, "subnet": GT_SUBNET}}}
    return doc, _gt_plan(payload)


def gt_cosmetic_edit() -> tuple[dict[str, Any], dict[str, Any]]:
    """GT-d: a no-op edit — the payload carries the SAME ip_configs value as the
    baseline. The IR is identical before and after -> SAFE (no findings, full
    coverage). Uses the minimal ip_configs payload; the field gate passes because
    ip_configs.*.ip IS in the gatewaytemplate allowlist."""
    doc = gt_multisite_doc()
    # Same value as baseline -> identical effective after template override
    payload = {"ip_configs": {GT_NET: {"ip": GT_GW_IP}}}
    return doc, _gt_plan(payload)


def gt_fetch_fail_site() -> tuple[dict[str, Any], dict[str, Any]]:
    """GT-e: same IP change as GT-a but site B's fetch fails -> org UNKNOWN
    (site_failures contains GT_SITE_B)."""
    doc = gt_multisite_doc(fetch_failures=(GT_SITE_B,))
    payload = {"ip_configs": {GT_NET: {"ip": GT_GW_IP_ALT}}}
    return doc, _gt_plan(payload)


# ---------------------------------------------------------------------------
# ST: sitetemplate org-template goldens
#
# A sitetemplate (`st1`) is shared by two sites. The sitetemplate carries a
# switch surface (port_usages + networks). The switch site uses the template's
# usages via the switch compile path.
#
# A cosmetic sitetemplate edit (add a new network nothing uses) -> SAFE.
# An edit to a sitetemplate.networks.*.vlan_id (modeled leaf) -> SAFE when no
#   existing device/usage references the changed vlan.
# ---------------------------------------------------------------------------

ST_TEMPLATE_ID = "st1"
ST_NET = "st_mgmt"
ST_VLAN = 940
ST_SITE_A = "stSiteA"
ST_SITE_B = "stSiteB"
_ST_ORG_ID = "org-st-tests"


def _st_base_template() -> dict[str, Any]:
    """The shared sitetemplate st1."""
    return {
        "id": ST_TEMPLATE_ID,
        "name": "st-shared",
        "networks": {ST_NET: {"vlan_id": ST_VLAN}},
        "port_usages": {"st_access": {"mode": "access", "port_network": ST_NET}},
    }


def _st_site_doc(site_id: str, *, sitetemplate_id: str = ST_TEMPLATE_ID) -> dict[str, Any]:
    """A minimal switch site using the sitetemplate."""
    return {
        "redaction_version": 6,
        "scope": {"org_id": _ST_ORG_ID, "site_id": site_id},
        "meta": {
            "acquired_at": "2026-06-15T00:00:00+00:00",
            "host": "api.mist.com",
            "fetched": [
                "site", "setting", "networktemplate", "devices",
                "device_stats", "port_stats", "wireless_clients", "wired_clients",
            ],
            "failures": [],
        },
        "site": {
            "id": site_id,
            "org_id": _ST_ORG_ID,
            "networktemplate_id": None,
            "gatewaytemplate_id": None,
            "sitetemplate_id": sitetemplate_id,
        },
        "setting": {"networks": {}, "port_usages": {}},
        "networktemplate": None,
        "gatewaytemplate": None,
        "sitetemplate": _st_base_template(),
        "derived_setting": None,
        "devices": [],
        "device_stats": [],
        "port_stats": [],
        "wireless_clients": [],
        "wired_clients": [],
        "wlans": [],
        "org_networks": [],
    }


def st_multisite_doc(
    *,
    site_a_template_id: str = ST_TEMPLATE_ID,
    site_b_template_id: str = ST_TEMPLATE_ID,
    fetch_failures: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The 2-site sitetemplate fixture using the typed 'templates' shape."""
    return {
        "templates": {
            "sitetemplate": {
                ST_TEMPLATE_ID: _st_base_template(),
            }
        },
        "sites": {
            ST_SITE_A: _st_site_doc(ST_SITE_A, sitetemplate_id=site_a_template_id),
            ST_SITE_B: _st_site_doc(ST_SITE_B, sitetemplate_id=site_b_template_id),
        },
        "fetch_failures": list(fetch_failures),
    }


def _st_plan(payload: dict[str, Any], *, template_id: str = ST_TEMPLATE_ID) -> dict[str, Any]:
    return {
        "source": "mist",
        "scope": {"org_id": _ST_ORG_ID},  # NO site_id -> org mode
        "ops": [{
            "action": "update", "order": 0, "object_type": "sitetemplate",
            "object_id": template_id, "payload": payload,
        }],
    }


def st_add_unused_vlan() -> tuple[dict[str, Any], dict[str, Any]]:
    """ST-a: cosmetic sitetemplate edit — add a new network nothing uses -> SAFE."""
    doc = st_multisite_doc()
    nets = {k: dict(v) for k, v in (_st_base_template().get("networks") or {}).items()}
    nets["st_unused"] = {"vlan_id": 941}
    payload = {"networks": nets}
    return doc, _st_plan(payload)


def st_fetch_fail_site() -> tuple[dict[str, Any], dict[str, Any]]:
    """ST-b: cosmetic sitetemplate edit but site B's fetch fails -> UNKNOWN."""
    doc = st_multisite_doc(fetch_failures=(ST_SITE_B,))
    nets = {k: dict(v) for k, v in (_st_base_template().get("networks") or {}).items()}
    nets["st_unused"] = {"vlan_id": 941}
    payload = {"networks": nets}
    return doc, _st_plan(payload)


# ---------------------------------------------------------------------------
# DP: device-profile golden (Task 20 scenario 7)
#
# A site with a gateway device carrying a `deviceprofile_id`. A gatewaytemplate
# edit that changes an OVERRIDABLE gateway leaf (ip_configs.*.ip) on that device
# -> the device-profile gate fires (UNKNOWN), because the unmodeled profile layer
# could override the outcome. A site where ONLY an AP carries a profile is not
# tainted (APs are ignored by the gate).
# ---------------------------------------------------------------------------

DP_ORG_ID = "org-dp-tests"
DP_SITE = "dpSite"
DP_GT_ID = "dp-g1"
DP_NET = "dp_net"
DP_VLAN = 970
DP_GW_MAC = "bb0000000001"
DP_GW_ID = "gw-dp1"
DP_AP_MAC = "cc0000000001"
DP_AP_ID = "ap-dp1"


def _dp_gateway_device(*, with_profile: bool) -> dict[str, Any]:
    gw: dict[str, Any] = {
        "mac": DP_GW_MAC,
        "id": DP_GW_ID,
        "type": "gateway",
        "model": "SRX300",
        "ip_configs": {},
        "port_config": {},
    }
    if with_profile:
        gw["deviceprofile_id"] = "dp-profile-1"
    return gw


def _dp_ap_device(*, with_profile: bool) -> dict[str, Any]:
    ap: dict[str, Any] = {
        "mac": DP_AP_MAC,
        "id": DP_AP_ID,
        "type": "ap",
        "model": "AP45",
    }
    if with_profile:
        ap["deviceprofile_id"] = "ap-profile-1"
    return ap


def _dp_site_doc(*, gw_profiled: bool, ap_profiled: bool) -> dict[str, Any]:
    return {
        "redaction_version": 6,
        "scope": {"org_id": DP_ORG_ID, "site_id": DP_SITE},
        "meta": {
            "acquired_at": "2026-06-15T00:00:00+00:00",
            "host": "api.mist.com",
            "fetched": [
                "site", "setting", "networktemplate", "devices",
                "device_stats", "port_stats", "wireless_clients",
                "wired_clients", "org_networks",
            ],
            "failures": [],
        },
        "site": {
            "id": DP_SITE,
            "org_id": DP_ORG_ID,
            "networktemplate_id": None,
            "gatewaytemplate_id": DP_GT_ID,
            "sitetemplate_id": None,
        },
        "setting": {
            "networks": {DP_NET: {"vlan_id": DP_VLAN, "subnet": "198.51.97.0/24",
                                  "gateway": "198.51.97.1"}},
            "port_usages": {},
        },
        "networktemplate": None,
        "gatewaytemplate": {
            "id": DP_GT_ID,
            "name": "dp-template",
            "ip_configs": {DP_NET: {"ip": "198.51.97.1"}},
            "port_config": {"ge-0/0/0": {"networks": [DP_NET]}},
        },
        "sitetemplate": None,
        "derived_setting": None,
        "devices": [_dp_gateway_device(with_profile=gw_profiled),
                    _dp_ap_device(with_profile=ap_profiled)],
        "device_stats": [],
        "port_stats": [],
        "wireless_clients": [],
        "wired_clients": [],
        "wlans": [],
        "org_networks": [{"name": DP_NET, "vlan_id": DP_VLAN, "subnet": "198.51.97.0/24"}],
    }


def _dp_gt_template() -> dict[str, Any]:
    return {
        "id": DP_GT_ID,
        "name": "dp-template",
        "ip_configs": {DP_NET: {"ip": "198.51.97.1"}},
        "port_config": {"ge-0/0/0": {"networks": [DP_NET]}},
    }


def dp_gatewaytemplate_edit_with_profiled_gw() -> tuple[dict[str, Any], dict[str, Any]]:
    """DP-a: the gatewaytemplate changes ip_configs.dp_net.ip on a site whose
    gateway carries a deviceprofile_id -> device_profile_gate fires -> UNKNOWN."""
    site_doc = _dp_site_doc(gw_profiled=True, ap_profiled=False)
    doc = {
        "templates": {"gatewaytemplate": {DP_GT_ID: _dp_gt_template()}},
        "sites": {DP_SITE: site_doc},
        "fetch_failures": [],
    }
    plan = {
        "source": "mist",
        "scope": {"org_id": DP_ORG_ID},
        "ops": [{
            "action": "update", "order": 0, "object_type": "gatewaytemplate",
            "object_id": DP_GT_ID,
            "payload": {"ip_configs": {DP_NET: {"ip": "198.51.97.2"}}},
        }],
    }
    return doc, plan


def dp_only_ap_profiled_not_tainted() -> tuple[dict[str, Any], dict[str, Any]]:
    """DP-b: ONLY the AP carries a deviceprofile_id (gateways do not). The
    gatewaytemplate changes ip_configs.dp_net.ip — APs are ignored by the
    device-profile gate -> the gate does NOT fire -> gets a real verdict
    (gateway_gap.gateway_unowned -> UNSAFE, since the known gateway owner
    changes)."""
    site_doc = _dp_site_doc(gw_profiled=False, ap_profiled=True)
    doc = {
        "templates": {"gatewaytemplate": {DP_GT_ID: _dp_gt_template()}},
        "sites": {DP_SITE: site_doc},
        "fetch_failures": [],
    }
    plan = {
        "source": "mist",
        "scope": {"org_id": DP_ORG_ID},
        "ops": [{
            "action": "update", "order": 0, "object_type": "gatewaytemplate",
            "object_id": DP_GT_ID,
            "payload": {"ip_configs": {DP_NET: {"ip": "198.51.97.99"}}},
        }],
    }
    return doc, plan


def ospf_op(doc: dict[str, Any], entries: dict[str, dict[str, Any]] | None, *,
            disable: bool = False, order: int = 0) -> dict[str, Any]:
    """A HUB device op whose payload sets ospf to the given state. `entries=None`
    + disable=True flips ospf_config.enabled false; otherwise the payload's
    ospf_areas.0.networks is REPLACED with `entries` (omit a name = withdrawn).

    The payload is MINIMAL (root-level Mist PUT: present roots replace wholesale,
    omitted roots persist) — it carries ONLY the ospf roots so the delta touches
    nothing but OSPF. A full-object HUB payload would reshape unrelated L2 state
    (a fixture artifact) and floor every verdict at REVIEW; this keeps the OSPF
    withdrawal the sole signal under test."""
    hub = _device(doc, HUB)
    payload: dict[str, Any] = {"type": hub.get("type", "switch")}
    if disable:
        payload["ospf_config"] = {"enabled": False}
    else:
        payload["ospf_config"] = {"enabled": True}
        payload["ospf_areas"] = {"0": {"networks": entries or {}}}
    return {
        "action": "update", "order": order, "object_type": "device",
        "object_id": str(hub["id"]), "payload": _drop_nones(payload),
    }
