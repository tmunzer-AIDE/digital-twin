"""Extract the component schemas the twin needs from the Mist OpenAPI spec.

Usage:
  uv run python tools/extract_oas.py /path/to/mist.openapi.json|yaml

Source spec: https://github.com/mistsys/mist_openapi (official; pin the version
you use in oas/VERSION). Writes small, fully-$ref-resolved JSON schema files into
src/digital_twin/adapters/mist/oas/. Re-run when bumping the OAS version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path("src/digital_twin/adapters/mist/oas")
# OAS component name -> output file. Names VERIFIED against mist.openapi.json
# (2,650 components; a `site_setting_derived` schema also exists if ever needed).
WANTED = {
    "site_setting": "site_setting.schema.json",
    "network_template": "networktemplate.schema.json",
    "device_switch": "device_switch.schema.json",
    "gateway_template": "gatewaytemplate.schema.json",
    "site_template": "sitetemplate.schema.json",
    "nac_rule": "nacrule.schema.json",
    "nac_rule_matching": "nacrule_matching.schema.json",
}


def load_spec(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        return yaml.safe_load(text)  # type: ignore[no-any-return]
    return json.loads(text)  # type: ignore[no-any-return]


def resolve_refs(node: Any, components: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if name in seen:  # break recursion cycles
                return {"type": "object"}
            return resolve_refs(components[name], components, (*seen, name))
        return {k: resolve_refs(v, components, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_refs(v, components, seen) for v in node]
    return node


def main() -> None:
    spec = load_spec(Path(sys.argv[1]))
    components: dict[str, Any] = spec["components"]["schemas"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, filename in WANTED.items():
        if name not in components:
            close = [c for c in components if name.replace("_", "") in c.lower().replace("_", "")]
            sys.exit(f"schema {name!r} not found; candidates: {close[:10]}")
        resolved = resolve_refs(components[name], components)
        (OUT_DIR / filename).write_text(json.dumps(resolved, indent=1, sort_keys=True))
        print(f"wrote {OUT_DIR / filename}")


if __name__ == "__main__":
    main()
