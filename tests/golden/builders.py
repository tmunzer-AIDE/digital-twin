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
