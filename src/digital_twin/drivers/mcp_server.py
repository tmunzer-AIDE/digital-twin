"""MCP driver: one tool, simulate_change(change_plan) -> verdict JSON.

The tool itself NEVER throws to the agent (spec) — any internal error becomes
an UNKNOWN verdict document with the error in decision_reasons.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from digital_twin.drivers.render import verdict_to_dict
from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import StateProvider

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
        return {
            "decision": "unknown",
            "decision_reasons": [f"internal error: {e}"],
            "findings": [],
        }


@mcp.tool()
def simulate_change_tool(change_plan: dict[str, Any]) -> dict[str, Any]:
    """Simulate a Mist ChangePlan against the live network state; returns the
    verdict document (decision: safe|review|unsafe|unknown + findings)."""
    return simulate_change(change_plan)


def main() -> None:
    mcp.run()
