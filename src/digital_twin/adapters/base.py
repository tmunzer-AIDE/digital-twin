"""VendorAdapter: the seam a vendor must fill — validate (L0), ingest, apply.

One adapter per vendor; ChangePlan.source selects it. Everything is errors-as-
values: validate returns findings, ingest returns a report (crash-isolated),
apply returns Rejection on bad targets.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState


class VendorAdapter(Protocol):
    def validate(self, op: ChangeOp) -> Any: ...  # vendor L0 result (findings + fatal)

    def ingest(self, raw: RawSiteState) -> Any: ...  # effective configs + IR + report

    def apply(self, raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection: ...
