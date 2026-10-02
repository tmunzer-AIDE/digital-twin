"""Backend presence is a config fact; authentication success is unverified."""

from __future__ import annotations

from dataclasses import replace

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    IR,
    Capability,
    Confidence,
    ConfidenceLevel,
    DeviceRole,
    IRCapability,
    IRDiff,
)
from digital_twin.ir.entities import requires_auth

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_AUTH_FIELDS = frozenset(
    {
        "authenticator_count",
        "authenticator_unresolved",
        "authenticator_config",
    }
)


def _violations(ctx: CheckContext, ir: IR) -> list[Violation]:
    out = []
    for pid, port in sorted(ir.ports.items()):
        dev = ir.devices[port.device_id]
        if (
            dev.role is not DeviceRole.SWITCH
            or port.disabled
            or not requires_auth(port.auth)
            or dev.authenticator_count != 0
            or dev.authenticator_unresolved
        ):
            continue
        out.append(
            Violation(
                key=(dev.id, pid),
                subject=ObjectRef("device", dev.id),
                affected=(dev.id, pid),
                summary=(
                    f"{pid} requires wired authentication but has no configured "
                    "RADIUS/Mist NAC backend"
                ),
                evidence={"port": pid, "configured_backends": 0},
                caused_by=(
                    *ctx.delta_index.causes("device", [dev.id]),
                    *ctx.delta_index.causes("port", [pid]),
                ),
            )
        )
    return out


class RadiusMissingCheck:
    id = "wired.auth.radius_missing"
    title = "Wired authentication backend missing or changed"
    domain = "wired.auth"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("device") or diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        relevant = {
            m.ref.id
            for m in ctx.diff.modified
            if m.ref.kind == "device" and _AUTH_FIELDS.intersection(m.changed_fields)
        }
        relevant.update(r.id for r in ctx.diff.added if r.kind == "device")
        auth_ports = {
            m.ref.id
            for m in ctx.diff.modified
            if m.ref.kind == "port" and {"auth", "disabled"}.intersection(m.changed_fields)
        } | {r.id for r in ctx.diff.added if r.kind == "port"}
        relevant.update(
            p.device_id for pid, p in ctx.proposed.ir.ports.items() if pid in auth_ports
        )
        notes = []
        for did in sorted(relevant):
            dev = ctx.proposed.ir.devices.get(did)
            if dev is None or dev.role is not DeviceRole.SWITCH:
                continue
            active_auth = any(
                p.device_id == did and not p.disabled and requires_auth(p.auth)
                for p in ctx.proposed.ir.ports.values()
            )
            if active_auth and (dev.authenticator_count is None or dev.authenticator_unresolved):
                notes.append(
                    f"{did}: authentication backend configuration is unresolved/unavailable"
                )
        result = run_delta_lint(
            check_id=self.id,
            base=_violations(ctx, ctx.baseline.ir),
            proposed=_violations(ctx, ctx.proposed.ir),
            coverage=Coverage(
                CoverageState.PARTIAL if notes else CoverageState.COMPLETE, tuple(notes)
            ),
        )
        findings = list(result.findings)
        for did in sorted(ctx.baseline.ir.devices.keys() & ctx.proposed.ir.devices.keys()):
            old, new = ctx.baseline.ir.devices[did], ctx.proposed.ir.devices[did]
            if (
                old.authenticator_config is None
                or new.authenticator_config is None
                or old.authenticator_config == new.authenticator_config
            ):
                continue
            notes.append(
                f"{did}: server reachability, credentials and NAC admission are not verified"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.backend_changed",
                    severity=Severity.WARNING,
                    confidence=_HIGH,
                    message=(
                        f"{did}: authentication backend configuration changed; "
                        "verify admission and reachability"
                    ),
                    subject=ObjectRef("device", did),
                    affected_entities=(did,),
                    evidence={
                        "configured_backends_before": old.authenticator_count,
                        "configured_backends_after": new.authenticator_count,
                    },
                    caused_by=ctx.delta_index.causes("device", [did]),
                )
            )
        return replace(
            result,
            findings=tuple(findings),
            status=status_from_findings(findings),
            coverage=Coverage(
                CoverageState.PARTIAL if notes else CoverageState.COMPLETE, tuple(notes)
            ),
        )
