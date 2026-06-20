"""Pure MAC-keyed enrichment join: wired ∪ wireless (base) + nac (overlay).

OBSERVATIONAL only. Per-row try/except so one malformed row never drops the
batch; the ingester (same module) adds a whole-body backstop so it can never be
fatal. NAC overlays the base per-field, but only when the overlay value is
USEFUL — Mist's literal "Unknown"/empty collapses to None so it cannot clobber a
good base value (e.g. a real OUI manufacturer)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from digital_twin.ir import ClientEnrichment, client_id

_Json = dict[str, Any]
_Extract = Callable[[_Json], dict[str, Any]]  # one source row -> its candidate fields


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" or s.lower() == "unknown" else s


def _first(row: _Json, *keys: str) -> Any:
    """First present value across `keys`; a list value yields its first element."""
    for k in keys:
        v = row.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None:
            return v
    return None


def _wired_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "mfg": row.get("manufacture"),
        "username": _first(row, "last_username", "username"),
        "auth_method": row.get("auth_method"),
        "auth_state": row.get("auth_state"),
    }


def _wireless_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "family": _first(row, "last_family", "family"),
        "mfg": row.get("manufacture"),
        "model": _first(row, "last_model", "model"),
        "os": _first(row, "last_os", "os"),
    }


def _nac_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "family": _first(row, "last_family", "family"),
        "mfg": _first(row, "last_mfg", "mfg"),
        "model": _first(row, "last_model", "model"),
        "os": _first(row, "last_os", "os"),
        "auth_type": row.get("auth_type"),
        "nacrule": _first(row, "last_nacrule_name", "nacrule_name"),
        "status": row.get("last_status"),
        "assigned_vlan": _first(row, "last_vlan", "vlan"),
        "vlan_source": row.get("vlan_source"),
        "username": _first(row, "last_username", "username"),
    }


def _apply(acc: dict[str, dict[str, Any]], row: _Json, extract: _Extract) -> None:
    try:
        mac = row.get("mac")
        if not mac:
            return
        cur = acc.setdefault(client_id(str(mac)), {})
        for key, raw in extract(row).items():
            cleaned = _clean(raw)
            if cleaned is not None:  # non-None overwrites -> processing order = precedence
                cur[key] = cleaned
    except Exception:  # noqa: BLE001 — one malformed row never drops the batch
        return


def build_client_enrichment(
    *, wired: Iterable[_Json], wireless: Iterable[_Json], nac: Iterable[_Json]
) -> dict[str, ClientEnrichment]:
    acc: dict[str, dict[str, Any]] = {}
    for row in wired:
        _apply(acc, row, _wired_vals)
    for row in wireless:
        _apply(acc, row, _wireless_vals)
    for row in nac:  # last -> NAC wins per useful field
        _apply(acc, row, _nac_vals)
    return {mac: ClientEnrichment(**vals) for mac, vals in acc.items() if vals}
