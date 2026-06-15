"""MCP driver: one tool, simulate_change(change_plan) -> verdict JSON.

The tool itself NEVER throws to the agent (spec) — any internal error becomes
an UNKNOWN verdict document with the error in decision_reasons.
For ORG (networktemplate) plans the tool auto-detects the mode and returns an
OrgVerdict dict; for SITE plans it returns the standard Verdict dict.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from digital_twin.contracts import Rejection
from digital_twin.drivers.cli import _is_org_plan
from digital_twin.drivers.render import org_verdict_to_dict, verdict_to_dict
from digital_twin.engine.pipeline import simulate, simulate_org_template
from digital_twin.ir import IRDiff
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import StateProvider
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.org_verdict import OrgVerdict
from digital_twin.verdict.verdict import assemble

mcp = FastMCP("digital-twin")


def _provider(replay_fixture: str | None) -> StateProvider:
    if replay_fixture:
        return FixtureProvider(replay_fixture)
    from digital_twin.providers.mist_api import MistApiProvider

    return MistApiProvider()


def _unknown_org_dict(reason: str, template_id: str = "") -> dict[str, Any]:
    """Build a well-formed UNKNOWN OrgVerdict dict for the MCP error envelope."""
    ov = OrgVerdict(
        decision=Decision.UNKNOWN,
        decision_reasons=(reason,),
        template_id=template_id,
        per_site={},
        driving_sites=(),
        site_failures={},
        template_findings=(),
        org_rejections=(Rejection(stage="driver", reasons=(reason,)),),
    )
    return org_verdict_to_dict(ov)


def simulate_change(
    change_plan: dict[str, Any],
    replay_fixture: str | None = None,
    l0_full_object: bool = False,
) -> dict[str, Any]:
    if _is_org_plan(change_plan):
        try:
            org_verdict = simulate_org_template(
                change_plan, provider=_provider(replay_fixture), l0_full_object=l0_full_object
            )
            return org_verdict_to_dict(org_verdict)
        except Exception as e:  # noqa: BLE001 — the tool never throws to the agent
            return _unknown_org_dict(f"internal error: {e}")

    try:
        verdict = simulate(
            change_plan, provider=_provider(replay_fixture), l0_full_object=l0_full_object
        )
        return verdict_to_dict(verdict)
    except Exception as e:  # noqa: BLE001 — the tool never throws to the agent
        # a REAL assembled UNKNOWN Verdict: agents get the identical document
        # shape on the error path, exactly when they most need predictable fields
        verdict = assemble(
            inputs=DecisionInputs(
                rejections=(Rejection(stage="driver", reasons=(f"internal error: {e}",)),),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=(),
            ),
            ir_diff=IRDiff((), (), ()),
        )
        return verdict_to_dict(verdict)


@mcp.tool()
def simulate_change_tool(
    change_plan: dict[str, Any], l0_full_object: bool = False
) -> dict[str, Any]:
    """Simulate a Mist ChangePlan against the live network state; returns the
    verdict document (decision: safe|review|unsafe|unknown + findings).
    For org/template plans (ops all have object_type 'networktemplate' and no
    site_id in scope), returns an OrgVerdict document with per-site rollup.
    Payloads follow Mist update semantics: root attributes present in the
    payload replace the current values wholesale, omitted roots persist, and
    {"-attribute": ""} deletes an attribute.
    L0 schema validation defaults to the roots the change touches (matching
    Mist's root-level-merge PUT, which never re-validates omitted roots). Set
    l0_full_object=true to validate the whole effective object instead — useful
    for auditing persisted config, at the cost of surfacing stale-OAS noise on
    roots the change did not touch."""
    return simulate_change(change_plan, l0_full_object=l0_full_object)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
