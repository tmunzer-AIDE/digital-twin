"""Ingester seam: one domain turns raw/effective Mist data into an IR slice.

Capabilities are EARNED, not declared: produces() states the POTENTIAL supply
(consumed by engine.capability_check for wiring validation), while ingest()
returns what was ACTUALLY produced — gated on fetch success and/or data
presence — and only those reach the IR. A failed fetch can therefore never
masquerade as a populated domain (the no-silent-blind-spot contract).
Adding a domain (gateway/wlan/wan) = adding one Ingester, nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from digital_twin.ir import Capability, IRBuilder
from digital_twin.providers.base import RawSiteState


@dataclass
class IngestContext:
    raw: RawSiteState
    site_effective: dict[str, Any]  # compile_site output
    device_effective: dict[str, dict[str, Any]]  # device_id -> compile_device output
    builder: IRBuilder


class Ingester(Protocol):
    name: str

    def produces(self) -> frozenset[Capability]:
        """POTENTIAL supply — what this ingester can produce when data is present."""
        ...

    def ingest(self, ctx: IngestContext) -> frozenset[Capability]:
        """Populate the IR slice; return the capabilities ACTUALLY earned."""
        ...
