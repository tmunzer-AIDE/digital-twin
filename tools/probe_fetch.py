"""Live probe: fetch one site and dump every raw payload to .cache/ (git-ignored).

Purpose: PIN the real response shapes and SDK call names the adapter assumes
(port stats LLDP/STP/LAG fields, AP lldp_stat, client fields). Run this FIRST on
a real org; fix providers/mist_api.py helpers and ingest field mappings if any
assumption fails; only then run the equivalence gate.

Usage: uv run python tools/probe_fetch.py <org_id> <site_id>
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from digital_twin.providers.base import FetchError, SiteScope
from digital_twin.providers.mist_api import MistApiProvider

OUT = Path(".cache/probe")


def main() -> None:
    org_id, site_id = sys.argv[1], sys.argv[2]
    raw = MistApiProvider().fetch_site(SiteScope(org_id, site_id), include_derived=True)
    if isinstance(raw, FetchError):
        sys.exit(f"baseline fetch failed: {[f'{f.object}: {f.error}' for f in raw.failures]}")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in (
        "site",
        "setting",
        "networktemplate",
        "devices",
        "device_stats",
        "port_stats",
        "wireless_clients",
        "wired_clients",
        "derived_setting",
    ):
        (OUT / f"{name}.json").write_text(json.dumps(getattr(raw, name), indent=1, default=str))
    (OUT / "meta.json").write_text(json.dumps(asdict(raw.meta), indent=1, default=str))
    print(f"fetched: {raw.meta.fetched}")
    print(f"failures: {[f.object for f in raw.meta.failures]}")
    print(f"dumped to {OUT}/ — inspect port_stats.json for LLDP/STP/LAG field names")


if __name__ == "__main__":
    main()
