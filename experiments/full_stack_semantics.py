"""Synthetic two-site examples of emergent failures from the same evaluator.

Run: uv run python -m experiments.full_stack_semantics
These are handwritten normalized projections, not imported Mist snapshots.
The firewall and routing nodes are simplified transfers, not SRX/SSR emulators.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from experiments.semantic_twin import Disposition, Node, Ref, Rule, Value, evaluate, replace_nodes


def nac(vlan: int) -> Node:
    return Node(
        "shared-nac",
        (
            Rule(
                matches=(("identity", "employee"),),
                writes=(("vlan", vlan), ("role", "employee")),
                next_nodes=("attachment",),
            ),
            Rule(terminal=Disposition.DROPPED, reason="admission denied"),
        ),
    )


def switch(site: str, vlans: tuple[int, ...]) -> Node:
    return Node(
        f"switch:{site}",
        tuple(Rule(matches=(("vlan", vlan),), next_nodes=(f"router:{site}",)) for vlan in vlans),
    )


def wlan(site: str, *, tunnel: bool = False) -> Node:
    return Node(
        f"wlan:{site}",
        (
            Rule(
                matches=(("supports_auth", True),),
                writes=(("forwarding", "edge" if tunnel else "local"),),
                next_nodes=("shared-nac",),
            ),
        ),
    )


def program() -> tuple[Node, ...]:
    """A shared policy controls wired and wireless users at two distinct sites."""
    nodes = [
        nac(10),
        Node(
            "attachment",
            tuple(
                Rule(
                    matches=(("site", site), ("attachment", attachment)),
                    next_nodes=(f"ap:{site}" if attachment == "wifi" else f"switch:{site}",),
                )
                for site in ("a", "b")
                for attachment in ("wifi", "wired")
            ),
        ),
    ]
    for site in ("a", "b"):
        nodes.extend(
            (
                wlan(site),
                Node(
                    f"ap:{site}",
                    (
                        Rule(
                            matches=(("forwarding", "edge"),),
                            writes=(("inner_vlan", Ref("vlan")), ("vlan", 1)),
                            next_nodes=(f"underlay:{site}",),
                        ),
                        Rule(matches=(("forwarding", "local"),), next_nodes=(f"switch:{site}",)),
                    ),
                ),
                Node(
                    f"underlay:{site}", (Rule(matches=(("vlan", 1),), next_nodes=("mist-edge",)),)
                ),
                switch(site, (10,)),
                Node(
                    f"router:{site}",
                    (Rule(matches=(("service", "payroll"),), next_nodes=("hub-firewall",)),),
                ),
            )
        )
    nodes.extend(
        (
            Node(
                "mist-edge",
                (Rule(writes=(("vlan", Ref("inner_vlan")),), next_nodes=("edge-egress",)),),
            ),
            Node("edge-egress", (Rule(matches=(("vlan", 10),), next_nodes=("hub-firewall",)),)),
            Node(
                "hub-firewall",
                (
                    Rule(matches=(("role", "employee"),), next_nodes=("payroll",)),
                    Rule(terminal=Disposition.DROPPED, reason="application policy denied"),
                ),
            ),
            Node("payroll", (Rule(terminal=Disposition.REACHED, reason="service reached"),)),
        )
    )
    return tuple(nodes)


def profile(
    site: str, attachment: str = "wifi", *, supports_auth: bool | None = True
) -> dict[str, Value]:
    return {
        "identity": "employee",
        "site": site,
        "attachment": attachment,
        "supports_auth": supports_auth,
        "service": "payroll",
    }


def run_examples() -> list[dict[str, object]]:
    baseline = program()
    scenarios = (
        ("baseline", (), True),
        ("nac_vlan_change_only", (nac(20),), True),
        ("wired_change_only", (switch("a", (20,)),), True),
        ("batch_repairs_site_a_but_breaks_unchanged_site_b", (nac(20), switch("a", (20,))), True),
        (
            "complete_batch_preserves_both_sites",
            (nac(20), switch("a", (20,)), switch("b", (20,))),
            True,
        ),
        (
            "shared_firewall_blocks_both_sites",
            (
                Node(
                    "hub-firewall",
                    (Rule(terminal=Disposition.DROPPED, reason="application policy denied"),),
                ),
            ),
            True,
        ),
        ("known_incompatible_wireless_client", (), False),
        ("missing_wireless_client_capability", (), None),
        (
            "tunneled_wlan_bypasses_local_client_vlan",
            (wlan("a", tunnel=True), switch("a", (20,))),
            True,
        ),
        (
            "shared_edge_egress_breaks_remote_wifi",
            (wlan("a", tunnel=True), wlan("b", tunnel=True), Node("edge-egress", ())),
            True,
        ),
    )
    report: list[dict[str, object]] = []
    for name, changes, supports_auth in scenarios:
        proposed = replace_nodes(baseline, changes)
        results: dict[str, object] = {}
        for site, attachment in (("a", "wifi"), ("a", "wired"), ("b", "wifi")):
            entry = f"wlan:{site}" if attachment == "wifi" else "shared-nac"
            result = evaluate(
                proposed, entry, profile(site, attachment, supports_auth=supports_auth), "payroll"
            )
            results[f"{site}:{attachment}"] = asdict(result)
        report.append({"scenario": name, "results": results})
    return report


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "scope": "Synthetic concrete transfer model; not a live Mist simulation",
                "scenarios": run_examples(),
            },
            indent=2,
        )
    )
