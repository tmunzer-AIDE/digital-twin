"""Provenance + FactMeta: where a fact came from, and its resulting confidence.

The provenance->confidence mapping is the CANONICAL table — the single source of
truth. The axis is authority/corroboration: a device's report about ITSELF (own STP,
own clients) is authoritative HIGH; a single-source claim about a relationship
(one-sided LLDP) is LOW.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .confidence import Confidence, ConfidenceLevel


class Provenance(StrEnum):
    CONFIG = "config"
    DESIGNATED = "designated"
    LLDP_TWO_SIDED = "lldp_two_sided"
    OBSERVED = "observed"
    INFERRED = "inferred"
    LLDP_ONE_SIDED = "lldp_one_sided"


_LEVEL: dict[Provenance, ConfidenceLevel] = {
    Provenance.CONFIG: ConfidenceLevel.HIGH,
    Provenance.DESIGNATED: ConfidenceLevel.HIGH,
    Provenance.LLDP_TWO_SIDED: ConfidenceLevel.HIGH,
    Provenance.OBSERVED: ConfidenceLevel.HIGH,
    Provenance.INFERRED: ConfidenceLevel.MEDIUM,
    Provenance.LLDP_ONE_SIDED: ConfidenceLevel.LOW,
}


@dataclass(frozen=True)
class FactMeta:
    provenance: Provenance
    confidence: Confidence


def fact_meta(provenance: Provenance, reasons: tuple[str, ...] = ()) -> FactMeta:
    return FactMeta(provenance, Confidence(_LEVEL[provenance], reasons))


CONFIG_META = fact_meta(Provenance.CONFIG)
OBSERVED_META = fact_meta(Provenance.OBSERVED)
