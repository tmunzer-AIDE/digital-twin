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


class UnsetScope:
    """Sentinel type for `validate(..., unknown_scope_roots=...)`: distinguishes
    "omitted" (the adapter reuses `scope_roots` for the unknown-attribute walker)
    from an explicit None (audit the whole object). See UNSET_SCOPE."""

    __slots__ = ()


UNSET_SCOPE = UnsetScope()


class VendorAdapter(Protocol):
    # scope_roots: restrict L0 (jsonschema) to these top-level roots (None = whole
    # object). The engine passes the change's touched roots so omitted/persisted roots
    # aren't re-validated against the committed OAS (Mist root-level-merge PUT).
    # unknown_scope_roots: restrict the OAS unknown-attribute WALKER to these roots —
    # the roots whose VALUES actually changed (validates the change, not the whole
    # persisted object). Omitted -> reuse scope_roots (back-compat scoped-L0);
    # explicit None -> audit the whole object.
    def validate(
        self,
        op: ChangeOp,
        *,
        scope_roots: Collection[str] | None = None,
        unknown_scope_roots: Collection[str] | None | UnsetScope = UNSET_SCOPE,
    ) -> Any: ...  # vendor L0 result (findings + fatal)

    def ingest(self, raw: RawSiteState) -> Any: ...  # effective configs + IR + report

    def apply(self, raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection: ...
