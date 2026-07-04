"""Reflective contract: every evidence key a check can emit must be a CONSCIOUS
visual-map decision.

visual_map.py paints findings onto the topology diagram by reading a hand-
maintained set of evidence keys (_NODE_EV_KEYS / _PORT_EV_KEYS / _LINK_EV_KEYS,
plus the inline vlan keys and the paired ``impacts`` array). A check that starts
emitting a NEW evidence key today silently fails to paint. This test AST-scans
every module under src/digital_twin/checks/ for the evidence-emission idioms and
asserts each discovered key is either painted by visual_map or explicitly opted
out below with a reason. Adding a new key therefore forces a decision: paint it
or document why not.

Design note: AST scanning was chosen over "run all golden scenarios and collect
produced evidence keys" because the AST sees keys on paths no golden exercises
(rare severities, telemetry-gated branches), so the contract cannot rot as
scenario coverage shifts. The scanner is idiom-enforcing: an evidence dict built
through a spread it cannot resolve fails the test rather than being skipped.

Recognized emission idioms (everything else with ``**`` fails loudly):
  1. ``evidence={"k": ...}``          keyword arg, dict literal
  2. ``evidence = {"k": ...}``        (ann-)assignment to a name ``evidence``
  3. ``evidence["k"] = ...``          subscript store
  4. ``evidence={..., **extra}``      where ``extra`` is a parameter of the
     enclosing helper: keys are harvested from dict literals at the helper's
     call sites (positional or keyword), incl. the ``**(extra or {})`` form
  5. ``evidence={**ev, "k": ...}``    re-emission of an existing finding's
     evidence (spread source mentions ``.evidence``): no new keys by itself
"""

from __future__ import annotations

import ast
from pathlib import Path

import digital_twin.checks as checks_pkg
from digital_twin.viz import visual_map as vm

CHECKS_DIR = Path(checks_pkg.__file__).parent

# Keys visual_map paints inline (not via the three allowlist tuples):
# _affected_contributions reads ev.get("vlan") / ev.get("affected_vlans") as
# vlan references and walks ev.get("impacts") as paired attachment/vlan/cause
# entries. Guarded below by asserting the idioms still exist in the source.
_INLINE_PAINTED = {"vlan", "affected_vlans", "impacts"}

