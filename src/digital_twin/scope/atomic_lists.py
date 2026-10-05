"""Content boundaries for lists that the change walker treats atomically.

Admitting an array's path does not admit arbitrary objects inside that array.
Keep this contract shared by the raw and effective/dependency screens.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DYNAMIC_RULE_KEYS = frozenset({
    "src", "usage", "equals", "equals_any", "expression", "description",
})
RADIUS_SERVER_KEYS = frozenset({
    "host", "secret", "port", "keywrap_enabled", "keywrap_format",
    "keywrap_kek", "keywrap_mack", "require_message_authenticator",
})


def dynamic_rule_valid(rule: Any) -> bool:
    """Accept only the implemented comparison shape, without string coercion."""
    if not isinstance(rule, Mapping) or set(rule) - DYNAMIC_RULE_KEYS:
        return False
    if not all(isinstance(rule.get(k), str) and rule[k] for k in ("src", "usage")):
        return False
    equals, equals_any = rule.get("equals"), rule.get("equals_any")
    if (equals is not None) == (equals_any is not None):
        return False
    if equals is not None and not isinstance(equals, str):
        return False
    if equals_any is not None and (
        not isinstance(equals_any, list) or not equals_any
        or not all(isinstance(v, str) for v in equals_any)
    ):
        return False
    return all(rule.get(k) is None or isinstance(rule[k], str)
               for k in ("expression", "description"))


def atomic_list_issues(tokens: tuple[str, ...], value: Any) -> tuple[tuple[str, ...], ...]:
    """Unsupported element paths; never include configuration values/secrets."""
    if not isinstance(value, list) or tokens[0] == "vars":
        return ()
    issues: list[tuple[str, ...]] = []
    for index, item in enumerate(value):
        path = (*tokens, str(index))
        if tokens[0] == "port_usages" and tokens[-1] == "rules":
            if isinstance(item, Mapping) and set(item) - DYNAMIC_RULE_KEYS:
                issues.extend((*path, key) for key in sorted(set(item) - DYNAMIC_RULE_KEYS))
            elif not dynamic_rule_valid(item):
                issues.append(path)
        elif tokens == ("radius_config", "auth_servers"):
            if not isinstance(item, Mapping):
                issues.append(path)
                continue
            for key, field in item.items():
                if key not in RADIUS_SERVER_KEYS:
                    issues.append((*path, key))
                elif field is not None:
                    valid = (
                        type(field) is bool if key in (
                            "keywrap_enabled", "require_message_authenticator"
                        ) else type(field) in (str, int) if key == "port"
                        else isinstance(field, str)
                    )
                    if not valid:
                        issues.append((*path, key))
        elif not isinstance(item, str):
            # All other admitted arrays are network names, addresses, AP/tag IDs,
            # NAC match values, or next-hop strings. They contain no objects.
            issues.append(path)
    return tuple(issues)
