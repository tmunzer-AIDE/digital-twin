"""MistAdapter: the thin FACADE — wires validate/, compile/, ingest/, apply/.

No business logic here. ingest() runs the Plan-2 chain (compile_site +
compile_device per switch + ingester registry) and returns BOTH artifacts the
spec requires from compile: the full effective configs (derived gate's input)
and the IR projection (checks' input). ir is None when ingest failed —
IngestReport carries the names; the engine maps that to UNKNOWN.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from digital_twin.adapters.mist.apply import apply_plan
from digital_twin.adapters.mist.compile.switch import compile_device, compile_site
from digital_twin.adapters.mist.ingest.base import IngestContext, Ingester
from digital_twin.adapters.mist.ingest.clients import ClientsIngester
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.registry import IngesterRegistry, IngestReport
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.adapters.mist.ingest.wlan import WlanIngester
from digital_twin.adapters.mist.validate import L0Result, validate_payload
from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.ir import IR, IRBuilder, device_id
from digital_twin.providers.base import RawSiteState

_Json = dict[str, Any]


@dataclass(frozen=True)
class IngestOutcome:
    ir: IR | None  # None when report.ok is False (diagnostic-only builder state)
    site_effective: _Json
    device_effective: dict[str, _Json]
    report: IngestReport


class MistAdapter:
    def __init__(self, ingesters: list[Ingester] | None = None) -> None:
        self._registry = IngesterRegistry(
            ingesters
            if ingesters is not None
            else [SwitchIngester(), LldpIngester(), ClientsIngester(), WlanIngester()]
        )

    def validate(
        self, op: ChangeOp, *, scope_roots: Collection[str] | None = None
    ) -> L0Result:
        return validate_payload(op.object_type, op.payload, scope_roots=scope_roots)

    def ingest(self, raw: RawSiteState) -> IngestOutcome:
        nt = dict(raw.networktemplate) if raw.networktemplate else None
        setting = dict(raw.setting)
        st = dict(raw.sitetemplate) if raw.sitetemplate else None
        site_effective = compile_site(nt, setting, sitetemplate=st)
        device_effective = {
            device_id(str(d["mac"])): compile_device(nt, setting, dict(d), sitetemplate=st)
            for d in raw.devices
            if d.get("type") == "switch" and d.get("mac")
        }
        builder = IRBuilder()
        report = self._registry.run(
            IngestContext(
                raw=raw,
                site_effective=site_effective,
                device_effective=device_effective,
                builder=builder,
            )
        )
        ir = builder.build() if report.ok else None
        return IngestOutcome(
            ir=ir,
            site_effective=site_effective,
            device_effective=device_effective,
            report=report,
        )

    def apply(self, raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection:
        return apply_plan(raw, ops)