# Evidence keys that are intentionally NOT painted on the topology diagram.
# One comment per entry. If your new key lands here, say WHY it should not
# paint (scalar/count/non-entity string), or add it to a visual_map allowlist.
_NOT_VISUALIZED = {
    # --- registry crash note ---
    "error",                # exception text from a crashed check; not an entity
    # --- pairwise mismatch checks (l1/mtu/native): the "link" key already
    #     paints both endpoints; the a_/b_ keys are per-side detail scalars ---
    "a_port", "b_port",     # endpoint port ids, redundant with painted "link"
    "a_l1", "b_l1",         # speed/duplex params per side (scalars)
    "a_mtu", "b_mtu",       # MTU integers per side
    "a_native", "b_native", # native-vlan ids per side (vlan of the LINK, not a view)
    # --- subnet_overlap ---
    "a", "b",               # [scope_id, subnet] pairs; scopes are not diagram entities
    # --- bgp_adjacency: BGP session attributes, not topology entities ---
    "neighbor_ip", "vrf", "broken_peers", "baseline_state", "baseline_neighbor_as",
    "base_local_as", "proposed_local_as", "base_neighbor_as", "proposed_neighbor_as",
    "local_as_changed", "neighbor_as_changed",
    "base_type", "proposed_type", "base_via", "proposed_via",
    # --- ospf_withdrawal: protocol scalars; "device"/"vlan" already paint ---
    "area", "base_areas", "proposed_areas",
    "base_metric", "proposed_metric",
    "base_prefix", "proposed_prefix",
    # --- stp_root / stp_edge ---
    "component_devices",    # device names duplicated in message; "baseline_root"/
                            # "proposed_root" carry the painted nodes
    "peer",                 # far-end port id; the emitted "link" key paints the pair
    "flag",                 # knob name (stp_edge/no_root_port), not an entity
    # --- l2_isolation: fragment_nodes is painted; the rest is explanatory ---
    "exit_anchor_nodes",    # baseline-side anchors, often absent from proposed IR
    "lost_anchor_nodes",    # ditto (removed devices cannot paint on proposed views)
    "lost_peers",           # baseline peer names for the message, not proposed nodes
    "occupants",            # client MACs/counts stranded on the fragment (scalars)
    "severity_reason",      # human-readable severity rationale string
    # --- l2_vlan_segmentation: before/after component COUNTS, not node lists ---
    "baseline_components", "proposed_components",
    # --- l2_loop STP-protection detail (cycle_nodes/link_ids already paint) ---
    "stp_disabled_ports", "stp_unknown_ports",
    # --- l2_loop.self_loop: "ports" (added to _PORT_EV_KEYS) already paints
    #     the pair; observed_states is a per-port {state,role} scalar dict ---
    "observed_states",
    # --- gateway_gap: "vlan" paints the view; these are IP/interface scalars ---
    "gateway", "subnet", "l3_interfaces", "baseline_l3_interfaces",
    # --- dhcp_path / snooping provenance scalars ---
    "removed_sources",      # names of removed DHCP sources (strings, not entities)
    "source",               # snooping data source label (config vs stats)
    # --- client/observation counts and identity payloads ---
    "observed_clients",     # client counts/MACs; clients are not diagram nodes
    "clients", "clients_at_risk", "affected_wireless_clients",
    "observed_power_draw",  # watts (scalar)
    "powered_ap",           # AP name from LLDP; APs are not diagram nodes
    # --- wlan checks: SSIDs/WLANs have no topology-diagram representation ---
    "ssid", "affected_ssids", "wlans", "auth_type", "reason",
    # --- vlan_collision: subject already paints the vlan view ---
    "vlan_id", "collisions",
    # --- scope_lint: address scopes are config objects, not diagram entities ---
    "scope", "scopes", "declared", "handed", "violations",
    # --- misc config-knob scalars ---
    "disabled",             # admin_disable boolean; "port" already paints
    "knobs",                # unmodeled_change knob names (strings)
    # --- stp_policy.blocking_risk: entities already paint via "port"/
    #     affected_entities; these are classification/explanatory scalars ---
    "knob",                 # the single changed knob name (stp_required), a string
    "peer_kind",            # "ap"/"client"/"bpdu_filter" classification tag
    "tie_provenance",       # LLDP provenance label for the peer tie (string)
    "occupants_behind",     # occupant counts behind the port (dict of scalars)
    # --- stp_policy.root_protect_risk: "port" and "elected_root" (added to
    #     _NODE_EV_KEYS) already paint the entities; these are scalars ---
    "only_path",            # boolean — always True when this code fires
    "election_confidence",  # "high"/"unprovable"/"observed" classification tag (string)
    "observed_role",        # observed-root route: literal "root" classification tag
                            # (string); "port" already paints the entity
    # --- stp_policy.link_mismatch: "link" (already painted) carries the pair;
    #     these are per-knob detail, not additional entity references ---
    "values",                # {port_id: effective knob value} — port ids already
                              # painted via affected_entities/"link", values are bools/tokens
    "observed_modes",        # {port_id: StpMode} corroborating context, not a new entity
    # --- nac (org-level policy objects, no site topology to paint) ---
    "kind", "changed_fields", "shadower", "shadowed_action",
}


def _painted_keys() -> set[str]:
    return set(vm._NODE_EV_KEYS) | set(vm._PORT_EV_KEYS) | set(vm._LINK_EV_KEYS) | _INLINE_PAINTED


def _dict_literal_keys(d: ast.Dict) -> tuple[set[str], list[ast.expr]]:
    """(top-level string keys, spread value expressions) of a dict literal."""
    keys: set[str] = set()
    spreads: list[ast.expr] = []
    for k, v in zip(d.keys, d.values, strict=True):
        if k is None:
            spreads.append(v)
        elif isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
        else:
            raise AssertionError(
                f"non-literal evidence key {ast.dump(k)} — use string literals"
            )
    return keys, spreads


def _spread_name(expr: ast.expr) -> str | None:
    """Bare name of a spread: ``**extra`` or ``**(extra or {})``."""
    if isinstance(expr, ast.Name):
        return expr.id
    if (
        isinstance(expr, ast.BoolOp)
        and isinstance(expr.op, ast.Or)
        and expr.values
        and isinstance(expr.values[0], ast.Name)
    ):
        return expr.values[0].id
    return None


