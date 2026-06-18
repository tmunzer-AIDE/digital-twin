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

from digital_twin.contracts import Diagram, Finding, Severity
from digital_twin.ir import IR
from digital_twin.representations.l2_graph import build_l2_graph

from .highlight import Hit, Highlight, build_highlight

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


def build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    hl = build_highlight(findings, ir)
    return (_l2_diagram(ir, hl),)  # per-VLAN + L3 added in Tasks 7-8


def safe_build_diagrams(ir: IR, findings: tuple[Finding, ...]) -> tuple[Diagram, ...]:
    try:
        return build_diagrams(ir, findings)
    except Exception:  # noqa: BLE001 — diagrams are presentational; never sink a verdict
        return ()
