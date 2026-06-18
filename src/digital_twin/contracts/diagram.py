"""Diagram: a dumb DTO for one rendered topology chart (mermaid source).

Pure value type — NO mermaid styling lives here (that is viz/mermaid.py), so
verdict/ can hold a Diagram without importing the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .finding import Severity


@dataclass(frozen=True)
class Diagram:
    view: str  # "l2" | "vlan:<id>" | "l3_exits"
    title: str
    severity: Severity | None  # worst severity highlighted here (ordering); None = nothing
    mermaid: str
    notes: tuple[str, ...] = ()  # captions: cause lines, "N findings not localized"
