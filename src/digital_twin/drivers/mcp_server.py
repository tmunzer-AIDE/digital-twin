"""MCP driver: one tool, simulate_change(change_plan) -> verdict JSON.

The tool itself NEVER throws to the agent (spec) — any internal error becomes
an UNKNOWN verdict document with the error in decision_reasons.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from digital_twin.contracts import Rejection
from digital_twin.drivers.render import verdict_to_dict
from digital_twin.engine.pipeline import simulate
from digital_twin.ir import IRDiff
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import StateProvider
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.verdict import assemble

mcp = FastMCP("digital-twin")


def _provider(replay_fixture: str | None) -> StateProvider:
    if replay_fixture:
        return FixtureProvider(replay_fixture)
    from digital_twin.providers.mist_api import MistApiProvider

    return MistApiProvider()


def simulate_change(
    change_plan: dict[str, Any], replay_fixture: str | None = None
) -> dict[str, Any]:
    try:
        verdict = simulate(change_plan, provider=_provider(replay_fixture))
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
def simulate_change_tool(change_plan: dict[str, Any]) -> dict[str, Any]:
    """Simulate a Mist ChangePlan against the live network state; returns the
    verdict document (decision: safe|review|unsafe|unknown + findings).
    Payloads follow Mist update semantics: root attributes present in the
    payload replace the current values wholesale, omitted roots persist, and
    {"-attribute": ""} deletes an attribute."""
    return simulate_change(change_plan)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
