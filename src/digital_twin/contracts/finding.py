"""Finding: the one result DTO shared by adapter validation (L0) and checks (L2/L3).

Spec: source = adapter|check; category network|operational — NETWORK severity
ERROR/CRITICAL drives UNSAFE, OPERATIONAL never does (it drives REVIEW: the twin
had trouble, which is not evidence the network breaks).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from digital_twin.ir import Confidence


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FindingSource(StrEnum):
    ADAPTER = "adapter"  # L0/L1 payload validation
    CHECK = "check"  # L2/L3 neutral checks


class FindingCategory(StrEnum):
    NETWORK = "network"  # predicted network breakage
    OPERATIONAL = "operational"  # the twin itself had trouble


@dataclass(frozen=True)
class ObjectRef:
    """The headline object a finding is about — what an admin needs to LOCATE it.
    `kind` is the object class (device|vlan|port|link|site_setting|
    networktemplate|dhcp_scope); `id` is the stable identifier; `name` is the
    human label when one is available (renderers fall back to `id` when None).
    The full set of involved entities still lives in `Finding.affected_entities`;
    this is just the single most useful pointer."""

    kind: str
    id: str
    name: str | None = None


@dataclass(frozen=True)
class Finding:
    source: FindingSource
    category: FindingCategory
    code: str  # stable machine code, e.g. "l2.blackhole.vlan_isolated"
    severity: Severity
    confidence: Confidence
    message: str
    affected_entities: tuple[str, ...] = ()  # IR entity ids
    evidence: Mapping[str, Any] = field(default_factory=dict)
    remediation: str | None = None
    subject: ObjectRef | None = None  # the headline object (which device/vlan/...)
