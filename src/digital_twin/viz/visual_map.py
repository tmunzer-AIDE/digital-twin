"""Pure builder: (baseline_ir, proposed_ir, findings) -> VisualMap.

Keyed per rendered view so a VLAN-scoped finding can never paint another VLAN's
chart. Removed-entity OWNERSHIP resolves against baseline_ir; everything rendered
resolves against proposed_ir. decision.py never reads the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from digital_twin.contracts import (
    Finding,
    FindingRef,
    ObjectRef,
    VisualTier,
)
from digital_twin.ir import IR
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph

_MIST_DEV_HEAD = "00000000-0000-0000-"


@dataclass(frozen=True)
class _Contribution:
    view: str
    kind: str
    id: str
    tier: VisualTier
    ref: FindingRef


def _mac(device_id: str) -> str:
    parts = device_id.split("-")
    if len(parts) == 5 and device_id.startswith(_MIST_DEV_HEAD):
        return parts[-1]
    return device_id


def _node(ir: IR, raw: str) -> str | None:
    """VC-folded device node for `raw`, or None if it is not a device."""
    vc = vc_root_map(ir)
    m = _mac(raw)
    if m in ir.devices or node_for(vc, m) in ir.devices:
        return node_for(vc, m)
    return None


def _port_node(ir: IR, pid: str) -> str | None:
    return _node(ir, pid.split(":", 1)[0]) if ":" in pid else None


def _resolve_affected(ent: str, ir: IR) -> tuple[str, str] | None:
    """(kind, id) for an untyped affected_entities value — ONLY if it resolves in
    the IR. Never promote by string shape (a colon-bearing MAC stays unresolved)."""
    n = _node(ir, ent)
    if n is not None:
        return ("device", n)
    if ent.isdigit() and int(ent) in ir.vlans:
        return ("vlan", ent)
    if ent in ir.ports:
        return ("port", ent)
    return None


def owner_device_nodes(
    kind: str, ent_id: str, baseline_ir: IR, proposed_ir: IR
) -> list[str]:
    """Owner/endpoint device node(s) for a cause. l3intf owner comes from BASELINE
    (it may be removed in proposed). vlan causes own no device -> []."""
    if kind == "device":
        n = _node(proposed_ir, ent_id) or _node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "port":
        n = _port_node(proposed_ir, ent_id) or _port_node(baseline_ir, ent_id)
        return [n] if n else []
    if kind == "link":
        out: list[str] = []
        for pid in ent_id.split("__"):
            n = _port_node(proposed_ir, pid) or _port_node(baseline_ir, pid)
            if n and n not in out:
                out.append(n)
        return out
    if kind == "l3intf":
        # proposed first (added/kept interface), then baseline (removed interface)
        for src in (proposed_ir, baseline_ir):
            for intf in src.l3intfs:
                if intf.id == ent_id:
                    return [node_for(vc_root_map(src), intf.device_id)]
        return []
    return []


@dataclass
class _ViewIndex:
    vlan_nodes: dict[int, set[str]] = field(default_factory=dict)
    routed_vlans: set[int] = field(default_factory=set)
    intfs_by_vlan: dict[int, list[L3Intf]] = field(default_factory=dict)

    def node_in_vlan(self, node: str, vid: int) -> bool:
        return node in self.vlan_nodes.get(vid, set())

    def intfs_for_vlan(self, vid: int) -> list[L3Intf]:
        return self.intfs_by_vlan.get(vid, [])


def _build_view_index(proposed_ir: IR) -> _ViewIndex:
    idx = _ViewIndex()
    l2 = build_l2_graph(proposed_ir)
    for vid in proposed_ir.vlans:
        g: nx.MultiGraph = build_vlan_graph(proposed_ir, l2, vid)
        idx.vlan_nodes[vid] = set(g.nodes)
    for intf in proposed_ir.l3intfs:
        if intf.vlan_id is not None:
            idx.intfs_by_vlan.setdefault(intf.vlan_id, []).append(intf)
    # routed == has a subnet OR is served by an l3 interface (mirrors _l3_exits_diagram)
    idx.routed_vlans = {
        vid for vid, v in proposed_ir.vlans.items() if v.subnet is not None
    } | set(idx.intfs_by_vlan)
    return idx


def _ints(v: Any) -> list[int]:
    if isinstance(v, int):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, int)]
    return []


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str)]
    return []


_NODE_EV_KEYS = (
    "device", "component_nodes", "fragment_nodes", "cycle_nodes",
    "baseline_root", "proposed_root",
)
_PORT_EV_KEYS = ("port", "new_member_ports", "untrusted_egress")  # snooping blocks egress ports
_LINK_EV_KEYS = ("link", "link_ids")  # l2_loop emits the cycle's link_ids


def _ref(f: Finding, index: int) -> FindingRef:
    return FindingRef(index=index, code=f.code, subject=f.subject)


def _affected_contributions(
    f: Finding, index: int, proposed_ir: IR, idx: _ViewIndex
) -> list[_Contribution]:
    ref = _ref(f, index)
    out: list[_Contribution] = []

    def add(view: str, kind: str, ent_id: str) -> None:
        out.append(_Contribution(view, kind, ent_id, VisualTier.AFFECTED, ref))

    # ----- finding-wide scalar references -----
    vlans: set[int] = set()
    nodes: set[str] = set()  # device node ids
    ports: set[str] = set()  # exact port ids (proposed-resolvable)
    links: set[str] = set()  # exact link ids (both endpoints proposed-resolvable)

    def note_port(pid: str) -> None:
        # track the exact port AND its owner device (renderability rule)
        if pid in proposed_ir.ports:
            ports.add(pid)
        n = _port_node(proposed_ir, pid)
        if n:
            nodes.add(n)

    def note_link(lid: str) -> None:
        eps = lid.split("__")
        if len(eps) == 2 and all(p in proposed_ir.ports for p in eps):
            links.add(lid)
        for pid in eps:
            n = _port_node(proposed_ir, pid)
            if n:
                nodes.add(n)

    if f.subject is not None:
        if f.subject.kind == "vlan" and f.subject.id.isdigit():
            vlans.add(int(f.subject.id))
        elif f.subject.kind == "device":
            n = _node(proposed_ir, f.subject.id)
            if n:
                nodes.add(n)
        elif f.subject.kind == "port":
            note_port(f.subject.id)
        elif f.subject.kind == "link":
            note_link(f.subject.id)
    ev: Any = f.evidence
    vlans.update(_ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")))
    for k in _NODE_EV_KEYS:
        for did in _strs(ev.get(k)):
            n = _node(proposed_ir, did)
            if n:
                nodes.add(n)
    for k in _PORT_EV_KEYS:
        for pid in _strs(ev.get(k)):
            note_port(pid)
    for k in _LINK_EV_KEYS:
        for lid in _strs(ev.get(k)):
            note_link(lid)
    for ent in f.affected_entities:
        resolved = _resolve_affected(ent, proposed_ir)
        if resolved is not None:
            rk, rid = resolved
            if rk == "device":
                nodes.add(rid)
            elif rk == "vlan":
                vlans.add(int(rid))
            elif rk == "port":
                note_port(rid)
        elif "__" in ent:
            note_link(ent)  # untyped link id in affected_entities

    # l2: every referenced node + the exact ports/links (so consumers never have
    # to re-infer the precise port/link from the finding)
    for n in nodes:
        add("l2", "device", n)
    for p in ports:
        add("l2", "port", p)
    for lk in links:
        add("l2", "link", lk)
    # vlan:<vid>: nodes that exist in that vlan's graph + the vlan box; l3_exits
    for vid in vlans:
        if vid not in proposed_ir.vlans:
            continue  # no diagram exists for a non-proposed vlan -> no phantom view
        add(f"vlan:{vid}", "vlan", str(vid))
        for n in nodes:
            if idx.node_in_vlan(n, vid):
                add(f"vlan:{vid}", "device", n)
        if vid in idx.routed_vlans:
            add("l3_exits", "vlan", str(vid))
            for intf in idx.intfs_for_vlan(vid):
                owner = _node(proposed_ir, intf.device_id)
                if owner and owner in nodes:  # ONLY interfaces owned by a HIT node
                    add("l3_exits", "intf", intf.id)

    # ----- paired arrays (impacts[]): attachment pairs ONLY with its own vlan -----
    for imp in ev.get("impacts") or ():
        if not isinstance(imp, dict):
            continue
        att = imp.get("attachment")
        ivid = imp.get("vlan")
        att_node = None
        if isinstance(att, str):
            if att in proposed_ir.ports:
                add("l2", "port", att)  # the EXACT impacted port (not just its device)
            att_node = _port_node(proposed_ir, att) or _node(proposed_ir, att)
        if att_node:
            add("l2", "device", att_node)
        if isinstance(ivid, int) and ivid in proposed_ir.vlans:
            add(f"vlan:{ivid}", "vlan", str(ivid))
            if att_node and idx.node_in_vlan(att_node, ivid):
                add(f"vlan:{ivid}", "device", att_node)
    return out


def _origin_owner_views(
    owner: str, vlans: set[int], proposed_ir: IR, idx: _ViewIndex, ref: FindingRef
) -> list[_Contribution]:
    """An owner device projects onto l2 (only if it still EXISTS in proposed —
    a removed device yields no phantom l2 entry, it becomes unlocalized), and onto
    each referenced vlan view only if it still participates in that vlan's proposed
    graph (no phantom nodes)."""
    if _node(proposed_ir, owner) is None:
        return []  # owner device removed -> no phantom node (spec: caption/unlocalized)
    cs = [_Contribution("l2", "device", owner, VisualTier.ORIGIN, ref)]
    for vid in vlans:
        if idx.node_in_vlan(owner, vid):
            cs.append(_Contribution(f"vlan:{vid}", "device", owner, VisualTier.ORIGIN, ref))
    return cs


def _origin_contributions(
    f: Finding, index: int, baseline_ir: IR, proposed_ir: IR, idx: _ViewIndex
) -> list[_Contribution]:
    ref = _ref(f, index)
    out: list[_Contribution] = []

    def emit_cause(cause_ref: ObjectRef, vlans: set[int]) -> None:
        # SELF-entry for a non-device cause, ONLY when it resolves in proposed IR
        # (a removed port/link/intf gets no dangling self-entry — owner device
        # below is the fallback). port/link render on l2; intf on l3_exits.
        if cause_ref.kind == "port" and cause_ref.id in proposed_ir.ports:
            out.append(_Contribution("l2", "port", cause_ref.id, VisualTier.ORIGIN, ref))
        elif cause_ref.kind == "link" and all(
            p in proposed_ir.ports for p in cause_ref.id.split("__")
        ):
            out.append(_Contribution("l2", "link", cause_ref.id, VisualTier.ORIGIN, ref))
        elif cause_ref.kind == "l3intf" and any(
            i.id == cause_ref.id for i in proposed_ir.l3intfs
        ):
            for vid in vlans:
                if vid in idx.routed_vlans:
                    out.append(_Contribution("l3_exits", "intf", cause_ref.id,
                                             VisualTier.ORIGIN, ref))
        # owner device(s) — the renderable fallback; _origin_owner_views gates on
        # proposed existence so a removed device produces no phantom l2 entry.
        for owner in owner_device_nodes(cause_ref.kind, cause_ref.id, baseline_ir, proposed_ir):
            out.extend(_origin_owner_views(owner, vlans, proposed_ir, idx, ref))

    ev: Any = f.evidence
    impacts = ev.get("impacts") or ()
    has_paired = any(isinstance(i, dict) and "caused_by" in i for i in impacts)

    if has_paired:
        # per-impact causes pair with their own vlan (no finding-wide union)
        for imp in impacts:
            if not isinstance(imp, dict):
                continue
            ivid = imp.get("vlan")
            vlans = {ivid} if isinstance(ivid, int) else set()
            for c in imp.get("caused_by") or ():
                if isinstance(c.ref, ObjectRef):
                    emit_cause(c.ref, vlans)
    else:
        # finding-wide caused_by + the finding's referenced vlans
        vlans = set()
        if f.subject is not None and f.subject.kind == "vlan" and f.subject.id.isdigit():
            vlans.add(int(f.subject.id))
        vlans.update(_ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")))
        for c in f.caused_by:
            emit_cause(c.ref, vlans)
    return out
