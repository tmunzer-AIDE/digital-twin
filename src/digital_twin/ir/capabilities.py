"""Capability: the unified, namespaced capability vocabulary, plus the M1 constants.

A Capability is a namespaced string (e.g. "wired.l2", later "analysis.reachability").
Producers/consumers across every layer type over `Capability`, so analysis-derived
capabilities slot in later with no type change. IRCapability provides the M1 IR-domain
constants (its members ARE Capabilities, being str-enum values).

Presence flags (was this domain populated at all), NOT quality — quality lives in
per-fact confidence and per-check coverage. New domains ADD capabilities; existing
checks are unaffected.
"""

from __future__ import annotations

from enum import StrEnum

# Unified capability type: a namespaced capability string.
Capability = str


class IRCapability(StrEnum):
    WIRED_L2 = "wired.l2"
    STP_STATE = "stp.state"
    CLIENTS_ACTIVE = "clients.active"
    L3_EXITS = "l3.exits"
