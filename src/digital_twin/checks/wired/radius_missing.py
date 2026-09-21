"""wired.auth.radius_missing — assigned authenticated ports need a backend."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, DeviceRole, IRDiff
from digital_twin.ir.entities import requires_auth
from digital_twin.ir.model import IR


def _violations(ir: IR, ctx: CheckContext) -> list[Violation]:
    out: list[Violation] = []
    for device in ir.devices.values():
        if device.role is not DeviceRole.SWITCH:
            continue
        auth_ports = tuple(
            sorted(
                port.id
                for port in ir.ports.values()
                if port.device_id == device.id and requires_auth(port.auth)
            )
        )
        if not auth_ports or device.authenticator_count != 0 or device.authenticator_unresolved:
            continue
        out.append(
            Violation(
                key=(device.id, auth_ports),
                subject=ObjectRef("device", device.id, device.name),
                affected=auth_ports,
                summary=(
                    f"switch {device.name or device.id} assigns 802.1X/MAB to "
                    f"{len(auth_ports)} port(s) but has no RADIUS server or Mist NAC backend"
                ),
                evidence={
                    "device": device.id,
                    "assigned_auth_ports": list(auth_ports),
                    "authenticator_count": 0,
                },
                caused_by=ctx.delta_index.causes("device", [device.id])
                + ctx.delta_index.causes("port", auth_ports),
            )
        )
    return out


class RadiusMissingCheck:
    id = "wired.auth.radius_missing"
    title = "802.1X/MAB configured without RADIUS or Mist NAC"
    domain = "wired.auth"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("device")

    def run(self, ctx: CheckContext) -> CheckResult:
        touched_devices = {
            ref.id
            for ref in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if ref.kind == "device"
        }
        touched_devices.update(
            ref.id.split(":", 1)[0]
            for ref in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if ref.kind == "port"
        )
        notes: list[str] = []
        for device in ctx.proposed.ir.devices.values():
            if device.id not in touched_devices or not device.authenticator_unresolved:
                continue
            if (
                any(
                    port.device_id == device.id and requires_auth(port.auth)
                    for port in ctx.proposed.ir.ports.values()
                )
                and not device.authenticator_count
            ):
                notes.append(
                    f"switch {device.id} has assigned 802.1X/MAB ports but its "
                    "RADIUS/Mist NAC backend state is unresolved"
                )
        return run_delta_lint(
            check_id=self.id,
            base=_violations(ctx.baseline.ir, ctx),
            proposed=_violations(ctx.proposed.ir, ctx),
            coverage=Coverage(
                CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                tuple(notes),
            ),
        )
