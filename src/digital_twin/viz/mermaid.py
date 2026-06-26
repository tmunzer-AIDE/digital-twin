# src/digital_twin/viz/mermaid.py
"""Render IR topology + a VisualMap into mermaid Diagram(s).

`build_diagrams` is PURE and may raise (tests catch render bugs). The pipeline
calls `safe_build_diagrams`, the ONLY place that swallows exceptions (-> ()), so
a render bug never sinks a verdict. v1 highlights NODES only; finding labels and
cause attribution go in Diagram.notes (VISIBLE via to_markdown), never as a class
and never as `%%` comments (mermaid does not render those).

Each chart paints classes by querying the view sub-map from the VisualMap (the
single highlighting mechanism post-Task-8). ORIGIN tier gets a distinct classDef;
AFFECTED tier colors by severity. This ensures a VLAN-scoped finding can never
paint another VLAN's chart (bleed-free).
"""

from __future__ import annotations

import networkx as nx

from digital_twin.analysis.exits import resolve_exit
from digital_twin.contracts import Diagram, Finding, Severity, VisualEntry, VisualMap, VisualTier
from digital_twin.ir import IR
from digital_twin.ir.entities import L3Intf
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.representations.l2_graph import build_l2_graph
from digital_twin.representations.vlan_graph import build_vlan_graph
from digital_twin.viz.visual_map import build_visual_map

_CLASSDEFS = (
    "  classDef crit fill:#fdd,stroke:#c00,stroke-width:2px;",
    "  classDef warn fill:#fff3cd,stroke:#e0a800;",
    "  classDef info fill:#eef,stroke:#88a;",
    "  classDef origin fill:#fff,stroke:#06c,stroke-width:3px,stroke-dasharray:5 3;",
)
_SEV_CLASS = {
    Severity.CRITICAL: "crit", Severity.ERROR: "crit",
    Severity.WARNING: "warn", Severity.INFO: "info",
}
_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}


def _class_for(entry: VisualEntry) -> str:
    return "origin" if entry.tier is VisualTier.ORIGIN else _SEV_CLASS[entry.severity]


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


def _captions_and_causes(
    view_map: dict[str, VisualEntry],
    findings: tuple[Finding, ...],
    ids: _Ids,
    entity_keys_on_chart: set[str],
) -> tuple[list[str], list[str]]:
    """Build (caption strings, cause strings) from findings referenced by entries on this chart.

    - captions: `severity: code: message` for each unique finding index that appears
      in any view entry whose entity is rendered on this chart.
    - causes: `code: caused by kind who [fields]` for each caused_by of those findings.
    """
    seen_finding_indices: set[int] = set()
    # collect finding indices from entries on this chart
    for ekey, entry in view_map.items():
        if ekey not in entity_keys_on_chart:
            continue
        for ref in entry.findings:
            seen_finding_indices.add(ref.index)

    captions: list[str] = []
    causes: list[str] = []
    seen_captions: set[str] = set()
    seen_causes: set[str] = set()

    for idx in sorted(seen_finding_indices):
        if idx >= len(findings):
            continue
        f = findings[idx]
        caption = _safe(f"{f.severity.value}: {f.code}: {f.message}")
        if caption not in seen_captions:
            captions.append(caption)
            seen_captions.add(caption)
        for c in f.caused_by:
            who = c.ref.name or c.ref.id
            flds = f" [{', '.join(c.fields)}]" if c.fields else ""
            cause_str = _safe(f"{f.code}: caused by {c.ref.kind} {who}{flds}")
            if cause_str not in seen_causes:
                causes.append(cause_str)
                seen_causes.add(cause_str)

    return captions, causes


def _class_lines_from_map(
    ids: _Ids,
    view_map: dict[str, VisualEntry],
    get_entity_key: str,
) -> list[str]:
    """Emit `class nX cls;` lines for drawn nodes that appear in view_map."""
    lines = []
    for ekey, entry in view_map.items():
        # only device entries are painted on node-level diagrams this way
        kind, _, raw_id = ekey.partition(":")
        if kind != "device":
            continue
        if raw_id not in ids._map:
            continue
        lines.append(f"  class {ids.get(raw_id)} {_class_for(entry)};")
    return lines


def _entity_keys_on_chart(
    ids: _Ids,
    view_map: dict[str, VisualEntry],
    *,
    include_all_vlans: bool = False,
) -> set[str]:
    """Set of view_map entity keys whose ids are actually drawn on this chart.

    When `include_all_vlans` is True, all vlan entries in the view_map are
    included regardless of whether a vlan box is drawn (needed for vlan-subject
    findings whose VLAN is the subject but has no box node on the chart).
    """
    result: set[str] = set()
    for ekey, _entry in view_map.items():
        kind, _, raw_id = ekey.partition(":")
        if kind == "device" and raw_id in ids._map:
            result.add(ekey)
        elif kind == "vlan" and (include_all_vlans or f"vlan:{raw_id}" in ids._map):
            result.add(ekey)
        elif kind == "intf" and f"intf:{raw_id}" in ids._map:
            result.add(ekey)
    return result


