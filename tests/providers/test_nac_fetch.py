import json

from digital_twin.contracts import FindingCategory, FindingSource, Severity
from digital_twin.observability.replay.store import FixtureProvider, ReplayStore
from digital_twin.providers.base import FetchError, NacFetch, OrgScope
from tests.adapters.mist.fixtures import raw_site


def test_nacfetch_shape():
    nf = NacFetch(rules=({"id": "r1"},), tags=({"id": "t1"},), tag_findings=())
    assert nf.rules[0]["id"] == "r1" and nf.tags[0]["id"] == "t1"


def test_tag_finding_shape_floors_review():
    # the pinned shape the Mist impl must emit on a nactags-only failure
    from digital_twin.providers.mist_api import _nactag_fetch_finding
    f = _nactag_fetch_finding("boom")
    assert (f.source is FindingSource.ADAPTER and f.category is FindingCategory.OPERATIONAL
            and f.severity is Severity.WARNING)


# ---------------------------------------------------------------------------
# FixtureProvider.resolve_org_nac
# ---------------------------------------------------------------------------


def _nac_fixture(tmp_path, nac_section):
    """Write a single-site fixture JSON with an optional 'nac' key and return the path.

    nac_section=None  → no 'nac' key in the doc (absent)
    nac_section={}    → present-but-empty
    nac_section={...} → present with real content
    """
    store = ReplayStore(tmp_path)
    path = store.save_raw("run", raw_site())
    data = json.loads(path.read_text())
    if nac_section is not None:
        data["nac"] = nac_section
    path.write_text(json.dumps(data))
    return path


def test_fixture_provider_nac_wrong_org_is_fetch_error(tmp_path):
    """Requesting a different org than the fixture holds → FetchError."""
    path = _nac_fixture(tmp_path, {"rules": [{"id": "r1"}], "tags": []})
    provider = FixtureProvider(path)
    # raw_site() uses org_id="o1"; request a different org
    result = provider.resolve_org_nac(OrgScope("wrong-org"))
    assert isinstance(result, FetchError)
    assert any("wrong-org" in f.error or "not the requested" in f.error for f in result.failures)


def test_fixture_provider_nac_with_rules_and_tags(tmp_path):
    """A fixture with a 'nac' section (rules + tags) → NacFetch with those payloads."""
    nac = {"rules": [{"id": "r1", "name": "rule-one"}], "tags": [{"id": "t1"}]}
    path = _nac_fixture(tmp_path, nac)
    provider = FixtureProvider(path)
    result = provider.resolve_org_nac(OrgScope("o1"))
    assert isinstance(result, NacFetch)
    assert result.rules == ({"id": "r1", "name": "rule-one"},)
    assert result.tags == ({"id": "t1"},)
    assert result.tag_findings == ()


def test_fixture_provider_nac_present_but_empty_is_not_fetch_error(tmp_path):
    """Regression: a present-but-empty 'nac': {} must yield NacFetch(rules=(), tags=()),
    NOT a FetchError. The old 'if not nac:' check wrongly treated {} as absent."""
    path = _nac_fixture(tmp_path, {})
    provider = FixtureProvider(path)
    result = provider.resolve_org_nac(OrgScope("o1"))
    assert isinstance(result, NacFetch), f"expected NacFetch, got {result!r}"
    assert result.rules == ()
    assert result.tags == ()


def test_fixture_provider_nac_absent_is_fetch_error(tmp_path):
    """A fixture with no 'nac' key at all → FetchError (section absent)."""
    path = _nac_fixture(tmp_path, None)  # no 'nac' key
    provider = FixtureProvider(path)
    result = provider.resolve_org_nac(OrgScope("o1"))
    assert isinstance(result, FetchError)
    assert any("nac" in f.error for f in result.failures)
