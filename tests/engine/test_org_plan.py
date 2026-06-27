"""simulate_org_plan: multi-op + delete fan-out (the delete-ripple engine).

A delete is modeled as a proposed layer that is ABSENT (OrgOverlay.proposed=None);
the per-site overlay pins each affected site's baseline to the resolved snapshot and
its proposed to None, so the per-site diff is EXACTLY the collapse. EVERY op in the
plan becomes an overlay; the affected sites are recompiled with ALL overlays applied
together (not op-by-op), which is what lets a COMBINED delete produce a finding that
no single delete could (the two-op-collapse proof below).

The provider is a hand-built FakeProvider (cf. tests/providers/test_mist_api.py's
FakeProvider and tests/engine/test_org_pipeline.py): resolve_org_template returns an
OrgTemplateContext (template body + assigned site ids), fetch_sites returns a per-site
RawSiteState map. RawSiteStates are built by hand with a minimal two-switch topology
so the corp VLAN compiles into a real member + IRB exit (no heavy real fixture).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from digital_twin.engine.pipeline import simulate_org_plan
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    OrgScope,
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
)
from digital_twin.verdict.decision import Decision

# --- topology -------------------------------------------------------------
# EDGE: corp access port (one wired client) + TWO parallel uplink trunks.
# HUB:  the two uplink trunks land here + the corp IRB exit (other_ip_configs).
# The corp NETWORK and the corp ACCESS usage (the member itself) live in the
# SITE SETTING — neither op deletes them, so the member always exists. trunkA's
# usage lives in networktemplate nt1; trunkB's usage in sitetemplate st1. corp
# rides to its exit over BOTH trunks, so removing ONE leaves redundancy.
EDGE = "aa0000000001"
HUB = "aa0000000002"
WIRED_CLIENT = "ddccbbaa0001"

_SETTING = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {"corp_access": {"mode": "access", "port_network": "corp"}},
}
# nt1 / st1 each carry ONE of the two parallel corp trunks. No "id" leaf: a
# template "id" would fold into site_effective and a delete (id vanishes) would
# trip the derived gate as an out-of-scope leaf — unrelated to the carriage.
_NT = {"port_usages": {"trunkA": {"mode": "trunk", "networks": ["corp"]}}}
_ST = {"port_usages": {"trunkB": {"mode": "trunk", "networks": ["corp"]}}}


def _edge() -> dict[str, Any]:
    return {
        "mac": EDGE, "id": "edge", "type": "switch", "model": "EX4100-48P", "name": "edge",
        "port_config": {
            "ge-0/0/0": {"usage": "corp_access"},
            "ge-0/0/46": {"usage": "trunkA"},
            "ge-0/0/47": {"usage": "trunkB"},
        },
    }


def _hub() -> dict[str, Any]:
    return {
        "mac": HUB, "id": "hub", "type": "switch", "model": "EX4100-48P", "name": "hub",
        "port_config": {"ge-0/0/46": {"usage": "trunkA"}, "ge-0/0/47": {"usage": "trunkB"}},
        "other_ip_configs": {
            "corp": {"type": "static", "ip": "10.0.0.1", "netmask": "255.255.255.0"}
        },
    }


def _port_stats() -> tuple[dict[str, Any], ...]:
    return tuple(
        {"mac": a, "port_id": p, "up": True, "neighbor_mac": b, "neighbor_port_desc": p}
        for p in ("ge-0/0/46", "ge-0/0/47")
        for a, b in ((EDGE, HUB), (HUB, EDGE))
    )


def _meta() -> StateMeta:
    # client fetches succeeded (empty == "no clients") so clients.active is EARNED
    return StateMeta(
        acquired_at=datetime.now(UTC), host="t",
        fetched=("site", "setting", "devices", "wired_clients", "wireless_clients"),
        failures=(),
    )


def _site(
    sid: str, *, networktemplate: dict[str, Any] | None, sitetemplate: dict[str, Any] | None,
    with_client: bool = True,
) -> RawSiteState:
    clients = (
        ({"mac": WIRED_CLIENT, "device_mac": EDGE, "port_id": "ge-0/0/0", "vlan": 10},)
        if with_client else ()
    )
    return RawSiteState(
        scope=SiteScope("o1", sid), site={"id": sid, "networktemplate_id": "nt1"},
        setting=dict(_SETTING), networktemplate=networktemplate, sitetemplate=sitetemplate,
        devices=(_edge(), _hub()), device_stats=(), port_stats=_port_stats(),
        wireless_clients=(), wired_clients=clients, derived_setting=None, meta=_meta(),
    )


class FakeProvider:
    """resolve_org_template -> OrgTemplateContext(body, assigned_ids); fetch_sites ->
    per-site RawSiteState map. `templates` maps object_type -> {id: (body, assigned_ids)};
    a requested id absent from that map resolves to a FetchError (whole-plan UNKNOWN)."""

    def __init__(
        self,
        sites: dict[str, RawSiteState | FetchError],
        templates: dict[str, dict[str, tuple[dict[str, Any], list[str]]]],
    ) -> None:
        self._sites = sites
        self._templates = templates

    def resolve_org_template(
        self, scope: OrgScope, template_id: str, object_type: str
    ) -> OrgTemplateContext | FetchError:
        by_id = self._templates.get(object_type, {})
        if template_id not in by_id:
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object=object_type, error=f"{template_id} not found"),),
                acquired_at=datetime.now(UTC), host="h",
            )
        body, assigned = by_id[template_id]
        return OrgTemplateContext(template=body, assigned_site_ids=tuple(assigned))

    def fetch_sites(
        self, scope: OrgScope, site_ids: Any = None, *, include_derived: bool = False
    ) -> dict[str, RawSiteState | FetchError]:
        ids = list(site_ids) if site_ids is not None else list(self._sites)
        return {sid: self._sites[sid] for sid in ids if sid in self._sites}

    def fetch_site(self, scope: Any, *, include_derived: bool = False) -> RawSiteState:
        raise NotImplementedError  # org path never calls the single-site fetch


def _plan(*ops: dict[str, Any], org_id: str = "o1") -> dict[str, Any]:
    return {"source": "mist", "scope": {"org_id": org_id}, "ops": list(ops)}


def _del(object_type: str, object_id: str, order: int = 0, payload: Any = None) -> dict[str, Any]:
    return {"action": "delete", "order": order, "object_type": object_type,
            "object_id": object_id, "payload": payload if payload is not None else {}}


def _upd(
    object_type: str, object_id: str, payload: dict[str, Any], order: int = 0
) -> dict[str, Any]:
    return {"action": "update", "order": order, "object_type": object_type,
            "object_id": object_id, "payload": payload}


def _has_code(verdict: Any, prefix: str) -> bool:
    return any(f.code.startswith(prefix) for f in verdict.findings)


# --- single networktemplate delete ---------------------------------------

def _single_delete_provider() -> FakeProvider:
    # corp net + access usage + BOTH trunks live in nt1; deleting it collapses corp
    nt = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "corp_access": {"mode": "access", "port_network": "corp"},
            "trunkA": {"mode": "trunk", "networks": ["corp"]},
        },
    }
    site = RawSiteState(
        scope=SiteScope("o1", "sX"), site={"id": "sX", "networktemplate_id": "nt1"},
        setting={"networks": {}, "port_usages": {}}, networktemplate=nt, sitetemplate=None,
        devices=(_edge(), _hub()), device_stats=(), port_stats=_port_stats(),
        wireless_clients=(),
        wired_clients=({"mac": WIRED_CLIENT, "device_mac": EDGE, "port_id": "ge-0/0/0",
                        "vlan": 10},),
        derived_setting=None, meta=_meta(),
    )
    return FakeProvider({"sX": site}, {"networktemplate": {"nt1": (nt, ["sX"])}})


def test_single_networktemplate_delete_recompiles_and_collapses():
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1")), provider=_single_delete_provider()
    )
    # changes names the delete
    assert len(ov.changes) == 1
    c = ov.changes[0]
    assert c.action == "delete" and c.ref.kind == "networktemplate" and c.ref.id == "nt1"
    # the site recompiles with networktemplate ABSENT -> a vlan defined only in the
    # template vanishes -> the corp domain collapses + the wired client is affected
    assert "sX" in ov.per_site
    v = ov.per_site["sX"]
    assert v.decision is not Decision.SAFE
    assert _has_code(v, "wired.l2.vlan_segmentation") or _has_code(v, "wired.l2.blackhole")


# --- 0-site delete --------------------------------------------------------

def test_zero_site_delete_is_safe_with_change_named():
    prov = FakeProvider({}, {"networktemplate": {"nt1": ({}, [])}})  # 0 assigned sites
    ov = simulate_org_plan(_plan(_del("networktemplate", "nt1")), provider=prov)
    assert ov.decision is Decision.SAFE
    assert ov.per_site == {}
    assert len(ov.changes) == 1 and ov.changes[0].action == "delete"
    assert any("no assigned sites" in r for r in ov.decision_reasons)


# --- THE PROOF: two ops, one shared site, combined collapse ----------------
# corp net + access (the MEMBER) live in the site setting (deleted by NEITHER op).
# trunkA carriage in nt1 (op A); trunkB carriage in st1 (op B). corp rides BOTH
# trunks to its IRB exit, so:
#   only-A  -> trunkB still carries corp -> member reaches exit -> SAFE
#   only-B  -> trunkA still carries corp -> SAFE
#   BOTH    -> no trunk carries corp -> member severed from its IRB exit -> finding
# This is the false-SAFE the COMBINED overlay catches that per-op simulation cannot.

def _two_op_provider() -> FakeProvider:
    site = _site("sX", networktemplate=_NT, sitetemplate=_ST)
    return FakeProvider(
        {"sX": site},
        {"networktemplate": {"nt1": (_NT, ["sX"])}, "sitetemplate": {"st1": (_ST, ["sX"])}},
    )


def test_only_op_a_delete_networktemplate_is_safe():
    ov = simulate_org_plan(_plan(_del("networktemplate", "nt1")), provider=_two_op_provider())
    assert ov.decision is Decision.SAFE  # trunkB (st1) still carries corp
    assert not _has_code(ov.per_site["sX"], "wired.l2.blackhole")


def test_only_op_b_delete_sitetemplate_is_safe():
    ov = simulate_org_plan(_plan(_del("sitetemplate", "st1")), provider=_two_op_provider())
    assert ov.decision is Decision.SAFE  # trunkA (nt1) still carries corp
    assert not _has_code(ov.per_site["sX"], "wired.l2.blackhole")


def test_both_deletes_together_collapse_corp_and_name_both_changes():
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", order=0), _del("sitetemplate", "st1", order=1)),
        provider=_two_op_provider(),
    )
    # the COMBINED overlay severs corp from its exit -> a finding no single op produced
    assert ov.decision is Decision.UNSAFE
    v = ov.per_site["sX"]
    assert _has_code(v, "wired.l2.blackhole.exit_lost")
    # changes names BOTH ops
    kinds_ids = {(c.ref.kind, c.ref.id, c.action) for c in ov.changes}
    assert kinds_ids == {
        ("networktemplate", "nt1", "delete"),
        ("sitetemplate", "st1", "delete"),
    }


# --- mixed delete + update -------------------------------------------------

def test_mixed_delete_and_update_both_overlays_apply():
    # delete nt1 (drops trunkA) + update st1 to ALSO drop corp from trunkB ->
    # both overlays land on the shared site -> corp severed -> finding. Proves a
    # delete overlay and an update overlay coexist on the same site.
    st_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}  # corp removed
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", order=0),
              _upd("sitetemplate", "st1", st_drop, order=1)),
        provider=_two_op_provider(),
    )
    assert ov.decision is Decision.UNSAFE
    assert _has_code(ov.per_site["sX"], "wired.l2.blackhole.exit_lost")
    actions = {(c.ref.id, c.action) for c in ov.changes}
    assert actions == {("nt1", "delete"), ("st1", "update")}


# --- OD-unknown-names-all --------------------------------------------------

def test_unknown_nonempty_delete_payload_names_all_ops():
    # (a) a two-op plan where one op is a delete with a NON-EMPTY payload: a valid
    # parse, then object_gate rejects -> org UNKNOWN whose `changes` names BOTH ops.
    prov = _two_op_provider()
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", order=0, payload={"networks": {}}),
              _del("sitetemplate", "st1", order=1)),
        provider=prov,
    )
    assert ov.decision is Decision.UNKNOWN
    assert any("delete payload must be empty" in r for r in ov.decision_reasons)
    ids = {(c.ref.kind, c.ref.id) for c in ov.changes}
    assert ids == {("networktemplate", "nt1"), ("sitetemplate", "st1")}
    # named UP FRONT (before resolve) so names are not yet hydrated
    assert all(c.ref.name is None for c in ov.changes)


def test_unknown_second_op_resolve_failure_names_all_ops():
    # (b) a two-op plan where the SECOND op's template fails to resolve -> UNKNOWN
    # whose `changes` names both ops: the first hydrated (resolved name), the
    # second by id (never resolved). st1 is intentionally absent from templates.
    site = _site("sX", networktemplate=_NT, sitetemplate=_ST)
    named_nt = {**_NT, "name": "net-tpl-one"}
    prov = FakeProvider(
        {"sX": site},
        {"networktemplate": {"nt1": (named_nt, ["sX"])}},  # st1 NOT present -> resolve fails
    )
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", order=0), _del("sitetemplate", "st1", order=1)),
        provider=prov,
    )
    assert ov.decision is Decision.UNKNOWN
    assert any("lookup failed" in r for r in ov.decision_reasons)
    by_id = {c.ref.id: c for c in ov.changes}
    assert set(by_id) == {"nt1", "st1"}
    assert by_id["nt1"].ref.name == "net-tpl-one"  # first op hydrated
    assert by_id["st1"].ref.name is None           # second op never resolved -> by id only


# --- site-scoped plan rejected --------------------------------------------

def test_site_scoped_plan_rejected():
    prov = _two_op_provider()
    site_plan = {
        "source": "mist", "scope": {"org_id": "o1", "site_id": "sX"},
        "ops": [{"action": "update", "order": 0, "object_type": "device",
                 "object_id": "d1", "payload": {}}],
    }
    ov = simulate_org_plan(site_plan, provider=prov)
    assert ov.decision is Decision.UNKNOWN
    assert any("simulate" in r.lower() for r in ov.decision_reasons)


def test_org_update_carries_config_diff_on_org_verdict():
    st_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    ov = simulate_org_plan(_plan(_upd("sitetemplate", "st1", st_drop)),
                           provider=_two_op_provider())
    assert ov.decision is not Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds
    assert cds["st1"].object_type == "sitetemplate" and cds["st1"].action == "update"
    by = {c.path: c for c in cds["st1"].changes}
    assert by["port_usages.trunkB.networks"].before == ["corp"]
    assert by["port_usages.trunkB.networks"].after == []


def test_org_delete_lists_removed_leaves():
    ov = simulate_org_plan(_plan(_del("networktemplate", "nt1")),
                           provider=_single_delete_provider())
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "nt1" in cds and cds["nt1"].action == "delete"
    assert {c.kind for c in cds["nt1"].changes} == {"removed"}
    assert "networks.corp.vlan_id" in {c.path for c in cds["nt1"].changes}


def test_org_unknown_drops_config_diffs():
    # PRE-loop check_objects rejection (non-empty delete payload) → UNKNOWN before any
    # diff can be built → config_diffs stays (). This is correct and intentional.
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", payload={"networks": {}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    assert ov.config_diffs == ()


def test_org_field_gate_unknown_carries_config_diff():
    # switch_mgmt is out-of-scope for sitetemplate -> in-loop field-gate UNKNOWN.
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", {"switch_mgmt": {"root_password": "x"}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds and cds["st1"].object_type == "sitetemplate"


def test_org_template_lookup_failed_keeps_earlier_op_diffs():
    # op0 (st1, in-scope) builds a diff; op1 ("ghost", unknown) -> template-lookup
    # failed IN the loop -> UNKNOWN. op0's diff must survive.
    trunkB_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    ghost_payload = {"port_usages": {"x": {"mode": "access"}}}
    ov = simulate_org_plan(
        _plan(
            _upd("sitetemplate", "st1", trunkB_drop, order=0),
            _upd("networktemplate", "ghost", ghost_payload, order=1),
        ),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds                      # earlier op survived
    assert "ghost" not in cds                # uncomputable op carries nothing


def test_org_final_unknown_carries_config_diff(monkeypatch):
    # Force the POST-loop decision to UNKNOWN so we exercise the (now unconditional)
    # final attach at ~618, not an in-loop reject. decide_org returns
    # (decision, reasons, driving).
    import digital_twin.engine.pipeline as pl
    monkeypatch.setattr(pl, "decide_org", lambda *a, **k: (Decision.UNKNOWN, ("forced",), ()))
    trunkB_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", trunkB_drop)),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds


def test_org_l0_fatal_carries_config_diff(monkeypatch):
    # Force L0 fatal on the org op; the diff is built before validate, so the
    # in-loop L0-fatal early return must carry it.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.adapters.mist.validate import L0Result
    monkeypatch.setattr(
        MistAdapter, "validate", lambda self, op, **k: L0Result(findings=(), fatal=True)
    )
    trunkB_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    ov = simulate_org_plan(
        _plan(_upd("sitetemplate", "st1", trunkB_drop)),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds


def test_org_apply_template_reject_keeps_earlier_op_diff(monkeypatch):
    # op0 (st1) builds its diff; op1 (nt1) is forced to fail apply_template — the
    # step that would compute op1's `after`, so op1 is uncomputable. op0 survives.
    import digital_twin.engine.pipeline as pl
    from digital_twin.contracts import Rejection
    real = pl.apply_template
    calls = {"n": 0}

    def fake(snapshot, payload):
        calls["n"] += 1
        if calls["n"] >= 2:
            return Rejection(stage="apply", reasons=("forced apply_template fail",))
        return real(snapshot, payload)

    monkeypatch.setattr(pl, "apply_template", fake)
    trunkB_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    trunkA_drop = {"port_usages": {"trunkA": {"mode": "trunk", "networks": []}}}
    ov = simulate_org_plan(
        _plan(
            _upd("sitetemplate", "st1", trunkB_drop, order=0),
            _upd("networktemplate", "nt1", trunkA_drop, order=1),
        ),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds        # earlier op survived
    assert "nt1" not in cds    # apply_template failed -> no `after` -> uncomputable