def _l2_diagram(
    ir: IR,
    view_map: dict[str, VisualEntry],
    findings: tuple[Finding, ...],
) -> Diagram:
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
    # paint classes from view_map
    cls_lines = _class_lines_from_map(ids, view_map, "device")
    lines += cls_lines
    entity_keys = _entity_keys_on_chart(ids, view_map)
    captions, causes = _captions_and_causes(view_map, findings, ids, entity_keys)
    # unlocalized: findings whose index appears in NO entry across the whole vmap
    # (computed in build_diagrams and threaded here via unloc_count parameter)
    sev = _worst(*(
        entry.severity for ekey, entry in view_map.items()
        if ekey.partition(":")[0] == "device"
        and ekey.partition(":")[2] in ids._map
    ))
    return Diagram(view="l2", title="L2 topology", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + causes))


def _vlan_diagram(
    ir: IR,
    l2: nx.MultiGraph,
    vid: int,
    view_map: dict[str, VisualEntry],
    findings: tuple[Finding, ...],
) -> Diagram:
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
    # paint device-level classes from view_map (VLAN-scoped: only this view's entries)
    cls_lines = _class_lines_from_map(ids, view_map, "device")
    lines += cls_lines
    # also paint the vlan box entry if present (for l3_exits cross-reference, but
    # on the vlan chart the box isn't drawn as a node — skip it here)
    # include_all_vlans=True: a vlan-subject finding has no drawn box on the vlan
    # chart but its captions MUST still appear (the vlan IS the subject of the chart)
    entity_keys = _entity_keys_on_chart(ids, view_map, include_all_vlans=True)
    captions, causes = _captions_and_causes(view_map, findings, ids, entity_keys)
    vname = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
    title = f"VLAN {vid}" + (f' "{vname}"' if vname else "")
    sev = _worst(*(
        entry.severity for ekey, entry in view_map.items()
        if ekey.partition(":")[2] in ids._map or ekey == f"vlan:{vid}"
        if ekey.partition(":")[0] in ("device", "vlan")
    ))
    return Diagram(view=f"vlan:{vid}", title=title, severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + causes))


def _l3_exits_diagram(
    ir: IR,
    view_map: dict[str, VisualEntry],
    findings: tuple[Finding, ...],
    l2_view_map: dict[str, VisualEntry] | None = None,
) -> Diagram:
    vc = vc_root_map(ir)
    by_vlan: dict[int, list[L3Intf]] = {}
    for intf in ir.l3intfs:
        if intf.vlan_id is not None:
            by_vlan.setdefault(intf.vlan_id, []).append(intf)
    routed = sorted(set(by_vlan) | {vid for vid, v in ir.vlans.items() if v.subnet is not None})
    ids = _Ids()
    intf_id_map: dict[str, str] = {}  # intf.id -> ikey used in ids
    lines = ["graph LR", *_CLASSDEFS]
    for vid in routed:
        name = ir.vlans[vid].name if vid in ir.vlans and ir.vlans[vid].name else None
        lines.append(f'  {ids.get(f"vlan:{vid}")}["{_label(f"VLAN {vid}", name)}"]')
        for intf in by_vlan.get(vid, []):
            owner = node_for(vc, intf.device_id)
            dev = ir.devices.get(owner)
            ikey = f"intf:{intf.id}"
            intf_id_map[intf.id] = ikey
            iname = dev.name if dev and dev.name else intf.device_id
            lines.append(f'  {ids.get(ikey)}(["{_label(iname, intf.role.value)}"])')
            lines.append(f'  {ids.get(f"vlan:{vid}")} -->|"served by"| {ids.get(ikey)}')

    # paint classes from view_map: vlan boxes and intf nodes
    # also use l2_view_map device entries to color intf nodes owned by those devices
    classes: list[str] = []
    # combine device entries from both l3_exits view_map and l2 fallback
    all_device_entries: dict[str, VisualEntry] = {}
    for src_map in (view_map, l2_view_map or {}):
        for ekey, entry in src_map.items():
            kind, _, raw_id = ekey.partition(":")
            if kind == "device" and raw_id not in all_device_entries:
                all_device_entries[raw_id] = entry
            elif kind == "device" and raw_id in all_device_entries:
                # take worst severity
                existing = all_device_entries[raw_id]
                if _SEV_RANK[entry.severity] > _SEV_RANK[existing.severity]:
                    all_device_entries[raw_id] = entry

    for ekey, entry in view_map.items():
        kind, _, raw_id = ekey.partition(":")
        if kind == "vlan":
            vlan_node_key = f"vlan:{raw_id}"
            if vlan_node_key in ids._map:
                classes.append(f"  class {ids.get(vlan_node_key)} {_class_for(entry)};")
        elif kind == "intf":
            intf_node_key = f"intf:{raw_id}"
            if intf_node_key in ids._map:
                classes.append(f"  class {ids.get(intf_node_key)} {_class_for(entry)};")

    # color intf nodes via device hits (from l3_exits view or l2 view fallback)
    for dev_id, dev_entry in all_device_entries.items():
        for intf in ir.l3intfs:
            owner = node_for(vc, intf.device_id)
            if owner == dev_id:
                intf_node_key = f"intf:{intf.id}"
                if intf_node_key in ids._map:
                    classes.append(
                        f"  class {ids.get(intf_node_key)} {_class_for(dev_entry)};"
                    )
    lines += classes

    entity_keys = _entity_keys_on_chart(ids, view_map)
    # also include findings from l2 device entries whose owned intfs are on this chart
    if l2_view_map:
        for ekey, _entry in l2_view_map.items():
            kind, _, raw_id = ekey.partition(":")
            if kind == "device":
                for intf in ir.l3intfs:
                    owner = node_for(vc, intf.device_id)
                    if owner == raw_id and f"intf:{intf.id}" in ids._map:
                        entity_keys.add(ekey)
                        break
        captions, causes = _captions_and_causes(
            {**view_map, **{k: v for k, v in l2_view_map.items() if k in entity_keys}},
            findings, ids, entity_keys,
        )
    else:
        captions, causes = _captions_and_causes(view_map, findings, ids, entity_keys)
    sev = _worst(
        *(entry.severity for ekey, entry in view_map.items() if _l3_entry_on_chart(ekey, ids)),
        *(dev_entry.severity for dev_id, dev_entry in all_device_entries.items()
          if any(
              f"intf:{intf.id}" in ids._map
              for intf in ir.l3intfs if node_for(vc, intf.device_id) == dev_id
          )),
    )
    return Diagram(view="l3_exits", title="Routed VLAN exits", severity=sev,
                   mermaid="\n".join(lines), notes=tuple(captions + causes))


