"""Policy rule for configuration-object display-name updates.

Changing only the top-level ``name`` attribute is network-safe for Mist
configuration objects, except for the security profile/policy families whose
names may participate in policy references.  This rule is intentionally
pre-fetch: it can cover configuration object types that the topology simulator
does not otherwise model, while every non-name field remains default-denied.
"""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.contracts import ChangePlan
from digital_twin.scope.allowlist import CONFIG_POLICY_OBJECT_TYPES

NAME_CHANGE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "secintelprofiles",
        "aamwprofiles",
        "avprofiles",
        "idpprofiles",
        "servicepolicies",
    }
)

@dataclass(frozen=True)
class NameChangeAssessment:
    safe: bool
    object_types: tuple[str, ...]
    reason: str


def _canonical_object_type(object_type: str) -> str:
    """Accept both twin names and Mist bridge names (``org_*``/``site_*``)."""
    normalized = object_type.strip().lower()
    for prefix in ("org_", "site_"):
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
    return normalized


def assess_name_only_change(plan: ChangePlan) -> NameChangeAssessment | None:
    """Classify a pure, non-empty top-level ``name`` update.

    ``None`` means the plan is not exclusively a name change and must continue
    through the normal simulation pipeline.  An excluded family is returned as
    an explicit non-safe assessment so it can never fall through to a future
    model and accidentally receive this policy grant.
    """
    if plan.source != "mist" or not plan.ops:
        return None

    object_types: list[str] = []
    for op in plan.ops:
        if op.object_type in CONFIG_POLICY_OBJECT_TYPES:
            # Explicit per-object policy (for example webhooks always REVIEW and
            # PSKs require usage telemetry) takes precedence over this generic rule.
            return None
        name = op.payload.get("name")
        if (
            op.action != "update"
            or set(op.payload) != {"name"}
            or not isinstance(name, str)
            or not name.strip()
        ):
            return None
        object_types.append(_canonical_object_type(op.object_type))

    normalized_types = tuple(dict.fromkeys(object_types))
    excluded = tuple(t for t in normalized_types if t in NAME_CHANGE_EXCEPTIONS)
    if excluded:
        joined = ", ".join(excluded)
        return NameChangeAssessment(
            safe=False,
            object_types=normalized_types,
            reason=(
                f"name changes for {joined} are excluded from the safe-name rule "
                "and require impact-aware validation"
            ),
        )

    joined = ", ".join(normalized_types)
    return NameChangeAssessment(
        safe=True,
        object_types=normalized_types,
        reason=f"name-only configuration update is safe for: {joined}",
    )
