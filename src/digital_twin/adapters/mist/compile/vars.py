"""{{var}} resolution over string leaves, from site_setting.vars.

Applied AFTER the merge (and after the device overlay). An unresolved variable is
an error carrying the offending paths (feeds the L1 'unresolved template variable'
finding later).
"""

from __future__ import annotations

import re
from typing import Any

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class UnresolvedVars(ValueError):
    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = missing  # var name -> paths where it occurs
        details = "; ".join(f"{var} at {', '.join(paths)}" for var, paths in missing.items())
        super().__init__(f"unresolved vars: {details}")


def resolve_vars(config: Any, variables: dict[str, str]) -> Any:
    missing: dict[str, list[str]] = {}
    resolved = _walk(config, variables, "", missing)
    if missing:
        raise UnresolvedVars(missing)
    return resolved


def _walk(node: Any, variables: dict[str, str], path: str, missing: dict[str, list[str]]) -> Any:
    if isinstance(node, dict):
        return {
            k: _walk(v, variables, f"{path}.{k}" if path else k, missing) for k, v in node.items()
        }
    if isinstance(node, list):
        return [_walk(v, variables, f"{path}[{i}]", missing) for i, v in enumerate(node)]
    if isinstance(node, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in variables:
                missing.setdefault(name, []).append(path)
                return m.group(0)
            return variables[name]

        return _VAR.sub(sub, node)
    return node
