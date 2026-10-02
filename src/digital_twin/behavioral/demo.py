"""Run with python -m digital_twin.behavioral.demo; uses synthetic input only.

The tiny reference grammar illustrates the compiler seam and a coupled WLAN /
EX change. These records are not raw Mist objects and this compiler does not
model radio, authentication, STP, DHCP, DNS, routing or application health.
"""

from __future__ import annotations

import json
from typing import Any

from .coverage import Coverage, Receipt, paths
from .juniper import ex_egress
from .program import Node, Outcome, Program, Query, Rule
from .snapshot import Batch, ObjectKey, Operation, Record, Snapshot
from .space import Domain, Space
from .twin import Comparison, Compilation, Twin


class ReferenceCompiler:
    """Exact grammar for supplied admission gates, WLAN tags and EX carriage.

    An unchanged extra field is deliberately unconsumed. No blanket receipt is
    issued for unknown records, object roots, or malformed supported fields.
    """

    def compile(self, snapshot: Snapshot) -> Compilation:
        nodes: list[Node] = []
        receipts: list[Receipt] = []
        for record in snapshot.records:
            body = record.body()
            if record.source != "reference" or record.schema_version != "reference-1":
                continue
            site = record.key.site_id
            known: dict[str, Any] = {}
            if record.key.kind == "admission" and type(body.get("enabled")) is bool:
                known["enabled"] = body["enabled"]
                # An explicit gate is supplied. This is not a NAC rule evaluator.
                for location in (r.key.site_id for r in snapshot.records if r.key.kind == "wlan"):
                    nodes.append(
                        Node(
                            f"admission:{location}",
                            (
                                Rule(destinations=(f"wlan:{location}",))
                                if body["enabled"]
                                else Rule(
                                    outcome=Outcome.DROPPED,
                                    reason="supplied admission gate is closed",
                                ),
                            ),
                            (record.key,),
                        )
                    )
            elif record.key.kind == "wlan" and (
                type(body.get("vlan_id")) is int and 1 <= body["vlan_id"] <= 4094
            ):
                known["vlan_id"] = body["vlan_id"]
                nodes.append(
                    Node(
                        f"wlan:{site}",
                        (
                            Rule(
                                writes=(("vlan", Domain.value(body["vlan_id"])),),
                                destinations=(f"ex:{site}",),
                            ),
                        ),
                        (record.key,),
                    )
                )
            elif (
                record.key.kind == "ex_port"
                and isinstance(body.get("tagged_vlans"), list)
                and all(type(v) is int and 1 <= v <= 4094 for v in body["tagged_vlans"])
            ):
                known["tagged_vlans"] = body["tagged_vlans"]
                nodes.append(
                    ex_egress(
                        f"ex:{site}",
                        native_vlan=None,
                        tagged_vlans=tuple(body["tagged_vlans"]),
                        destination=f"carried:{site}",
                        sources=(record.key,),
                    )
                )
                nodes.append(Node(f"carried:{site}", (Rule(outcome=Outcome.DELIVERED),)))
            receipts.extend(
                Receipt.bind(
                    record,
                    path,
                    model="reference-carriage-grammar",
                    evidence=("tests/behavioral/test_twin.py",),
                )
                for path in paths(known)
                if known
            )
        return Compilation(Program(tuple(nodes)), Coverage(tuple(receipts)))


def fixture() -> tuple[Snapshot, tuple[Query, ...]]:
    records = [
        Record.create(
            ObjectKey("demo", "admission", "shared"),
            {"enabled": True},
            source="reference",
            schema_version="reference-1",
        )
    ]
    for site in ("a", "b"):
        for kind, body in (("wlan", {"vlan_id": 10}), ("ex_port", {"tagged_vlans": [10]})):
            records.append(
                Record.create(
                    ObjectKey("demo", kind, kind, site),
                    body,
                    source="reference",
                    schema_version="reference-1",
                )
            )
    queries = tuple(
        Query(
            f"carriage:{site}",
            f"admission:{site}",
            Space.of(control="data"),
            f"carried:{site}",
            population="supplied admitted WLAN cohort",
        )
        for site in ("a", "b")
    )
    return Snapshot("demo", tuple(records)), queries


def scenario() -> Comparison:
    snapshot, queries = fixture()
    wlan = snapshot.get(ObjectKey("demo", "wlan", "wlan", "a"))
    port = snapshot.get(ObjectKey("demo", "ex_port", "ex_port", "a"))
    batch = Batch(
        snapshot.revision,
        (
            Operation.update(wlan, {"vlan_id": 20}),
            Operation.update(port, {"tagged_vlans": [20]}),
        ),
    )
    return Twin.compile(snapshot, ReferenceCompiler()).simulate(
        batch, queries=queries, rollout="mixed"
    )


def main() -> None:
    result = scenario()
    print(
        json.dumps(
            {
                "input": "synthetic reference grammar; not a live Mist organization",
                "property": "VLAN carriage for supplied admitted cohorts",
                "baseline": {e.query_id: e.status.value for e in result.baseline.evaluations},
                "proposed": {e.query_id: e.status.value for e in result.proposed.evaluations},
                "stages": [
                    {
                        "operations": s.operation_indices,
                        "results": {e.query_id: e.status.value for e in s.assessment.evaluations},
                    }
                    for s in result.stages
                ],
                "deployment_gaps": result.deployment_gaps,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
