from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.drivers.render import _finding_line
from digital_twin.ir import Confidence, ConfidenceLevel


def _f(caused_by):
    return Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="wired.l2.vlan_segmentation.split",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="vlan 7 partitioned",
        subject=ObjectRef("vlan", "7"),
        caused_by=caused_by,
    )


def _cause(kind, id_, name=None, fields=()):
    return Cause(ref=ObjectRef(kind, id_, name=name), fields=fields)


def test_single_cause_clause():
    c = _cause("port", "dev1:mge-0/0/0", name="mge-0/0/0", fields=("native_vlan",))
    line = _finding_line(_f((c,)))
    assert '(caused by port "mge-0/0/0" [native_vlan])' in line


def test_multiple_causes_clause():
    c0 = _cause("port", "dev1:mge-0/0/0", name="mge-0/0/0", fields=("native_vlan",))
    c1 = _cause("port", "dev1:mge-0/0/1", name="mge-0/0/1", fields=("native_vlan",))
    line = _finding_line(_f((c0, c1)))
    assert "caused by" in line and "mge-0/0/0" in line and "mge-0/0/1" in line


def test_no_clause_when_empty():
    assert "caused by" not in _finding_line(_f(()))


def test_cause_without_name_shows_id():
    line = _finding_line(_f((_cause("device", "dev9"),)))
    assert "caused by device dev9" in line


def test_dict_serializes_caused_by():
    from digital_twin.drivers.render import _plain

    c = _cause("port", "p1", name="mge-0/0/0", fields=("native_vlan",))
    d = _plain(_f((c,)))
    assert d["caused_by"][0]["ref"]["name"] == "mge-0/0/0"
    assert d["caused_by"][0]["fields"] == ["native_vlan"]
