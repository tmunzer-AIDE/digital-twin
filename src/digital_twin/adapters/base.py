"""VendorAdapter: the seam a vendor must fill — validate (L0), ingest, apply.

One adapter per vendor; ChangePlan.source selects it. Everything is errors-as-
values: validate returns findings, ingest returns a report (crash-isolated),
apply returns Rejection on bad targets.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Protocol

from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState


class VendorAdapter(Protocol):
    # scope_roots: restrict L0 to these top-level roots (None = whole object).
    # The engine passes the change's touched roots so omitted/persisted roots
    # aren't re-validated against the committed OAS (Mist root-level-merge PUT).
    def validate(
        self, op: ChangeOp, *, scope_roots: Collection[str] | None = None
    ) -> Any: ...  # vendor L0 result (findings + fatal)

    def ingest(self, raw: RawSiteState) -> Any: ...  # effective configs + IR + report

    def apply(self, raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection: ...