def _l3_entry_on_chart(ekey: str, ids: _Ids) -> bool:
    """True if this entity key corresponds to something drawn on the l3_exits chart."""
    kind, _, raw_id = ekey.partition(":")
    if kind == "vlan" and f"vlan:{raw_id}" in ids._map:
        return True
    if kind == "intf" and f"intf:{raw_id}" in ids._map:
        return True
    return False


def _vlan_id_of(d: Diagram) -> int:
    return int(d.view.split(":", 1)[1])


def build_diagrams(
    baseline_ir: IR, proposed_ir: IR, findings: tuple[Finding, ...]
) -> tuple[Diagram, ...]:
    vmap: VisualMap = build_visual_map(baseline_ir, proposed_ir, findings)

    # compute unlocalized: finding indices that appear in NO entry across the whole vmap
    all_indexed: set[int] = set()
    for view_entries in vmap.values():
        for entry in view_entries.values():
            for ref in entry.findings:
                all_indexed.add(ref.index)
    unlocalized = sum(1 for i in range(len(findings)) if i not in all_indexed)

    def _with_unloc(d: Diagram) -> Diagram:
        if unlocalized:
            from dataclasses import replace
            return replace(d, notes=d.notes + (f"{unlocalized} finding(s) not localized",))
        return d

    l2 = build_l2_graph(proposed_ir)
    out: list[Diagram] = [_with_unloc(_l2_diagram(proposed_ir, vmap.get("l2", {}), findings))]
    vlan_diagrams = [
        _with_unloc(_vlan_diagram(proposed_ir, l2, vid, vmap.get(f"vlan:{vid}", {}), findings))
        for vid in sorted(proposed_ir.vlans)
    ]

    def _order(d: Diagram) -> tuple[bool, int, int]:
        rank = _SEV_RANK[d.severity] if d.severity is not None else -1
        return (d.severity is None, -rank, _vlan_id_of(d))  # affected first, then numeric id

    vlan_diagrams.sort(key=_order)
    out += vlan_diagrams
    out.append(_with_unloc(_l3_exits_diagram(
        proposed_ir, vmap.get("l3_exits", {}), findings,
        l2_view_map=vmap.get("l2", {}),
    )))
    return tuple(out)


def safe_build_diagrams(
    baseline_ir: IR, proposed_ir: IR, findings: tuple[Finding, ...]
) -> tuple[Diagram, ...]:
    try:
        return build_diagrams(baseline_ir, proposed_ir, findings)
    except Exception:  # noqa: BLE001 — diagrams are presentational; never sink a verdict
        return ()
