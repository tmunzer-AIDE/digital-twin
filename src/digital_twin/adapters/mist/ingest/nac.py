"""Org NAC rules/tags → IR (GS34). Load-bearing: a row with an id that fails to
parse is minted opaque (kept, keyed by id) so the diff still sees it and shadowing
skips it; only an id-less row is dropped. Both emit a pinned operational finding."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    IR,
    Confidence,
    ConfidenceLevel,
    IRBuilder,
    NacRule,
    NacTag,
)
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS

_Json = Mapping[str, Any]
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
# positive match dims that are plain id/string lists
_LIST_DIMS = ("port_types", "nactags", "site_ids", "sitegroup_ids",
              "family", "mfg", "model", "os_type", "vendor")


def _finding(code: str, message: str, rule_id: str | None) -> Finding:
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code=code,
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=message,
        evidence={"id": rule_id} if rule_id else {},
    )


def _digest(row: _Json) -> str:
    clean = {k: v for k, v in row.items() if k not in IGNORED_RAW_FIELDS}
    blob = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _str_set(value: Any) -> frozenset[str]:
    """A list of STRING ids/values → frozenset[str]. Raises on a non-list OR any
    non-string element. NB: NOT `str(v)` — a dict/list element (e.g. nactags=[{...}])
    must NOT be silently stringified into a clean value that then participates in shadow
    proofs; raise → the row is minted opaque. Mist NAC list fields are all string lists."""
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError("expected a list")
    if not all(isinstance(v, str) for v in value):
        raise ValueError("non-string list element")
    return frozenset(value)


def _dim_set(m: Mapping[str, Any], name: str) -> frozenset[str]:
    """One positive list dimension → frozenset[str]. A present-and-not-None value goes
    through _str_set (which RAISES on a malformed value → the row is minted opaque); an
    absent/null dim → ∅. NB: must test `is not None`, NOT truthiness — a present-but-falsy
    malformed value (port_types=0, nactags="", site_ids=False) must reach _str_set and
    raise, not silently collapse to a clean "any". An empty list [] is a real ∅."""
    v = m.get(name)
    return _str_set(v) if v is not None else frozenset()


def _auth_types(value: Any) -> frozenset[str]:
    """auth_type is a string in the wild; accept str or list[str]. A non-string list
    element is malformed → raise → opaque (never str()-ified into a trusted value)."""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ValueError("non-string auth_type element")
        return frozenset(value)
    raise ValueError("auth_type not a string/list")


def _not_matching(block: Any) -> frozenset[tuple[str, str]]:
    if block is None:
        return frozenset()                       # absent/null = no negative criteria
    if not isinstance(block, Mapping):
        # present-but-non-Mapping ([] / "" / 0) is MALFORMED proof content, not "absent"
        # — raise so the row is minted opaque (never a silent clean rule).
        raise ValueError("not_matching present but not an object")
    out: set[tuple[str, str]] = set()            # an empty dict {} falls through → ∅
    for dim, val in block.items():
        if isinstance(val, list):
            if not all(isinstance(v, str) for v in val):
                raise ValueError("non-string not_matching list element")  # → opaque
            out.update((str(dim), v) for v in val)
        elif isinstance(val, str):
            if val:                              # non-empty string only ("" == absent)
                out.add((str(dim), val))
        elif val is not None:                    # int/bool/dict scalar = malformed → opaque
            raise ValueError("not_matching value not a string/list")
    return frozenset(out)


def _build_rule(row: _Json) -> NacRule:
    """Parse a rule row into a clean NacRule. Raises on any proof-field problem."""
    m = row.get("matching")
    if m is None:
        m = {}                                   # absent/null = no match constraints
    elif not isinstance(m, Mapping):
        # present-but-non-Mapping ([] / "" / 0) is MALFORMED — raise → opaque row, never
        # a silent clean catch-all (`row.get("matching") or {}` would have masked this).
        raise ValueError("matching present but not an object")
    enabled = row.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled not a bool")
    order = row.get("order")
    # order is proof-bearing (shadowing needs A earlier than B): a PRESENT-but-non-int
    # order is a parse failure → opaque path (so the malformed value stays diff-bearing
    # via opaque_digest, not silently collapsed to None==None). A genuinely-ABSENT order
    # is a real "unordered" state → None, not opaque.
    if order is not None and (not isinstance(order, int) or isinstance(order, bool)):
        raise ValueError("order present but not an int")
    pos = {dim: _dim_set(m, dim) for dim in _LIST_DIMS}
    return NacRule(
        id=str(row["id"]),
        name=row.get("name"),
        order=order,
        enabled=enabled,
        action=row.get("action"),
        auth_types=_auth_types(m.get("auth_type")),
        port_types=pos["port_types"],
        match_tags=pos["nactags"],
        site_ids=pos["site_ids"],
        sitegroup_ids=pos["sitegroup_ids"],
        family=pos["family"],
        mfg=pos["mfg"],
        model=pos["model"],
        os_type=pos["os_type"],
        vendor=pos["vendor"],
        not_matching=_not_matching(row.get("not_matching")),
        # is not None (NOT truthiness): a present-but-falsy-malformed apply_tags must reach
        # _str_set and raise → opaque, not collapse to a clean ∅.
        apply_tags=_str_set(row["apply_tags"]) if row.get("apply_tags") is not None
        else frozenset(),
        opaque_digest=None,
    )


def _opaque_rule(row: _Json) -> NacRule:
    """A row with an id but a parse problem: keep it, keyed by id, opaque + best-effort."""
    order = row.get("order")
    enabled = row.get("enabled", True)
    return NacRule(
        id=str(row["id"]),
        name=row.get("name") if isinstance(row.get("name"), str) else None,
        order=int(order) if isinstance(order, int) and not isinstance(order, bool) else None,
        enabled=enabled if isinstance(enabled, bool) else True,
        action=row.get("action") if isinstance(row.get("action"), str) else None,
        opaque_digest=_digest(row),
    )


def build_nac_ir(
    rules: Iterable[_Json], tags: Iterable[_Json]
) -> tuple[IR, tuple[Finding, ...]]:
    builder = IRBuilder()
    findings: list[Finding] = []
    seen: set[str] = set()
    for row in rules:
        raw_id = row.get("id")
        if not raw_id:
            findings.append(_finding("nac.ingest.dropped",
                                     "nacrule row without an id was dropped", None))
            continue
        rid = str(raw_id)
        # Pre-check the id so add_nacrule (below) is GUARANTEED unique and cannot raise
        # IRValidationError — which subclasses ValueError, so catching it as a "parse
        # problem" and re-adding the opaque row would raise AGAIN and escape (crash).
        if rid in seen:
            findings.append(_finding("nac.ingest.duplicate",
                                     f"duplicate nacrule id {rid} — later row dropped", rid))
            continue
        seen.add(rid)
        # ONLY the parse is guarded; insertion is separate and now collision-free.
        try:
            rule = _build_rule(row)
        except (ValueError, KeyError, TypeError):
            rule = _opaque_rule(row)
            findings.append(_finding("nac.ingest.opaque",
                                     f"nacrule {rid} partially unparseable → opaque", rid))
        builder.add_nacrule(rule)
    for trow in tags:
        tid = trow.get("id")
        if not tid:
            continue  # labels-only; a tagless tag is harmless context
        try:
            builder.add_nactag(NacTag(
                id=str(tid), name=trow.get("name"), type=trow.get("type"),
                match=trow.get("match"),
                values=_str_set(trow["values"]) if trow.get("values") else frozenset(),
                match_all=bool(trow.get("match_all", False)),
            ))
        except (ValueError, KeyError, TypeError):
            continue  # nactags are labels-only; skip a malformed one
    return builder.build(), tuple(findings)
