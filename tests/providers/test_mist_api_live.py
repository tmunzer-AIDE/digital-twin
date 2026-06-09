import os

import pytest

from digital_twin.providers.base import SiteScope

pytestmark = pytest.mark.live

_REQUIRED_ENV = ("MIST_HOST", "MIST_APITOKEN", "DT_GATE_ORG_ID", "DT_GATE_SITE_IDS")
requires_env = pytest.mark.skipif(
    not all(os.environ.get(v) for v in _REQUIRED_ENV),
    reason=f"live env not configured (need {', '.join(_REQUIRED_ENV)})",
)


@requires_env
def test_fetch_site_returns_baseline_and_meta():
    from digital_twin.providers.base import RawSiteState
    from digital_twin.providers.mist_api import MistApiProvider

    org_id = os.environ["DT_GATE_ORG_ID"]
    site_id = os.environ["DT_GATE_SITE_IDS"].split(",")[0]
    raw = MistApiProvider().fetch_site(SiteScope(org_id, site_id))
    assert isinstance(raw, RawSiteState), f"baseline fetch failed: {raw}"
    assert raw.setting is not None
    assert "site" in raw.meta.fetched and "setting" in raw.meta.fetched
