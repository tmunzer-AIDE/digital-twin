"""Per-object_type targeting + Mist UPDATE semantics for ONE raw object.

Mist PUT is a ROOT-LEVEL update (confirmed against the real API):
- a root attribute PRESENT in the payload is replaced WHOLESALE;
- a root attribute OMITTED from the payload PERSISTS unchanged;
- deletion is EXPLICIT via a dash marker: {"-attribute_name": ""}.
effective_update() is the single owner of these semantics — apply uses it to
mutate state, and the engine uses it to preview the effective object for the
field gate and L0. Server-managed identity fields are preserved from the
current object regardless (Mist ignores attempts to change them, and
downstream ingest needs mac/type/model). RawSiteState is frozen; replacement
returns a NEW state (dataclasses.replace).
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
    if object_type == "wlan":
        for w in raw.wlans:
            if str(w.get("id")) == object_id:
                return w
        return None
    return None


def update_conflicts(payload: _Json) -> list[str]:
    """Roots both SET and DELETED in one payload — an authoring error."""
    return sorted(k[1:] for k in payload if k.startswith("-") and k[1:] in payload)


def effective_update(current: _Json, payload: _Json) -> dict[str, Any]:
    """The full object Mist would hold after this update (root-level merge +
    dash-marker deletions + identity preservation)."""
    deleted = {k[1:] for k in payload if k.startswith("-")}
    out = {k: v for k, v in current.items() if k not in deleted}
    out.update({k: v for k, v in payload.items() if not k.startswith("-")})
    for key in IDENTITY_FIELDS:
        if key in current:
            out[key] = current[key]
    return out


def replace_object(
    raw: RawSiteState, object_type: str, object_id: str, payload: _Json
) -> RawSiteState:
    """Caller must have resolved the object first (get_object is not None)."""
    if object_type == "site_setting":
        return dc_replace(raw, setting=effective_update(raw.setting, payload))
    if object_type == "wlan":
        wlans = tuple(
            effective_update(w, payload) if str(w.get("id")) == object_id else w
            for w in raw.wlans
        )
        return dc_replace(raw, wlans=wlans)
    devices = tuple(
        effective_update(dev, payload) if str(dev.get("id")) == object_id else dev
        for dev in raw.devices
    )
    return dc_replace(raw, devices=devices)


def delete_object(raw: RawSiteState, object_type: str, object_id: str) -> RawSiteState:
    """Remove an object from raw state. The caller must have resolved it first."""
    if object_type == "wlan":
        wlans = tuple(w for w in raw.wlans if str(w.get("id")) != object_id)
        return dc_replace(raw, wlans=wlans)
    raise ValueError(f"delete is not supported for object_type {object_type!r}")
