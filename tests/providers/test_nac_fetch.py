from digital_twin.contracts import FindingCategory, FindingSource, Severity
from digital_twin.providers.base import NacFetch


def test_nacfetch_shape():
    nf = NacFetch(rules=({"id": "r1"},), tags=({"id": "t1"},), tag_findings=())
    assert nf.rules[0]["id"] == "r1" and nf.tags[0]["id"] == "t1"


def test_tag_finding_shape_floors_review():
    # the pinned shape the Mist impl must emit on a nactags-only failure
    from digital_twin.providers.mist_api import _nactag_fetch_finding
    f = _nactag_fetch_finding("boom")
    assert (f.source is FindingSource.ADAPTER and f.category is FindingCategory.OPERATIONAL
            and f.severity is Severity.WARNING)
