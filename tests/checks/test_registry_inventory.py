"""The README's advertised check inventory must track the real registries.

Shadow-contract guard: the README table is hand-written prose, the registry is
code — this test fails the build the moment a check is added/removed without
updating the README (count OR table rows)."""

import re
from pathlib import Path

from digital_twin.checks.nac.delta import NacDeltaCheck
from digital_twin.checks.nac.shadowing import NacShadowingCheck
from digital_twin.checks.wired import ALL_WIRED_CHECKS

_README = Path(__file__).resolve().parents[2] / "README.md"

# the org-NAC checks are wired directly into the pipeline (no list registry);
# this tuple is the NAC-side inventory the README's "<n> NAC checks" refers to
NAC_CHECKS = (NacDeltaCheck(), NacShadowingCheck())


def test_readme_advertised_counts_match_registries():
    text = _README.read_text()

    total = re.search(r"ships \*\*(\d+) checks\*\*", text)
    assert total, "README no longer advertises 'ships **<n> checks**'"

    wired = re.search(r"\*\*(\d+) wired/wireless checks\*\*", text)
    assert wired, "README no longer advertises '**<n> wired/wireless checks**'"
    assert int(wired.group(1)) == len(ALL_WIRED_CHECKS), (
        "README wired/wireless check count is stale vs ALL_WIRED_CHECKS"
    )

    nac = re.search(r"\*\*(\d+) NAC checks\*\*", text)
    assert nac, "README no longer advertises '**<n> NAC checks**'"
    assert int(nac.group(1)) == len(NAC_CHECKS), (
        "README NAC check count is stale vs the pipeline's NAC checks"
    )

    assert int(total.group(1)) == len(ALL_WIRED_CHECKS) + len(NAC_CHECKS), (
        "README total check count is stale vs the registries"
    )


def test_readme_table_lists_every_registered_check():
    text = _README.read_text()
    registered = [c.id for c in (*ALL_WIRED_CHECKS, *NAC_CHECKS)]
    missing = [cid for cid in registered if f"`{cid}`" not in text]
    assert not missing, f"README check table is missing: {missing}"

    # and the table has exactly one numbered row per registered check
    rows = re.findall(r"^\| \d+ \| .+ \| `([\w.]+)` \|", text, flags=re.MULTILINE)
    assert sorted(rows) == sorted(registered), (
        "README check-table rows do not match the registered check ids"
    )
