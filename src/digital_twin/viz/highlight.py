# src/digital_twin/viz/highlight.py
"""Map findings onto graph entities for diagram highlighting.

ADDITIVE: a finding contributes ALL the entities it references — `subject`,
structured `evidence` keys, and `affected_entities` — never short-stopping. The
worst severity wins per entity. `caused_by` is collected as caption text and is
NEVER highlighted (cause != blast radius). Findings that resolve to no graph
entity are counted in `unlocalized`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from digital_twin.contracts import Finding, Severity
from digital_twin.ir import IR
from digital_twin.ir.indexes import node_for, vc_root_map

_SEV_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}
_MIST_DEV_HEAD = "00000000-0000-0000-"


@dataclass
class Hit:
    severity: Severity  # WORST severity touching the entity -> drives its class/color
    labels: list[tuple[Severity, str]] = field(default_factory=list)  # (own severity, text)


@dataclass
class Highlight:
    nodes: dict[str, Hit]  # graph node id (VC-folded device) -> worst hit
    vlans: dict[int, Hit]  # vlan id -> worst hit
    causes: list[str]  # caption lines from caused_by (NOT highlighted)
    unlocalized: int  # findings that resolved to no graph entity


def _mac(device_id: str) -> str:
    # Mist device ids are 00000000-0000-0000-XXXX-<mac>, XXXX a type tag (1000
    # switch/ap, 2000 gateway, ...). Normalize to the trailing segment generally.
    parts = device_id.split("-")
    if len(parts) == 5 and device_id.startswith(_MIST_DEV_HEAD):
        return parts[-1]
    return device_id


def build_highlight(findings: Iterable[Finding], ir: IR) -> Highlight:
    vc = vc_root_map(ir)

    def node(raw_dev_id: str) -> str | None:
        m = _mac(raw_dev_id)
        return node_for(vc, m) if m in ir.devices or node_for(vc, m) in ir.devices else None

    def port_node(pid: str) -> str | None:
        return node(pid.split(":", 1)[0]) if ":" in pid else None

    hl = Highlight(nodes={}, vlans={}, causes=[], unlocalized=0)

    def add_node(nid: str | None, sev: Severity, label: str) -> bool:
        if nid is None:
            return False
        cur = hl.nodes.get(nid)
        if cur is None:
            hl.nodes[nid] = Hit(sev, [(sev, label)])
        else:
            if (sev, label) not in cur.labels:
                cur.labels.append((sev, label))  # keep EACH label's own severity
            if _SEV_RANK[sev] > _SEV_RANK[cur.severity]:
                cur.severity = sev  # class uses the worst
        return True

    def add_vlan(vid: int, sev: Severity, label: str) -> bool:
        cur = hl.vlans.get(vid)
        if cur is None:
            hl.vlans[vid] = Hit(sev, [(sev, label)])
        else:
            if (sev, label) not in cur.labels:
                cur.labels.append((sev, label))
            if _SEV_RANK[sev] > _SEV_RANK[cur.severity]:
                cur.severity = sev
        return True

    for f in findings:
        label = f"{f.code}: {f.message}"
        hit_any = False

        # 1) subject (typed)
        s = f.subject
        if s is not None:
            if s.kind == "device":
                hit_any |= add_node(node(s.id), f.severity, label)
            elif s.kind == "vlan":
                hit_any |= add_vlan(int(s.id), f.severity, label) if s.id.isdigit() else False
            elif s.kind == "port":
                hit_any |= add_node(port_node(s.id), f.severity, label)
            elif s.kind == "link":
                for pid in s.id.split("__"):
                    hit_any |= add_node(port_node(pid), f.severity, label)

        # 2) structured evidence keys
        ev: Any = f.evidence
        for vid in _ints(ev.get("vlan")) + _ints(ev.get("affected_vlans")):
            hit_any |= add_vlan(vid, f.severity, label)
        _node_keys = ("device", "component_nodes", "fragment_nodes",
                      "baseline_root", "proposed_root")
        for did in [d for k in _node_keys for d in _strs(ev.get(k))]:
            hit_any |= add_node(node(did), f.severity, label)
        for pid in _strs(ev.get("port")) + _strs(ev.get("new_member_ports")):
            hit_any |= add_node(port_node(pid), f.severity, label)
        for lid in _strs(ev.get("link")):
            for pid in lid.split("__"):
                hit_any |= add_node(port_node(pid), f.severity, label)
        for imp in ev.get("impacts") or ():
            if isinstance(imp, dict):
                att = imp.get("attachment")
                if isinstance(att, str):
                    hit_any |= add_node(port_node(att) or node(att), f.severity, label)
                if isinstance(imp.get("vlan"), int):
                    hit_any |= add_vlan(imp["vlan"], f.severity, label)

        # 3) affected_entities (untyped) disambiguated against the IR
        for ent in f.affected_entities:
            if ent in ir.devices or node(ent) in ir.devices:
                hit_any |= add_node(node(ent), f.severity, label)
            elif ent.isdigit() and int(ent) in ir.vlans:
                hit_any |= add_vlan(int(ent), f.severity, label)
            elif ent in ir.ports:
                hit_any |= add_node(port_node(ent), f.severity, label)
            elif "__" in ent:
                for pid in ent.split("__"):
                    hit_any |= add_node(port_node(pid), f.severity, label)

        # cause attribution -> caption text, NEVER a highlight
        for c in f.caused_by:
            who = c.ref.name or c.ref.id
            flds = f" [{', '.join(c.fields)}]" if c.fields else ""
            hl.causes.append(f"{f.code}: caused by {c.ref.kind} {who}{flds}")

        if not hit_any:
            hl.unlocalized += 1

    return hl


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
