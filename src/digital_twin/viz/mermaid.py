# src/digital_twin/viz/mermaid.py
"""Render IR topology + a Highlight into mermaid Diagram(s).

`build_diagrams` is PURE and may raise (tests catch render bugs). The pipeline
calls `safe_build_diagrams`, the ONLY place that swallows exceptions (-> ()), so
a render bug never sinks a verdict. v1 highlights NODES only (link/port findings
already mapped to endpoint device nodes by build_highlight); finding labels and
cause attribution go in Diagram.notes (VISIBLE via to_markdown), never as a class
and never as `%%` comments (mermaid does not render those).
"""

from __future__ import annotations

import networkx as nx

from digital_twin.analysis.exits import resolve_exit
from digital_twin.contracts import Diagram, Finding, Severity
from digital_twin.ir import IR
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph

from .highlight import Highlight, Hit, build_highlight

_CLASSDEFS = (
    "  classDef crit fill:#fdd,stroke:#c00,stroke-width:2px;",
    "  classDef warn fill:#fff3cd,stroke:#e0a800;",
    "  classDef info fill:#eef,stroke:#88a;",
)
_SEV_CLASS = {
    Severity.CRITICAL: "crit", Severity.ERROR: "crit",
    Severity.WARNING: "warn", Severity.INFO: "info",
}
_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


def _safe(text: object, cap: int = 120) -> str:
    t = (
        str(text).replace("\n", " ").replace('"', "'").replace("[", "(").replace("]", ")")
        .replace("|", "/").replace("<", "‹").replace(">", "›")
    )
    return t if len(t) <= cap else t[: cap - 1] + "…"


def _label(*parts: object) -> str:
    return "<br/>".join(_safe(p) for p in parts if p is not None and str(p) != "")


