"""Explain storm-control risks, retaining REVIEW for unknown traffic behavior."""

from __future__ import annotations

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import IR, Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff, Port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _storm(port: Port | None) -> dict[str, str]:
    digest = port.misc.storm_control if port and port.misc else None
    if digest is None:
        return {}
    return dict(part.split("=", 1) for part in digest.split(";") if "=" in part)


def _percentage(values: dict[str, str]) -> int | None:
    raw = values.get("percentage", "80")
    if raw.isdigit() and 1 <= int(raw) <= 100:
        return int(raw)
    return None


def _critical(ir: IR, port: Port | None) -> bool:
    return bool(
        port
        and not port.disabled
        and (
            port.is_uplink
            or port.profile in {"ap", "uplink"}
            or (port.misc and port.misc.inter_switch_link)
            or any(port.id in (link.a_port, link.b_port) for link in ir.links)
        )
    )


class StormControlPolicyCheck:
    id = "wired.port.storm_control_policy"
    title = "Storm-control shutdown and threshold changes"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("link")

    def run(self, ctx: CheckContext) -> CheckResult:
        base, prop = ctx.baseline.ir, ctx.proposed.ir
        findings = []
        changed = False
        for pid, port in sorted(prop.ports.items()):
            previous = base.ports.get(pid)
            old, new = _storm(previous), _storm(port)
            old_digest = previous.misc.storm_control if previous and previous.misc else None
            new_digest = port.misc.storm_control if port.misc else None
            edited = old_digest != new_digest
            changed |= edited
            if new.get("disable_port") == "True" and _critical(prop, port):
                preexisting = old.get("disable_port") == "True" and _critical(base, previous)
                findings.append(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code=f"{self.id}." + ("preexisting" if preexisting else "uplink_shutdown"),
                        severity=Severity.INFO if preexisting else Severity.WARNING,
                        confidence=_HIGH,
                        message=f"{pid}: storm control can disable a modeled link or uplink port"
                        + (" (pre-existing)" if preexisting else ""),
                        subject=ObjectRef("port", pid),
                        affected_entities=(pid,),
                        evidence={"disable_port": True},
                        caused_by=()
                        if preexisting
                        else tuple(
                            dict.fromkeys(
                                (
                                    *ctx.delta_index.causes("port", [pid]),
                                    *ctx.delta_index.causes(
                                        "link",
                                        sorted(
                                            link.id
                                            for link in prop.links
                                            if pid in (link.a_port, link.b_port)
                                        ),
                                    ),
                                )
                            )
                        ),
                    )
                )
                changed |= not preexisting
            if not edited:
                continue
            old_pct, new_pct = _percentage(old), _percentage(new)
            if old_digest and old_digest.startswith("unresolved:"):
                old_pct = None
            if new_digest and new_digest.startswith("unresolved:"):
                new_pct = None
            if old_pct is not None and new_pct is not None and new_pct < old_pct:
                code, detail = (
                    "threshold_lowered",
                    "lower threshold may drop legitimate burst traffic",
                )
            elif new_digest and (
                new_digest.startswith("unresolved:")
                or new_pct is None
                or any(
                    new.get(k, "False") not in {"True", "False"}
                    for k in (
                        "disable_port",
                        "no_broadcast",
                        "no_multicast",
                        "no_registered_multicast",
                        "no_unknown_unicast",
                    )
                )
            ):
                code, detail = "unresolved", "storm-control values cannot be interpreted"
            else:
                code, detail = (
                    "policy_changed",
                    "storm-control policy changed; verify against traffic bursts",
                )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code}",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=f"{pid}: {detail}",
                    subject=ObjectRef("port", pid),
                    affected_entities=(pid,),
                    evidence={"percentage_before": old_pct, "percentage_after": new_pct},
                    caused_by=ctx.delta_index.causes("port", [pid]),
                )
            )
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            confidence=_HIGH,
            coverage=Coverage(
                CoverageState.PARTIAL if changed else CoverageState.COMPLETE,
                ("traffic burst measurements and platform enforcement are not modeled",)
                if changed
                else (),
            ),
            reasoning="compared storm-control intent and modeled port dependencies",
        )
