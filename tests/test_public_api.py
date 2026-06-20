def test_ir_public_api():
    from digital_twin.ir import (
        IR,
        Capability,
        Client,
        Confidence,
        Device,
        FactMeta,
        IRBuilder,
        IRCapability,
        IRDiff,
        Link,
        Port,
        Provenance,
        Vlan,
        access_ports_by_vlan,
        clients_by_ap,
        diff_ir,
        exits_by_vlan,
        fact_meta,
        min_confidence,
        vc_root_map,
    )

    assert IRBuilder().build().ir_version
    assert all(
        callable(f)
        for f in (
            diff_ir,
            min_confidence,
            fact_meta,
            vc_root_map,
            access_ports_by_vlan,
            exits_by_vlan,
            clients_by_ap,
        )
    )
    assert all(
        x is not None
        for x in (
            IR,
            Client,
            Confidence,
            Device,
            FactMeta,
            IRCapability,
            IRDiff,
            Link,
            Port,
            Provenance,
            Vlan,
        )
    )
    cap: Capability = IRCapability.WIRED_L2
    assert cap == "wired.l2"


def test_representations_public_api():
    from digital_twin.representations import (
        L2Edge,
        VlanNode,
        build_l2_graph,
        build_vlan_graph,
        link_carried_vlans,
    )

    assert all(callable(f) for f in (build_l2_graph, build_vlan_graph, link_carried_vlans))
    assert L2Edge is not None and VlanNode is not None


def test_plan2_public_api():
    from digital_twin.adapters.mist import (
        ClientsIngester,
        IngesterRegistry,
        IngestReport,
        LldpIngester,
        SwitchIngester,
        compile_device,
        compile_site,
        merge_only,
    )
    from digital_twin.engine import validate_supply
    from digital_twin.providers import (
        FetchError,
        MistApiProvider,
        OrgScope,
        OrgTemplateContext,
        RawSiteState,
        SiteScope,
        StateProvider,
    )

    assert all(callable(f) for f in (compile_site, compile_device, merge_only, validate_supply))
    assert IngestReport is not None and FetchError is not None
    assert all(
        x is not None
        for x in (
            SwitchIngester,
            LldpIngester,
            ClientsIngester,
            IngesterRegistry,
            MistApiProvider,
            RawSiteState,
            SiteScope,
            OrgScope,
            OrgTemplateContext,
            StateProvider,
        )
    )


def test_plan3_public_api():
    from digital_twin.adapters.base import VendorAdapter
    from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
    from digital_twin.adapters.mist.apply import apply_plan, get_object, replace_object
    from digital_twin.adapters.mist.validate import L0Result, validate_payload
    from digital_twin.contracts import (
        ChangeOp,
        ChangePlan,
        ChangeScope,
        Finding,
        FindingCategory,
        FindingSource,
        Rejection,
        Severity,
    )
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST, RAW_ALLOWLIST
    from digital_twin.scope.derived_gate import check_derived
    from digital_twin.scope.envelope import parse_change_plan
    from digital_twin.scope.field_gate import screen_op
    from digital_twin.scope.object_gate import check_objects

    assert all(
        callable(f)
        for f in (
            parse_change_plan,
            check_objects,
            screen_op,
            check_derived,
            validate_payload,
            apply_plan,
            get_object,
            replace_object,
        )
    )
    assert all(
        x is not None
        for x in (
            VendorAdapter,
            MistAdapter,
            IngestOutcome,
            L0Result,
            ChangeOp,
            ChangePlan,
            ChangeScope,
            Finding,
            FindingCategory,
            FindingSource,
            Rejection,
            Severity,
            EFFECTIVE_ALLOWLIST,
            RAW_ALLOWLIST,
        )
    )


def test_plan4_public_api():
    from digital_twin.analysis.context import AnalysisContext
    from digital_twin.analysis.cycles import Cycle, find_cycles
    from digital_twin.analysis.exits import ExitKind, ExitResolution, resolve_exit
    from digital_twin.analysis.vlan_reachability import VlanComponent
    from digital_twin.checks.base import (
        Check,
        CheckContext,
        CheckResult,
        Coverage,
        CoverageState,
        Status,
    )
    from digital_twin.checks.registry import CheckRegistry
    from digital_twin.checks.wired import ALL_WIRED_CHECKS
    from digital_twin.verdict.decision import Decision, DecisionInputs, decide
    from digital_twin.verdict.verdict import Verdict, assemble

    assert len(ALL_WIRED_CHECKS) == 17
    assert all(callable(f) for f in (find_cycles, resolve_exit, decide, assemble))
    assert all(
        x is not None
        for x in (
            AnalysisContext,
            Cycle,
            ExitKind,
            ExitResolution,
            VlanComponent,
            Check,
            CheckContext,
            CheckResult,
            Coverage,
            CoverageState,
            Status,
            CheckRegistry,
            Decision,
            DecisionInputs,
            Verdict,
        )
    )


def test_plan5_public_api():
    from digital_twin.drivers.cli import main
    from digital_twin.drivers.mcp_server import simulate_change
    from digital_twin.drivers.render import render_human, verdict_to_dict
    from digital_twin.engine.pipeline import simulate, simulate_org_template
    from digital_twin.engine.run_context import RunContext
    from digital_twin.observability.logging import bound_logger
    from digital_twin.observability.replay.redaction import redact
    from digital_twin.observability.replay.store import (
        FixtureProvider,
        ReplayStore,
        load_fixture_raw,
    )
    from digital_twin.observability.trace import Trace
    from digital_twin.verdict.org_verdict import OrgVerdict
    from digital_twin.verdict.state_meta import StateMetaView, build_state_meta

    assert all(
        callable(f)
        for f in (
            simulate,
            simulate_org_template,
            main,
            simulate_change,
            render_human,
            verdict_to_dict,
            bound_logger,
            redact,
            load_fixture_raw,
            build_state_meta,
        )
    )
    assert all(
        x is not None
        for x in (RunContext, Trace, ReplayStore, FixtureProvider, StateMetaView, OrgVerdict)
    )


def test_ospf_withdrawal_is_registered():
    from digital_twin.checks.wired import ALL_WIRED_CHECKS

    assert any(c.id == "wired.l3.ospf_withdrawal" for c in ALL_WIRED_CHECKS)