class _Ids:
    """Per-chart synthetic node ids (mermaid ids cannot contain : / - .)."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def get(self, key: str) -> str:
        if key not in self._map:
            self._map[key] = f"n{len(self._map)}"
        return self._map[key]


def _worst(*sevs: Severity | None) -> Severity | None:
    present = [s for s in sevs if s is not None]
    return max(present, key=lambda s: _SEV_RANK[s]) if present else None


def _class_lines(ids: _Ids, node_hits: dict[str, Hit]) -> tuple[list[str], list[str]]:
    """(`class nX cls;` lines, human caption strings) for nodes declared on THIS
    chart. Captions go into Diagram.notes (VISIBLE via to_markdown)."""
    classes: list[str] = []
    captions: list[str] = []
    for raw_id, hit in node_hits.items():
        if raw_id not in ids._map:  # node not on this chart
            continue
        classes.append(f"  class {ids.get(raw_id)} {_SEV_CLASS[hit.severity]};")
        for lsev, ltext in hit.labels:
            captions.append(_safe(f"{lsev.value}: {ltext}"))
    return classes, captions


def _l2_diagram(ir: IR, hl: Highlight) -> Diagram:
    g = build_l2_graph(ir)
    ids = _Ids()
    lines = ["graph LR", *_CLASSDEFS]
    for node in g.nodes:
        dev = ir.devices.get(node)
        label = _label(dev.name or node if dev else node, dev.role.value if dev else "?")
        lines.append(f'  {ids.get(node)}["{label}"]')
    for u, v, data in g.edges(data=True):
        edge = data["data"]
        lbl = ",".join(str(x) for x in sorted(edge.vlans)) or edge.kind
        lines.append(f'  {ids.get(u)} ---|"{_safe(lbl)}"| {ids.get(v)}')
    cls, captions = _class_lines(ids, hl.nodes)
    lines += cls
    causes = [_safe(c) for c in hl.causes]
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    sev = _worst(*(h.severity for raw, h in hl.nodes.items() if raw in ids._map))
    return Diagram(view="l2", title="L2 topology", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + causes + unloc))


def _vlan_diagram(ir: IR, l2: nx.MultiGraph, vid: int, hl: Highlight) -> Diagram:
    vc = vc_root_map(ir)
    g = build_vlan_graph(ir, l2, vid)
    ids = _Ids()
    # resolve_exit covers IRB/SVI (is_exit) AND boundary-uplink GATEWAY nodes on a
    # carrying edge; union the owners of any l3intf for the vlan (incl GATEWAY-role
    # interfaces, which resolve_exit rule 1 — IRB/SVI only — does not see).
    exit_nodes = set(resolve_exit(ir, g).nodes)
    for intf in ir.l3intfs:
        if intf.vlan_id == vid:
            exit_nodes.add(node_for(vc, intf.device_id))
    lines = ["graph LR", *_CLASSDEFS]
    for node in sorted(set(g.nodes) | exit_nodes):  # add exit devices absent from the subgraph
        dev = ir.devices.get(node)
        name = (dev.name or node) if dev else node
        if node in exit_nodes:
            lines.append(f'  {ids.get(node)}(["{_label(name, "exit")}"])')
        else:
            lines.append(f'  {ids.get(node)}["{_label(name, dev.role.value if dev else "?")}"]')
    for u, v, _data in sorted(g.edges(data=True), key=lambda e: (min(e[0], e[1]), max(e[0], e[1]))):
        a, b = (u, v) if u <= v else (v, u)
        lines.append(f'  {ids.get(a)} ---|"{_safe(vid)}"| {ids.get(b)}')
    cls, captions = _class_lines(ids, hl.nodes)
    lines += cls
    vhit = hl.vlans.get(vid)
    vname = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
    title = f"VLAN {vid}" + (f' "{vname}"' if vname else "")
    sev = _worst(vhit.severity if vhit else None,
                 *(h.severity for raw, h in hl.nodes.items() if raw in ids._map))
    vlan_caps = [_safe(f"{lsev.value}: {ltext}") for lsev, ltext in vhit.labels] if vhit else []
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    return Diagram(view=f"vlan:{vid}", title=title, severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + vlan_caps + unloc))


def _l3_exits_diagram(ir: IR, hl: Highlight) -> Diagram:
    vc = vc_root_map(ir)
    by_vlan: dict[int, list[L3Intf]] = {}
    for intf in ir.l3intfs:
        if intf.vlan_id is not None:
            by_vlan.setdefault(intf.vlan_id, []).append(intf)
    routed = sorted(set(by_vlan) | {vid for vid, v in ir.vlans.items() if v.subnet is not None})
    ids = _Ids()
    intf_owner: dict[str, str] = {}  # ikey -> owning device node (for highlighting)
    lines = ["graph LR", *_CLASSDEFS]
    for vid in routed:
        name = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
        lines.append(f'  {ids.get(f"vlan:{vid}")}["{_label(f"VLAN {vid}", name)}"]')
        for intf in by_vlan.get(vid, []):
            owner = node_for(vc, intf.device_id)
            dev = ir.devices.get(owner)
            ikey = f"intf:{intf.id}"
            intf_owner[ikey] = owner
            iname = dev.name if dev and dev.name else intf.device_id
            lines.append(f'  {ids.get(ikey)}(["{_label(iname, intf.role.value)}"])')
            lines.append(f'  {ids.get(f"vlan:{vid}")} -->|"served by"| {ids.get(ikey)}')
    # highlight affected VLAN boxes AND interface nodes whose owning device is hit
    classes: list[str] = []
    for vid, hit in hl.vlans.items():
        if f"vlan:{vid}" in ids._map:
            classes.append(f"  class {ids.get(f'vlan:{vid}')} {_SEV_CLASS[hit.severity]};")
    for ikey, owner in intf_owner.items():
        nh = hl.nodes.get(owner)
        if nh is not None:
            classes.append(f"  class {ids.get(ikey)} {_SEV_CLASS[nh.severity]};")
    lines += classes
    owners_hit = sorted({o for o in intf_owner.values() if o in hl.nodes})
    sev = _worst(
        *(h.severity for vid, h in hl.vlans.items() if f"vlan:{vid}" in ids._map),
        *(hl.nodes[o].severity for o in owners_hit),
    )
    vlan_caps = [
        _safe(f"{lsev.value}: {ltext}")
        for vid, hit in hl.vlans.items() if f"vlan:{vid}" in ids._map
        for lsev, ltext in hit.labels
    ]
    intf_caps = [
        _safe(f"{lsev.value}: {ltext}")
        for o in owners_hit for lsev, ltext in hl.nodes[o].labels
    ]
    unloc = [f"{hl.unlocalized} finding(s) not localized"] if hl.unlocalized else []
    return Diagram(view="l3_exits", title="Routed VLAN exits", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(vlan_caps + intf_caps + unloc))


def _vlan_id_of(d: Diagram) -> int:
    return int(d.view.split(":", 1)[1])


def build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    hl = build_highlight(findings, ir)
    l2 = build_l2_graph(ir)
    out: list[Diagram] = [_l2_diagram(ir, hl)]
    vlan_diagrams = [_vlan_diagram(ir, l2, vid, hl) for vid in sorted(ir.vlans)]

    def _order(d: Diagram) -> tuple[bool, int, int]:
        rank = _SEV_RANK[d.severity] if d.severity is not None else -1
        return (d.severity is None, -rank, _vlan_id_of(d))  # affected first, then numeric id

    vlan_diagrams.sort(key=_order)
    out += vlan_diagrams
    out.append(_l3_exits_diagram(ir, hl))
    return tuple(out)


def safe_build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    try:
        return build_diagrams(ir, findings)
    except Exception:  # noqa: BLE001 — diagrams are presentational; never sink a verdict
        return ()
