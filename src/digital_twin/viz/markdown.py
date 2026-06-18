"""Assemble Diagrams into one markdown blob (```mermaid blocks) for the UI."""

from __future__ import annotations

from collections.abc import Iterable

from digital_twin.contracts import Diagram


def to_markdown(diagrams: Iterable[Diagram]) -> str:
    blocks: list[str] = []
    for d in diagrams:
        block = [f"## {d.title}", "", "```mermaid", d.mermaid, "```"]
        block += [f"> {n}" for n in d.notes]
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)