def _scan_module(path: Path) -> tuple[set[str], list[str]]:
    """(evidence keys emitted by this module, unresolved-idiom complaints)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    keys: set[str] = set()
    problems: list[str] = []

    # Pass 1: helpers whose parameter is spread into an evidence dict
    # (idiom 4), and their parameter position/name for call-site harvesting.
    spread_params: dict[str, tuple[int, str]] = {}  # func -> (arg index, arg name)
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        argnames = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "evidence" and isinstance(kw.value, ast.Dict):
                    for spread in _dict_literal_keys(kw.value)[1]:
                        name = _spread_name(spread)
                        if name in argnames:
                            assert name is not None
                            pos = [a.arg for a in fn.args.args]
                            spread_params[fn.name] = (
                                pos.index(name) if name in pos else -1,
                                name,
                            )

    # Names assigned from an existing finding's evidence (idiom 5),
    # e.g. ``ev = f.evidence or {}`` — spreading them adds no new keys.
    evidence_aliases = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and ".evidence" in ast.unparse(node.value)
        for t in node.targets
        if isinstance(t, ast.Name)
    }

    def harvest(d: ast.Dict, where: str) -> None:
        ks, spreads = _dict_literal_keys(d)
        keys.update(ks)
        for spread in spreads:
            src = ast.unparse(spread)
            name = _spread_name(spread)
            if name is not None and any(name == pname for _, pname in spread_params.values()):
                continue  # resolved via helper call sites below
            if ".evidence" in src or name in evidence_aliases:
                continue  # re-emission of already-scanned evidence
            problems.append(f"{path.name}:{where}: unrecognized evidence spread **{src}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "evidence" and isinstance(kw.value, ast.Dict):
                    harvest(kw.value, f"line {node.lineno}")
            # idiom 4 call sites: dict literals bound to a spread-parameter
            # (plain calls and ``self._helper(...)`` method calls alike)
            fname: str | None = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if fname in spread_params:
                idx, pname = spread_params[fname]
                bound: ast.expr | None = None
                if 0 <= idx < len(node.args):
                    bound = node.args[idx]
                for kw in node.keywords:
                    if kw.arg == pname:
                        bound = kw.value
                if isinstance(bound, ast.Dict):
                    keys.update(_dict_literal_keys(bound)[0])
                elif bound is not None and not (
                    isinstance(bound, ast.Constant) and bound.value is None
                ):
                    problems.append(
                        f"{path.name}:line {node.lineno}: non-literal dict passed to "
                        f"evidence-spread parameter {pname!r} of {fname}()"
                    )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Name)
                    and t.id == "evidence"
                    and isinstance(node.value, ast.Dict)
                ):
                    harvest(node.value, f"line {node.lineno}")
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "evidence"
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                ):
                    keys.add(t.slice.value)
    return keys, problems


def _scan_all() -> tuple[dict[str, set[str]], list[str]]:
    by_key: dict[str, set[str]] = {}
    problems: list[str] = []
    for path in sorted(CHECKS_DIR.rglob("*.py")):
        ks, probs = _scan_module(path)
        problems.extend(probs)
        for k in ks:
            by_key.setdefault(k, set()).add(path.name)
    return by_key, problems


def test_scanner_recognizes_every_emission_idiom():
    _, problems = _scan_all()
    assert not problems, "evidence built through an idiom the scanner cannot " \
        "resolve — extend the scanner or simplify the emission:\n" + "\n".join(problems)


def test_scanner_finds_a_healthy_key_population():
    """Regression guard for the scanner itself: if a refactor of the emission
    idiom blinds the AST scan, the contract test would pass vacuously."""
    by_key, _ = _scan_all()
    for expected in ("vlan", "port", "link_ids", "component_nodes", "impacts"):
        assert expected in by_key, f"scanner no longer sees {expected!r}"
    assert len(by_key) > 40, f"suspiciously few evidence keys found: {sorted(by_key)}"


def test_visual_map_still_paints_the_inline_keys():
    """_INLINE_PAINTED keys are read inline by _affected_contributions rather
    than through the allowlist tuples; pin the idioms so the exemption above
    cannot outlive the code."""
    src = Path(vm.__file__).read_text()
    assert 'ev.get("vlan")' in src
    assert 'ev.get("affected_vlans")' in src
    assert 'ev.get("impacts")' in src


def test_every_evidence_key_is_painted_or_consciously_opted_out():
    by_key, _ = _scan_all()
    painted = _painted_keys()
    unaccounted = {
        k: sorted(mods)
        for k, mods in sorted(by_key.items())
        if k not in painted and k not in _NOT_VISUALIZED
    }
    assert not unaccounted, (
        "new evidence key(s) with no visual-map decision — either add the key to "
        "a visual_map allowlist (_NODE_EV_KEYS/_PORT_EV_KEYS/_LINK_EV_KEYS) so it "
        f"paints, or opt out in _NOT_VISUALIZED with a reason: {unaccounted}"
    )


def test_opt_out_set_carries_no_dead_weight():
    """An opted-out key that no check emits any more, or that visual_map now
    paints, is a stale entry — prune it so the set stays meaningful."""
    by_key, _ = _scan_all()
    stale_gone = _NOT_VISUALIZED - set(by_key)
    assert not stale_gone, f"opted-out keys no longer emitted by any check: {sorted(stale_gone)}"
    both = _NOT_VISUALIZED & _painted_keys()
    assert not both, \
        f"keys both painted and opted out — remove from _NOT_VISUALIZED: {sorted(both)}"
