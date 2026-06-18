# tests/viz/test_markdown.py
from digital_twin.contracts import Diagram, Severity
from digital_twin.viz.markdown import to_markdown


def test_to_markdown_wraps_each_diagram():
    d = Diagram(view="l2", title="L2 topology", severity=Severity.ERROR,
                mermaid="graph LR\n  n0[x]", notes=("1 finding(s) not localized",))
    md = to_markdown((d,))
    assert "## L2 topology" in md
    assert "```mermaid" in md and "graph LR" in md
    assert "1 finding(s) not localized" in md  # notes rendered as caption


def test_to_markdown_empty_is_empty_string():
    assert to_markdown(()) == ""
