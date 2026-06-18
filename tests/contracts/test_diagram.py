import dataclasses

import pytest

from digital_twin.contracts import Diagram, Severity


def test_diagram_constructs_with_defaults():
    d = Diagram(view="l2", title="L2 topology", severity=None, mermaid="graph LR")
    assert d.view == "l2" and d.notes == ()


def test_diagram_is_frozen():
    d = Diagram(view="l2", title="t", severity=Severity.ERROR, mermaid="graph LR")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.view = "x"  # type: ignore[misc]
