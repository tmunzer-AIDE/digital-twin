"""Per-object_type targeting: find and replace ONE raw object in RawSiteState.

Replacement is wholesale (Mist PUT semantics) with ONE honesty exception:
server-managed identity fields are preserved from the current object — Mist
ignores attempts to change them, and downstream ingest needs mac/type/model.
RawSiteState is frozen; replacement returns a NEW state (dataclasses.replace).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.providers.base import RawSiteState

_Json = Mapping[str, Any]

# Server-managed: preserved from the current object regardless of the payload.
IDENTITY_FIELDS: tuple[str, ...] = (
    "id",
    "org_id",
    "site_id",
    "mac",
    "serial",
    "model",
    "type",
    "created_time",
    "modified_time",
)


def get_object(raw: RawSiteState, object_type: str, object_id: str) -> _Json | None:
    if object_type == "site_setting":
        return raw.setting if object_id == raw.scope.site_id else None
    if object_type == "device":
        for dev in raw.devices:
            if str(dev.get("id")) == object_id:
                return dev
    return None


def _merged(current: _Json, payload: _Json) -> dict[str, Any]:
    new = dict(payload)
    for key in IDENTITY_FIELDS:
        if key in current:
            new[key] = current[key]
    return new


def replace_object(
    raw: RawSiteState, object_type: str, object_id: str, payload: _Json
) -> RawSiteState:
    """Caller must have resolved the object first (get_object is not None)."""
    if object_type == "site_setting":
        return dc_replace(raw, setting=_merged(raw.setting, payload))
    devices = tuple(
        _merged(dev, payload) if str(dev.get("id")) == object_id else dev for dev in raw.devices
    )
    return dc_replace(raw, devices=devices)
