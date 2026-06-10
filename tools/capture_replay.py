"""Capture ONE redacted replay fixture from the real org (read-only).

Usage: uv run python tools/capture_replay.py <site_id> <out_path>
Env:   MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID
The output is redacted ON WRITE (store contract) and safe to commit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from digital_twin.observability.replay.store import ReplayStore
from digital_twin.providers.base import RawSiteState, SiteScope
from digital_twin.providers.mist_api import MistApiProvider


def main() -> None:
    site_id, out = sys.argv[1], Path(sys.argv[2])
    raw = MistApiProvider().fetch_site(SiteScope(os.environ["DT_GATE_ORG_ID"], site_id))
    if not isinstance(raw, RawSiteState):
        sys.exit(f"fetch failed: {raw}")
    store = ReplayStore(out.parent)
    path = store.save_raw(out.stem, raw)
    print(f"captured (redacted): {path}")


if __name__ == "__main__":
    main()
