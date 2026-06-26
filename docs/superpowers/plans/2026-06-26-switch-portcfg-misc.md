# Switch Port-Config Misc Attrs (SP4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the final 5 switch port attrs in-scope: `voip_network` as a real voice VLAN (full VLAN-graph integration + a client.impact extension), `mac_limit` as a real over-limit drop check, and `inter_switch_link`/`storm_control`/`enable_qos` as recognized→REVIEW.

**Architecture:** `voip_network` resolves to a voice VLAN folded into the port's carriage + an ACCESS-port voice membership in the VLAN graph, so existing L2 checks react, plus a `client.impact` `vlan_removed` branch. `mac_limit` → `Port.mac_limit` + a `wired.port.mac_limit_exceeded` check (requires WIRED_L2 only, inspects CLIENTS_ACTIVE internally, both-sides). The three knobs → a `PortMisc` value object + a `wired.port.unmodeled_change` policy-floor check.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col, E/F/I), mypy-strict, networkx.

**Spec:** `docs/superpowers/specs/2026-06-26-switch-portcfg-misc-design.md`

## Global Constraints

- **No false-SAFE / no-drop:** an in-scope leaf is modeled by a check or not allowlisted; a recognized leaf never resolves SAFE on a change. `Port.misc = None` ⇔ whole misc surface default; `mac_limit` templated/unparseable is a token, never collapsed to "unlimited".
- **OAS maps:** none of the 5 are on `port_config`. `mac_limit` is on overwrite+local+usage; the other four on local+usage only. Resolver applies them from local + usage (never `port_config` inline) — `mac_limit` additionally from overwrite. Gate: `port_usages.*` (site/device/networktemplate) + `local_port_config.*` (device); `mac_limit` also `port_config_overwrite.*`.
- **voip voice membership is ACCESS-mode-gated;** trunks carry voice via tagged carriage, not as endpoint members.
- **mac_limit_exceeded** `requires() = {WIRED_L2}` only; "client data present" ⇔ `CLIENTS_ACTIVE in base_ir.capabilities AND in prop_ir.capabilities`; capped at REVIEW (never ERROR/UNSAFE).
- **Gate after every task, from the worktree:** `uv run pytest -q && uv run ruff check . && uv run mypy src` — all green. (mypy not on tests/; test-only Pyright noise isn't a gate failure.)
- **Commits** end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Worktree:** work ONLY in `/Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc`; run git as `git -C <that-path> ...` and verify HEAD branch + parent before committing. NEVER touch the main checkout.

---

## File Structure

- **Modify** `ir/entities.py` — `Port.voice_vlan` (T1), `Port.mac_limit` (T3), `PortMisc` + `Port.misc` (T4).
- **Modify** `adapters/mist/ingest/ports.py` — resolver tuples + `voice_vlan_of` (T1).
- **Modify** `adapters/mist/ingest/switch.py` — voice/`_mac_limit`/`_port_misc` wiring (T1, T3, T4).
- **Modify** `ir/indexes.py` — `access_ports_by_vlan` voice membership (T1).
- **Modify** `checks/wired/client_impact.py` — `vlan_removed` branch (T2).
- **Create** `checks/wired/mac_limit.py` (T3), `checks/wired/unmodeled_change.py` (T4).
- **Modify** `checks/wired/__init__.py` (T3, T4), `tests/test_public_api.py` (23→24 T3, 24→25 T4).
- **Modify** `scope/allowlist.py` — `voip_network` (T2), `mac_limit` (T3), 3 knobs (T4).
- **Tests:** `tests/adapters/mist/test_ingest_ports.py`, `test_ingest_switch.py`, `tests/ir/test_indexes.py` (or sibling), `tests/checks/test_client_impact*.py`, `tests/checks/test_mac_limit.py` (new), `tests/checks/test_unmodeled_change.py` (new), `tests/scope/test_allowlist.py`/`test_field_gate.py`, `tests/engine/test_pipeline.py`, `docs/ROADMAP.md`.

---

## Task 1: `voip_network` → voice VLAN (IR + resolver + ingest + VLAN membership)

**Files:**
- Modify: `ir/entities.py` (`Port.voice_vlan`), `adapters/mist/ingest/ports.py` (tuples + `voice_vlan_of`), `adapters/mist/ingest/switch.py` (wire voice), `ir/indexes.py` (`access_ports_by_vlan`)
- Test: `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`, `tests/ir/test_indexes.py`

**Interfaces:**
- Produces: `Port.voice_vlan: int | None`; `voice_vlan_of(usage: dict, networks: dict) -> int | None`; `_MISC_ATTRS` (the 5 SP4 names) in `ports.py`; `access_ports_by_vlan` includes ACCESS-port voice membership.

- [ ] **Step 1: Write failing tests**

Add to `tests/adapters/mist/test_ingest_ports.py`:
```python
def test_voip_resolves_from_usage_not_port_config():
    eff = _eff(
        port_usages={"voice": {"mode": "access", "port_network": "corp", "voip_network": "voice"}},
        port_config={"ge-0/0/1": {"usage": "voice"},
                     "ge-0/0/2": {"usage": "office", "voip_network": "voice"}},  # pc voip ignored
    )
    # NETWORKS in this fixture: corp=10, voice=30
    u1, _ = _resolved(eff)["ge-0/0/1"]
    assert u1.get("voip_network") == "voice"          # from usage
    u2, _ = _resolved(eff)["ge-0/0/2"]
    assert u2.get("voip_network") is None              # port_config voip NOT applied


def test_voice_vlan_of_resolution_and_unresolvable():
    from digital_twin.adapters.mist.ingest.ports import voice_vlan_of
    nets = {"voice": {"vlan_id": 30}, "bad": {"vlan_id": "{{voice_vlan}}"}, "none": {}}
    assert voice_vlan_of({"voip_network": "voice"}, nets) == 30
    assert voice_vlan_of({"voip_network": "bad"}, nets) is None    # templated -> None, no crash
    assert voice_vlan_of({"voip_network": "none"}, nets) is None   # no vlan_id -> None
    assert voice_vlan_of({"voip_network": "absent"}, nets) is None
    assert voice_vlan_of({}, nets) is None
```
Add to `tests/adapters/mist/test_ingest_switch.py` (mirror the `test_l1_config_*` IngestContext scaffold; mac prefix `aa0000000001`):
```python
def test_voip_sets_voice_vlan_and_access_membership():
    eff = {
        "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
        "port_usages": {"phone": {"mode": "access", "port_network": "corp", "voip_network": "voice"},
                        "up": {"mode": "trunk", "all_networks": True, "voip_network": "voice"}},
        "port_config": {"ge-0/0/1": {"usage": "phone"}, "ge-0/0/2": {"usage": "up"}},
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.indexes import access_ports_by_vlan
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder())
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    acc = ir.ports["aa0000000001:ge-0/0/1"]
    assert acc.voice_vlan == 30 and 30 in acc.tagged_vlans   # access: voice folded + member
    members30 = {p.id for p in access_ports_by_vlan(ir).get(30, [])}
    assert "aa0000000001:ge-0/0/1" in members30              # access port is a MEMBER of voice vlan
    trunk = ir.ports["aa0000000001:ge-0/0/2"]
    assert trunk.voice_vlan == 30                            # trunk resolves voice...
    assert "aa0000000001:ge-0/0/2" not in members30          # ...but is NOT an endpoint member
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -k "voip or voice_vlan" -q`
Expected: FAIL (`Port` has no `voice_vlan`; voip applied from port_config; no voice membership).

- [ ] **Step 3: Resolver — move voip to local+usage; add `_MISC_ATTRS`; `voice_vlan_of`**

In `adapters/mist/ingest/ports.py`: REMOVE the `"voip_network",` line from `_USAGE_OVERRIDE_ATTRS` (it is NOT a `port_config` inline attr per OAS). After `_AUTH_ATTRS`, add:
```python
# SP4 misc attrs: OAS-present on local_port_config + port_usages ONLY (mac_limit
# also on port_config_overwrite). Applied from local here (+ overwrite for
# mac_limit); usage-level flows via usage_definition. NOT in _USAGE_OVERRIDE_ATTRS
# (the port_config inline layer — none of these are on port_config).
_MISC_ATTRS = (
    "voip_network", "mac_limit", "storm_control", "enable_qos", "inter_switch_link",
)
```
Change `_OVERWRITE_ATTRS` to add `mac_limit` (the only SP4 attr on overwrite):
```python
_OVERWRITE_ATTRS = ("port_network", "poe_disabled", "disabled", "speed", "duplex", "mac_limit")
```
Change `_LOCAL_ATTRS` to splice `_MISC_ATTRS`:
```python
_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled", *_AUTH_ATTRS, *_MISC_ATTRS)
```
Add a module helper next to `usage_vlans`:
```python
def voice_vlan_of(usage: dict[str, Any], networks: dict[str, Any]) -> int | None:
    """The voice VLAN id from `voip_network` (same namespace/resolution as
    port_network). None when unset OR unresolvable (a templated/non-numeric
    vlan_id must yield None, never raise)."""
    name = usage.get("voip_network")
    if not name or name not in networks:
        return None
    vid = networks[name].get("vlan_id")
    if vid is None:
        return None
    try:
        return int(vid)
    except (TypeError, ValueError):
        return None  # templated/unparseable vlan_id -> unresolvable
```

- [ ] **Step 4: IR field + ingest wiring**

In `ir/entities.py` `Port`, add after `tagged_vlans`:
```python
    voice_vlan: int | None = None  # SP4: resolved voip_network (voice VLAN), tagged
```
In `adapters/mist/ingest/switch.py`, import `voice_vlan_of` alongside `usage_vlans`, and in the `_switch_ports_and_l3` loop, after `native, tagged = usage_vlans(usage, networks)`:
```python
            voice = voice_vlan_of(usage, networks)
            if voice is not None and voice != native:
                tagged = tuple(sorted(set(tagged) | {voice}))
```
and add `voice_vlan=voice,` to the `Port(...)` kwargs (next to `tagged_vlans=tagged,`).

- [ ] **Step 5: Voice membership in the VLAN graph (ACCESS-gated)**

In `ir/indexes.py`, extend `access_ports_by_vlan`:
```python
def access_ports_by_vlan(ir: IR) -> dict[int, list[Port]]:
    """Member access ports keyed by VLAN: the native (data) VLAN, AND the voice
    VLAN (SP4) — an access phone is a member of both. ACCESS-only: a trunk carries
    the voice VLAN via tagged carriage, not as an endpoint member."""
    out: dict[int, list[Port]] = defaultdict(list)
    for p in ir.ports.values():
        if p.mode is not PortMode.ACCESS:
            continue
        if p.native_vlan is not None:
            out[p.native_vlan].append(p)
        if p.voice_vlan is not None and p.voice_vlan != p.native_vlan:
            out[p.voice_vlan].append(p)
    return dict(out)
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py tests/ir/ -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. **Goldens:** the voice VLAN now enters the VLAN graph as a member where `site.json` uses `voip_network` — run `uv run pytest tests/golden/ -q`; the suite is delta-based so static voice membership on both sides cancels, but if a golden churns, STOP and diff it (confirm it's a genuine voice-VLAN component effect) before re-pinning.

- [ ] **Step 7: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc add \
  src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/ports.py \
  src/digital_twin/adapters/mist/ingest/switch.py src/digital_twin/ir/indexes.py \
  tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc commit -m "$(cat <<'EOF'
feat(ir,ingest): model voip_network as a voice VLAN (carriage + access membership)

voice_vlan_of resolves voip_network like port_network; ingest folds it into
tagged_vlans and sets Port.voice_vlan; access_ports_by_vlan makes an ACCESS port
a MEMBER of its voice VLAN (trunks carry via tagged, not as members). voip moved
out of _USAGE_OVERRIDE_ATTRS to _MISC_ATTRS (local+usage only, not port_config).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: voip `client.impact` extension + field gate + e2e

**Files:**
- Modify: `checks/wired/client_impact.py` (`_impact_of` branch), `scope/allowlist.py` (`voip_network`)
- Test: `tests/checks/test_client_impact.py` (or sibling), `tests/scope/test_allowlist.py`, `tests/scope/test_field_gate.py`, `tests/engine/test_pipeline.py`

**Interfaces:**
- Consumes: `Port.voice_vlan` (T1); `client_impact._impact_of`; `_MODELED_USAGE_ATTRS`.
- Produces: `client.impact` `vlan_removed` impact; `voip_network` in scope (usage site/device/nettemplate + local device).

- [ ] **Step 1: Write failing tests**

Add to the client-impact test module (mirror its existing IR-building helpers — real `IRBuilder` + `wired_client` + `set_client_enrichment`):
```python
def test_voice_vlan_removed_flags_active_phone():
    # phone on voice VLAN 30; voip_network removed -> port no longer offers 30,
    # but VLAN 30 stays healthy elsewhere (blackhole would miss it) -> vlan_removed
    from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
    from digital_twin.analysis.context import AnalysisContext
    from digital_twin.checks.base import CheckContext, Status
    from digital_twin.checks.wired.client_impact import ClientImpactCheck
    from tests.factories import sw, wired_client

    def ir(voice):
        b = IRBuilder().add_device(sw("S"))
        b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                        mode=PortMode.ACCESS, native_vlan=10, voice_vlan=voice,
                        tagged_vlans=((30,) if voice else ())))
        b.add_client(wired_client("ph:01", "S:ge-0/0/1", vlan=30))
        b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
        return b.build()

    base, prop = ir(30), ir(None)
    r = ClientImpactCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))
    assert r.status is Status.WARN
    impacts = r.findings[0].evidence["impacts"]
    assert any(i["impact"] == "vlan_removed" and i["mac"] == "ph:01" for i in impacts)
```
Add to `tests/scope/test_allowlist.py`:
```python
def test_voip_network_in_scope_usage_and_local_not_port_config():
    site, dev = set(RAW_ALLOWLIST["site_setting"]), set(RAW_ALLOWLIST["device"])
    assert "port_usages.*.voip_network" in site and "port_usages.*.voip_network" in dev
    assert "local_port_config.*.voip_network" in dev
    assert "local_port_config.*.voip_network" not in site
    assert "port_config.*.voip_network" not in dev
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_client_impact.py tests/scope/test_allowlist.py -k "voice_vlan_removed or voip" -q`
Expected: FAIL (no `vlan_removed` impact; voip not allowlisted).

- [ ] **Step 3: Extend `client_impact._impact_of`**

In `checks/wired/client_impact.py` `_impact_of`, after the existing native `vlan_move` block (and before the `vlan = client.vlan` blackhole section), add (within the `attach_kind is PORT` branch, where `base_port`/`prop_port` are known non-None):
```python
            base_offered = {base_port.native_vlan, base_port.voice_vlan} - {None}
            prop_offered = {prop_port.native_vlan, prop_port.voice_vlan} - {None}
            if client.vlan in base_offered and client.vlan not in prop_offered:
                return self._entry(
                    ctx, client, "vlan_removed",
                    f"vlan {client.vlan} no longer offered on this port",
                    caused_by=ctx.delta_index.causes("port", [client.attach_id]),
                )
```

- [ ] **Step 4: Allowlist `voip_network`**

In `scope/allowlist.py`, add `"voip_network"` to `_MODELED_USAGE_ATTRS` (→ `port_usages.*` for site/device/networktemplate and `local_port_config.*` for device; not port_config/overwrite — voip isn't on those).

- [ ] **Step 5: e2e**

Add to `tests/engine/test_pipeline.py` using this file's real helpers (`dc_replace`/`_raw`/`FakeProvider`/`_plan`/`_op`/`simulate`/`Decision`/`SETTING`/`SWITCH`). Done at SITE-SETTING level (cleaner than `local_port_config` — avoids the local-`None` removal-semantics question, and `voip_network` on `port_usages` is allowlisted at site_setting scope). Two facts the template depends on: (1) `SETTING.networks` already has `corp`=10 + `voice`=30, but `SETTING.port_usages` only defines `office`/`uplink` and `SWITCH.port_config` is the RANGE `ge-0/0/0-1` — so the template MUST inject a `phone` usage into the raw setting and give the switch an explicit `ge-0/0/0` phone port; (2) **CLIENTS_ACTIVE** needs BOTH `"wired_clients"` and `"wireless_clients"` in `raw.meta.fetched` (`ingest/clients.py`; `_raw()` ships `("devices",)`), and wired-client dicts use keys `device_mac`/`port_id`/`mac`/`vlan` (NOT `vlan_id`). Template:
```python
def test_voip_removal_flags_active_phone_e2e():
    # baseline: a phone access port offers data VLAN 10 + voice VLAN 30, with a
    # live phone on VLAN 30; the op drops voip_network from the phone usage ->
    # the port stops offering VLAN 30 -> client.impact vlan_removed (the VLAN may
    # still be healthy elsewhere, so blackhole would miss it).
    phone = {"mode": "access", "port_network": "corp", "voip_network": "voice"}
    setting = {**SETTING, "port_usages": {**SETTING["port_usages"], "phone": phone}}
    sw_a = {**SWITCH, "port_config": {
        "ge-0/0/0": {"usage": "phone"}, "ge-0/0/1": {"usage": "office"}}}
    raw0 = _raw()
    raw = dc_replace(
        raw0, setting=setting, devices=(sw_a,),
        wired_clients=({"device_mac": "aa0000000001", "port_id": "ge-0/0/0",
                        "mac": "ph01", "vlan": 30},),
        meta=dc_replace(raw0.meta, fetched=("devices", "wired_clients", "wireless_clients")),
    )
    # proposed: phone usage WITHOUT voip_network (partial payload — other roots persist)
    new_usages = {**setting["port_usages"], "phone": {"mode": "access", "port_network": "corp"}}
    v = simulate(_plan([_op(payload={"port_usages": new_usages})]), provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    assert "wired.client.impact.active_clients" in {f.code for f in v.findings}
    impacts = next(f for f in v.findings
                   if f.code == "wired.client.impact.active_clients").evidence["impacts"]
    assert any(i["impact"] == "vlan_removed" for i in impacts)
    assert v.decision is Decision.REVIEW
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS (incl. golden suite). If a golden churns, STOP and confirm it's a true voice-VLAN effect before re-pinning.

- [ ] **Step 7: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc add \
  src/digital_twin/checks/wired/client_impact.py src/digital_twin/scope/allowlist.py \
  tests/checks tests/scope/test_allowlist.py tests/engine/test_pipeline.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc commit -m "$(cat <<'EOF'
feat(checks,scope): voip client.impact (vlan_removed) + allowlist voip_network

client.impact now flags an active client whose offered VLAN (native OR voice) the
port stops offering (blackhole misses a still-healthy VLAN). voip_network in scope
on port_usages (site/device/nettemplate) + local_port_config (device).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `mac_limit` (IR + normalizer + check + registration + gate)

**Files:**
- Modify: `ir/entities.py` (`Port.mac_limit`), `adapters/mist/ingest/switch.py` (`_mac_limit` + wire)
- Create: `checks/wired/mac_limit.py`
- Modify: `checks/wired/__init__.py`, `tests/test_public_api.py` (23→24), `scope/allowlist.py`
- Test: `tests/adapters/mist/test_ingest_switch.py`, `tests/checks/test_mac_limit.py`, `tests/scope/test_allowlist.py`, `tests/scope/test_field_gate.py`

**Interfaces:**
- Consumes: T1 resolver (`mac_limit` in `_OVERWRITE_ATTRS`+`_LOCAL_ATTRS`); `clients_by_port`; `CheckContext`.
- Produces: `Port.mac_limit: int | str | None`; `_mac_limit(v) -> int | str | None`; `MacLimitExceededCheck` (`id="wired.port.mac_limit_exceeded"`); codes `.exceeded`/`.unverified`/`.unresolved`.

- [ ] **Step 1: Write failing tests**

Add to `tests/adapters/mist/test_ingest_switch.py`:
```python
def test_mac_limit_normalizer():
    from digital_twin.adapters.mist.ingest.switch import _mac_limit
    assert _mac_limit(5) == 5 and _mac_limit("5") == 5
    assert _mac_limit(0) is None and _mac_limit("") is None and _mac_limit(None) is None
    assert _mac_limit(True) is None
    assert isinstance(_mac_limit("{{var}}"), str) and _mac_limit("{{var}}").startswith("unresolved")
    assert isinstance(_mac_limit({"x": 1}), str)  # object -> token, not None
```
Create `tests/checks/test_mac_limit.py`:
```python
"""wired.port.mac_limit_exceeded: a lowered/new MAC cap that drops currently-
connected wired clients (or can't be confirmed safe). requires WIRED_L2 only;
client data = CLIENTS_ACTIVE on BOTH sides; capped at REVIEW."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.mac_limit import MacLimitExceededCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from tests.factories import sw, wired_client


def _ir(limit, *, n_clients=0, clients_active=True):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, mac_limit=limit))
    for i in range(n_clients):
        b.add_client(wired_client(f"cc:{i:02}", "S:ge-0/0/1", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    if clients_active:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def _run(base, prop):
    return MacLimitExceededCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def test_requires_wired_l2_only():
    assert MacLimitExceededCheck().requires() == frozenset({IRCapability.WIRED_L2})


def test_lowered_below_observed_is_review_exceeded():
    base, prop = _ir(None, n_clients=3), _ir(2, n_clients=3)
    r = _run(base, prop)
    f = r.findings[0]
    assert f.code == "wired.port.mac_limit_exceeded.exceeded"
    assert f.severity is Severity.WARNING and r.status is Status.WARN


def test_within_limit_with_clients_is_silent():
    assert _run(_ir(None, n_clients=2), _ir(10, n_clients=2)).findings == ()


def test_restrictive_without_client_caps_is_unverified():
    base, prop = _ir(None, n_clients=0, clients_active=False), _ir(2, n_clients=0, clients_active=False)
    assert _run(base, prop).findings[0].code == "wired.port.mac_limit_exceeded.unverified"


def test_baseline_lacks_capability_is_unverified():
    base = _ir(None, n_clients=0, clients_active=False)      # baseline blind
    prop = _ir(2, n_clients=0, clients_active=True)          # proposed has it
    assert _run(base, prop).findings[0].code == "wired.port.mac_limit_exceeded.unverified"


def test_unresolved_limit_is_review():
    assert _run(_ir(None), _ir("unresolved:{{v}}")).findings[0].code == \
        "wired.port.mac_limit_exceeded.unresolved"


def test_raised_or_unlimited_is_silent():
    assert _run(_ir(2, n_clients=1), _ir(10, n_clients=1)).findings == ()
    assert _run(_ir(2, n_clients=1), _ir(None, n_clients=1)).findings == ()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_mac_limit.py tests/adapters/mist/test_ingest_switch.py -k mac_limit -q`
Expected: FAIL (`_mac_limit`/`MacLimitExceededCheck` undefined; `Port` has no `mac_limit`).

- [ ] **Step 3: IR field + `_mac_limit` + wire**

In `ir/entities.py` `Port`, add (near the other config fields):
```python
    mac_limit: int | str | None = None  # SP4: concrete cap / None=unlimited / str=unresolved token
```
In `adapters/mist/ingest/switch.py`, add a helper next to `_l1_config`:
```python
def _mac_limit(v: Any) -> int | str | None:
    """Concrete cap (int>0) / None (unlimited: 0/absent/empty/bool) / a stable
    `unresolved:` token (templated/object/unparseable — NEVER collapsed to None,
    which would hide an in-scope change)."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s) or None
        return f"unresolved:{s}" if s else None
    return f"unresolved:{v!r}"
```
In the switch `Port(...)` construction, add `mac_limit=_mac_limit(usage.get("mac_limit")),`.

- [ ] **Step 4: Implement the check**

Create `src/digital_twin/checks/wired/mac_limit.py`:
```python
"""wired.port.mac_limit_exceeded — a lowered/new MAC limit that drops currently-
connected wired clients, or that we cannot confirm is safe.

Per port whose mac_limit the delta made MORE RESTRICTIVE (None=unlimited >
concrete > lower-concrete; an unresolved/templated value is uncertain): if the
limit is unresolved -> REVIEW (.unresolved); else if active wired-client data is
present on BOTH sides (CLIENTS_ACTIVE) -> compare the baseline client count
(currently connected) against the new limit: over -> REVIEW (.exceeded), within
-> silent; if client data is absent -> REVIEW (.unverified, cannot confirm). The
*count* over-limit is certain but *which* MACs the switch evicts (and aging) are
not, so this is capped at REVIEW — never ERROR/UNSAFE; requires WIRED_L2 only so
the .unverified path is not registry-short-circuited.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity,
)
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import Client
from digital_twin.ir.indexes import clients_by_port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("current per-port client count is unobservable (no active-client data)",),
)


def _more_restrictive(old: int | str | None, new: int | str | None) -> bool:
    """`new` caps more than `old`. None=unlimited (least), int=that cap, str=
    unresolved (treated as uncertain -> restrictive when it changed)."""
    if new is None:
        return False
    if isinstance(new, str):
        return old != new
    # new is concrete int
    if old is None or isinstance(old, str):
        return True
    return new < old


class MacLimitExceededCheck:
    id = "wired.port.mac_limit_exceeded"
    title = "MAC limit lowered below connected clients"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("client")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        clients_known = (
            IRCapability.CLIENTS_ACTIVE in base_ir.capabilities
            and IRCapability.CLIENTS_ACTIVE in prop_ir.capabilities
        )
        wired = clients_by_port(base_ir)
        findings: list[Finding] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].mac_limit if pid in base_ir.ports else None
            new = prop_ir.ports[pid].mac_limit if pid in prop_ir.ports else None
            if old == new or not _more_restrictive(old, new):
                continue
            f = self._finding(ctx, pid, new, wired, clients_known)
            if f is not None:
                findings.append(f)
        worst = Status.WARN if findings else Status.PASS
        return CheckResult(
            check_id=self.id, status=worst, findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_HIGH,
            reasoning="compared per-port mac_limit vs connected clients baseline vs proposed",
        )

    def _finding(
        self, ctx: CheckContext, pid: str, new: int | str | None,
        wired: dict[str, list[Client]], clients_known: bool,
    ) -> Finding | None:
        cause = ctx.delta_index.causes("port", [pid])
        if isinstance(new, str):  # unresolved/templated
            return self._mk(pid, "unresolved", _MEDIUM,
                            f"mac_limit changed to a non-evaluable value ({new})", cause)
        # new is a concrete int (restrictive, per the caller's _more_restrictive gate)
        if not clients_known:
            return self._mk(pid, "unverified", _MEDIUM,
                            f"mac_limit set to {new}; current client count is unobservable", cause)
        observed = len(wired.get(pid, []))
        if observed > new:
            return self._mk(pid, "exceeded", _HIGH,
                            f"{observed} connected client(s) exceed the new mac_limit {new}", cause)
        return None  # proven within the cap

    def _mk(
        self, pid: str, code: str, conf: Confidence, msg: str, cause: tuple[Cause, ...],
    ) -> Finding:
        return Finding(
            source=FindingSource.CHECK, category=FindingCategory.NETWORK,
            code=f"{self.id}.{code}", severity=Severity.WARNING, confidence=conf,
            message=f"port {pid}: {msg}", affected_entities=(pid,),
            subject=ObjectRef("port", pid), evidence={"port": pid}, caused_by=cause,
        )
```

- [ ] **Step 5: Register + bump public API**

In `checks/wired/__init__.py`: import `from .mac_limit import MacLimitExceededCheck`; append `MacLimitExceededCheck()` to `ALL_WIRED_CHECKS`; add `"MacLimitExceededCheck"` to `__all__`. In `tests/test_public_api.py` change `== 23` to `== 24`.

- [ ] **Step 6: Allowlist `mac_limit`**

In `scope/allowlist.py`: add `"mac_limit"` to `_MODELED_USAGE_ATTRS` (usage + local) AND add `"port_config_overwrite.*.mac_limit"` to `_OVERWRITE_LEAVES` (the only SP4 attr on overwrite).

Add scope tests to `tests/scope/test_allowlist.py`:
```python
def test_mac_limit_in_scope_usage_local_overwrite_not_port_config():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_usages.*.mac_limit" in dev and "local_port_config.*.mac_limit" in dev
    assert "port_config_overwrite.*.mac_limit" in dev
    assert "port_config.*.mac_limit" not in dev
```

- [ ] **Step 6b: Reconcile existing `mac_limit`-as-unmodeled tests**

Prior SPs used `mac_limit` as the stand-in "still-unmodeled" leaf. Now that it is in scope, three existing assertions go stale and MUST be repointed to a *permanently*-unmodeled leaf (verified against `oas/device_switch.schema.json`: `poe_keep_state_when_reboot` is on `port_config_overwrite` but unmodeled; `use_vstp` is on `port_usages`/`local_port_config` but unmodeled — neither is an SP4 attr).

In `tests/scope/test_allowlist.py`, the line `assert "port_config_overwrite.*.mac_limit" not in device  # still unmodeled` → change to two lines:
```python
    assert "port_config_overwrite.*.mac_limit" in device  # SP4: resolver-honored + modeled
    assert "port_config_overwrite.*.poe_keep_state_when_reboot" not in device  # still unmodeled
```
In `tests/scope/test_field_gate.py` `test_unmodeled_usage_leaf_rejects`, swap `mac_limit` → `use_vstp` (comment: SP4 moved mac_limit into scope; use_vstp stays unmodeled):
```python
    payload = {
        **CURRENT,
        "port_usages": {"office": {"mode": "access", "port_network": "corp", "use_vstp": True}},
    }
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection)
    assert any("use_vstp" in reason for reason in r.reasons)
```
In `tests/scope/test_field_gate.py` `test_unmodeled_overwrite_leaf_still_rejects`, swap `mac_limit` → `poe_keep_state_when_reboot`:
```python
    payload = {**SWITCH_CUR, "port_config_overwrite": {
        "ge-0/0/0": {"poe_keep_state_when_reboot": True}}}
    r = screen_op("device", SWITCH_CUR, payload)
    assert isinstance(r, Rejection)
    assert any("port_config_overwrite.ge-0/0/0.poe_keep_state_when_reboot" in reason
               for reason in r.reasons)
```
(Also grep the test suite for any other `mac_limit` rejection assumption: `grep -rn "mac_limit" tests/scope tests/engine` — repoint any that assert it is out-of-scope/UNKNOWN.)

- [ ] **Step 7: Run tests + gate**

Run: `uv run pytest tests/checks/test_mac_limit.py tests/test_public_api.py tests/scope/ tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc add \
  src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/switch.py \
  src/digital_twin/checks/wired/mac_limit.py src/digital_twin/checks/wired/__init__.py \
  tests/test_public_api.py src/digital_twin/scope/allowlist.py \
  tests/checks/test_mac_limit.py tests/scope/test_allowlist.py tests/scope/test_field_gate.py \
  tests/adapters/mist/test_ingest_switch.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc commit -m "$(cat <<'EOF'
feat: wired.port.mac_limit_exceeded — lowered MAC cap drops connected clients

Port.mac_limit (int|str-token|None); _mac_limit normalizer (no silent collapse of
templated/object). Check requires WIRED_L2 only, inspects CLIENTS_ACTIVE on both
sides; restrictive limit over observed -> .exceeded, no client data -> .unverified,
unresolved -> .unresolved; capped at REVIEW. Allowlisted on usage+local+overwrite.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `PortMisc` (inter_switch_link / storm_control / enable_qos) → recognized→REVIEW

**Files:**
- Modify: `ir/entities.py` (`PortMisc` + `Port.misc`), `adapters/mist/ingest/switch.py` (`_port_misc` + `_storm_digest` + wire)
- Create: `checks/wired/unmodeled_change.py`
- Modify: `checks/wired/__init__.py`, `tests/test_public_api.py` (24→25), `scope/allowlist.py`
- Test: `tests/ir/test_port_misc.py`, `tests/checks/test_unmodeled_change.py`, `tests/adapters/mist/test_ingest_switch.py`, `tests/scope/test_allowlist.py`

**Interfaces:**
- Consumes: T1 resolver (these 3 in `_LOCAL_ATTRS` via `_MISC_ATTRS`); `CheckContext`.
- Produces: `PortMisc` (frozen); `Port.misc: PortMisc | None`; `_port_misc(usage) -> PortMisc | None`; `PortUnmodeledChangeCheck` (`id="wired.port.unmodeled_change"`); code `.recognized`.

- [ ] **Step 1: Write failing tests**

Create `tests/ir/test_port_misc.py`:
```python
from digital_twin.ir.entities import PortMisc


def test_default_is_all_default():
    assert PortMisc() == PortMisc()


def test_lone_flip_is_non_default():
    assert PortMisc(enable_qos=True) != PortMisc()
    assert PortMisc(inter_switch_link=True) != PortMisc()
    assert PortMisc(storm_control="percentage=50") != PortMisc()
```
Add storm-default normalization tests to `tests/adapters/mist/test_ingest_switch.py`:
```python
def test_port_misc_storm_defaults_normalize_to_none():
    from digital_twin.adapters.mist.ingest.switch import _port_misc, _storm_digest
    # a default-shaped storm_control object == no misc surface (no REVIEW)
    default_sc = {"disable_port": False, "no_broadcast": False, "no_multicast": False,
                  "no_registered_multicast": False, "no_unknown_unicast": False, "percentage": 80}
    assert _storm_digest(default_sc) is None
    assert _port_misc({"storm_control": default_sc}) is None
    assert _port_misc({}) is None
    # only a non-default value digests / makes a non-None PortMisc
    assert _storm_digest({**default_sc, "percentage": 50}) == "percentage=50"
    assert _port_misc({"storm_control": {**default_sc, "no_broadcast": True}}) is not None
```
Create `tests/checks/test_unmodeled_change.py`:
```python
"""wired.port.unmodeled_change: inter_switch_link/storm_control/enable_qos changes
are recognized and floored to REVIEW (impact not modeled). Never SAFE/UNSAFE."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.unmodeled_change import PortUnmodeledChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import PortMisc
from tests.factories import sw


def _ir(misc):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, misc=misc))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return PortUnmodeledChangeCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def test_enable_qos_change_is_review():
    r = _run(_ir(None), _ir(PortMisc(enable_qos=True)))
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.port.unmodeled_change.recognized"
    assert r.findings[0].severity is Severity.WARNING


def test_no_change_is_silent():
    assert _run(_ir(None), _ir(None)).findings == ()
    assert _run(_ir(PortMisc(enable_qos=True)), _ir(PortMisc(enable_qos=True))).findings == ()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ir/test_port_misc.py tests/checks/test_unmodeled_change.py -q`
Expected: FAIL (`PortMisc`/check undefined).

- [ ] **Step 3: IR `PortMisc` + `Port.misc` + ingest**

In `ir/entities.py`, add a frozen dataclass near `PortAuth`:
```python
@dataclass(frozen=True)
class PortMisc:
    """SP4 recognized-but-unmodeled port knobs (inter_switch_link / enable_qos /
    storm_control). Frozen + comparable; Port.misc is None ONLY when all are
    default, so a lone flip is detectable."""

    inter_switch_link: bool = False
    enable_qos: bool = False
    storm_control: str | None = None  # canonical digest of the storm_control object
```
In `Port`, add: `misc: PortMisc | None = None  # SP4: recognized->REVIEW knobs`.
In `adapters/mist/ingest/switch.py`, add helpers next to `_mac_limit`:
```python
_SENTINEL = object()  # "key absent from defaults" marker (unknown keys are kept)
# OAS defaults for storm_control — a default-shaped object (real fixtures send
# exactly this) must normalize to None so absent == explicit-default == no REVIEW.
_STORM_DEFAULTS: dict[str, Any] = {
    "disable_port": False, "no_broadcast": False, "no_multicast": False,
    "no_registered_multicast": False, "no_unknown_unicast": False, "percentage": 80,
}


def _storm_digest(sc: Any) -> str | None:
    """Order-independent canonical digest of the NON-default storm_control fields;
    None when absent OR all-default (so unset == explicit-default). Unknown keys
    are kept (conservative)."""
    if not isinstance(sc, dict):
        return None
    nondefault = {k: v for k, v in sc.items() if _STORM_DEFAULTS.get(k, _SENTINEL) != v}
    if not nondefault:
        return None
    return ";".join(f"{k}={nondefault[k]}" for k in sorted(nondefault))


def _port_misc(usage: dict[str, Any]) -> PortMisc | None:
    m = PortMisc(
        inter_switch_link=bool(usage.get("inter_switch_link")),
        enable_qos=bool(usage.get("enable_qos")),
        storm_control=_storm_digest(usage.get("storm_control")),
    )
    return m if m != PortMisc() else None
```
Import `PortMisc` from `digital_twin.ir.entities`; add `misc=_port_misc(usage),` to the switch `Port(...)`.

- [ ] **Step 4: Implement the check**

Create `src/digital_twin/checks/wired/unmodeled_change.py`:
```python
"""wired.port.unmodeled_change — inter_switch_link / storm_control / enable_qos
changed. These have no reachability/connectivity model the twin reasons about
(inter_switch_link enables the unmodeled networks.isolation feature; storm_control
is a runtime traffic-protection knob; enable_qos is pure scheduling), so the twin
recognizes the change and floors REVIEW — never SAFE, never ERROR/UNSAFE.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Capability, Confidence, ConfidenceLevel, IRCapability, IRDiff
from digital_twin.ir.entities import PortMisc

_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the changed knob has no modeled connectivity impact",),
)


def _changed(old: PortMisc | None, new: PortMisc | None) -> list[str]:
    o, n = old or PortMisc(), new or PortMisc()
    out: list[str] = []
    if o.inter_switch_link != n.inter_switch_link:
        out.append("inter_switch_link")
    if o.enable_qos != n.enable_qos:
        out.append("enable_qos")
    if o.storm_control != n.storm_control:
        out.append("storm_control")
    return out


class PortUnmodeledChangeCheck:
    id = "wired.port.unmodeled_change"
    title = "Recognized-but-unmodeled port knob changed"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        findings: list[Finding] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].misc if pid in base_ir.ports else None
            new = prop_ir.ports[pid].misc if pid in prop_ir.ports else None
            if old == new:
                continue
            knobs = _changed(old, new)
            if not knobs:
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.recognized", severity=Severity.WARNING, confidence=_MEDIUM,
                    message=f"port {pid}: {', '.join(knobs)} changed — impact not modeled (review)",
                    affected_entities=(pid,), subject=ObjectRef("port", pid),
                    evidence={"port": pid, "knobs": knobs},
                    caused_by=ctx.delta_index.causes("port", [pid]),
                )
            )
        return CheckResult(
            check_id=self.id, status=Status.WARN if findings else Status.PASS,
            findings=tuple(findings), coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=_MEDIUM if findings else Confidence(level=ConfidenceLevel.HIGH),
            reasoning="compared per-port recognized-but-unmodeled knobs baseline vs proposed",
        )
```

- [ ] **Step 5: Register + bump + allowlist**

In `checks/wired/__init__.py`: import `from .unmodeled_change import PortUnmodeledChangeCheck`; append to `ALL_WIRED_CHECKS`; add to `__all__`. In `tests/test_public_api.py` change `== 24` to `== 25`. In `scope/allowlist.py` add `"inter_switch_link"`, `"storm_control"`, `"enable_qos"` to `_MODELED_USAGE_ATTRS`.

Add scope test to `tests/scope/test_allowlist.py`:
```python
def test_misc_knobs_in_scope_usage_local_not_port_config():
    dev = set(RAW_ALLOWLIST["device"])
    for a in ("inter_switch_link", "storm_control", "enable_qos"):
        assert f"port_usages.*.{a}" in dev and f"local_port_config.*.{a}" in dev
        assert f"port_config.*.{a}" not in dev
        assert f"port_config_overwrite.*.{a}" not in dev
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/ir/test_port_misc.py tests/checks/test_unmodeled_change.py tests/test_public_api.py tests/scope/ -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc add \
  src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/switch.py \
  src/digital_twin/checks/wired/unmodeled_change.py src/digital_twin/checks/wired/__init__.py \
  tests/test_public_api.py src/digital_twin/scope/allowlist.py \
  tests/ir/test_port_misc.py tests/checks/test_unmodeled_change.py \
  tests/adapters/mist/test_ingest_switch.py tests/scope/test_allowlist.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc commit -m "$(cat <<'EOF'
feat: wired.port.unmodeled_change — inter_switch_link/storm_control/enable_qos -> REVIEW

PortMisc value object (None = all-default; lone flip detectable; storm_control
order-independent digest). Policy-floor check floors REVIEW on any knob change,
naming it; never SAFE/UNSAFE. Allowlisted on usage + local.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: End-to-end + goldens + ROADMAP

**Files:**
- Test: `tests/engine/test_pipeline.py`
- Modify: `docs/ROADMAP.md`

**Interfaces:**
- Consumes: the pipeline e2e harness (`dc_replace`/`_raw`/`FakeProvider`/`_plan`/`_op`/`simulate`/`Decision`/`SWITCH`/`SETTING`).

- [ ] **Step 1: Write failing e2e tests**

Add to `tests/engine/test_pipeline.py` (mirror prior SP e2e tests; set `no_local_overwrite: False` on the touched port up front):
```python
def test_mac_limit_lowered_below_clients_is_review():
    # a port with 2 observed wired clients; lowering mac_limit to 1 -> REVIEW.
    # CLIENTS_ACTIVE requires BOTH client keys in meta.fetched (ingest/clients.py);
    # _raw() ships fetched=("devices",), so widen it or this hits .unverified.
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw0 = _raw()
    raw = dc_replace(
        raw0, devices=(sw_a,),
        wired_clients=(
            {"device_mac": "aa0000000001", "port_id": "ge-0/0/0", "mac": "c1", "vlan": 10},
            {"device_mac": "aa0000000001", "port_id": "ge-0/0/0", "mac": "c2", "vlan": 10},
        ),
        meta=dc_replace(raw0.meta, fetched=("devices", "wired_clients", "wireless_clients")),
    )
    payload = {"local_port_config": {"ge-0/0/0": {"mac_limit": 1}}}
    v = simulate(_plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
                 provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert "wired.port.mac_limit_exceeded.exceeded" in codes, codes
    assert v.decision is Decision.REVIEW


def test_enable_qos_change_is_review_not_unknown():
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw = dc_replace(_raw(), devices=(sw_a,))
    payload = {"local_port_config": {"ge-0/0/0": {"enable_qos": True}}}
    v = simulate(_plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
                 provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    assert any(c.startswith("wired.port.unmodeled_change") for c in {f.code for f in v.findings})
    assert v.decision is Decision.REVIEW
```
NOTE: the `enable_qos` test needs no client data (PortMisc change is client-independent), so its plain `dc_replace(_raw(), devices=(sw_a,))` is fine. The `mac_limit` test's widened `meta.fetched` is what earns `CLIENTS_ACTIVE` — verify with a quick check that `.exceeded` (not `.unverified`) fires; if it still reports `.unverified`, the `meta.fetched` widening didn't take (re-check the `dc_replace(raw0.meta, ...)` call).

- [ ] **Step 2: Run to verify it fails for the right reason**

Run: `uv run pytest tests/engine/test_pipeline.py -k "mac_limit_lowered or enable_qos_change" -q`
Expected: FAIL on the ASSERTION, not import/setup.

- [ ] **Step 3: Confirm passes**

Tasks 1–4 deliver the behavior. Run the same; expected PASS. Debug per the NOTE if mac_limit client wiring is the blocker.

- [ ] **Step 4: Full golden suite**

Run: `uv run pytest tests/golden/ -q`
Expected: PASS. If a golden churns (most likely from voice-VLAN membership), STOP, diff it, confirm it's a true effect before re-pinning.

- [ ] **Step 5: ROADMAP entry**

In `docs/ROADMAP.md`, under the most recent completed entries:
```markdown
- ✅ Switch port-config misc — voip_network (voice VLAN) + mac_limit + recognized→REVIEW knobs (SP4, final of the port-config attribute-modeling program) — done 2026-06-26
```

- [ ] **Step 6: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc add \
  tests/engine/test_pipeline.py docs/ROADMAP.md
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp4-misc commit -m "$(cat <<'EOF'
test(portcfg-misc): e2e mac_limit + unmodeled-knob simulate (not UNKNOWN); roadmap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §1 voip full integration (voice_vlan + carriage + ACCESS membership + client.impact + gate) → Tasks 1–2. ✓
- §2 mac_limit (int|str|None normalizer; requires WIRED_L2; both-sides CLIENTS_ACTIVE; .exceeded/.unverified/.unresolved; capped REVIEW) → Task 3. ✓
- §3 inter_switch_link/storm_control/enable_qos PortMisc recognized→REVIEW → Task 4. ✓
- §4 registration + 23→25; no L0 change → Tasks 3 (→24), 4 (→25). ✓
- OAS placement (mac_limit overwrite+local+usage; others local+usage; none port_config) → scope tests in Tasks 2/3/4. ✓
- Owner review pins: client.impact voice-loss (T2), mac_limit requires WIRED_L2 + both-sides + baseline-blind .unverified (T3), ACCESS-gated voice membership + trunk-not-member (T1). ✓
- Plan-review round 2 pins: stale `mac_limit`-as-unmodeled tests repointed to `poe_keep_state_when_reboot`/`use_vstp` (T3 Step 6b); `MacLimitExceededCheck` strict-typed (`dict[str,list[Client]]`, typed `_mk`, `Cause`/`Client` imports); `voice_vlan_of` catches templated `vlan_id` → None (T1); `_storm_digest` normalizes OAS defaults away so a default-shaped object → `Port.misc is None` (T4). ✓
- Plan-review round 3 pins: client-dependent e2es widen `meta.fetched` (both client keys) so `CLIENTS_ACTIVE` is earned — else T5 `mac_limit` hits `.unverified` not `.exceeded`, T2 voip sees no active phone (T2/T5); single `-k` exprs; `test_field_gate.py` staged in T3. ✓
- Plan-review round 4 pin: T2 voip e2e moved to site-setting level and INJECTS a `phone` usage into the raw setting (`SETTING.port_usages` only has `office`/`uplink`; `SWITCH.port_config` is the `ge-0/0/0-1` range) + explicit `ge-0/0/0` phone port, so baseline actually resolves `voice_vlan=30` and the op (drop `voip_network` from the phone usage) drives `vlan_removed`. T5 templates already match the proven SP3 single-port + `no_local_overwrite:False` pattern with the defined `office` usage. ✓

**Placeholder scan:** No TBD/TODO. The client-dependent e2es (T2 voip, T5 mac_limit) ship complete templates that widen `meta.fetched` to `("devices","wired_clients","wireless_clients")` — verified against `ingest/clients.py` (CLIENTS_ACTIVE needs both client keys) and the wired-client dict shape (`device_mac`/`port_id`/`mac`/`vlan`). Remaining NOTEs only ask the implementer to align fixture names (usage/`device_id`) to this file's existing `SETTING`/`SWITCH`.

**Type consistency:** `Port.voice_vlan: int|None`, `Port.mac_limit: int|str|None`, `Port.misc: PortMisc|None`; `voice_vlan_of`/`_mac_limit`/`_storm_digest`/`_port_misc` signatures consistent; `_more_restrictive(old,new)` and `_changed(old,new)` typed; finding codes `wired.port.mac_limit_exceeded.{exceeded,unverified,unresolved}` and `wired.port.unmodeled_change.recognized` match impl↔tests; `_MISC_ATTRS` (5) consistent; `voip_network` removed from `_USAGE_OVERRIDE_ATTRS` and added to `_MISC_ATTRS`/`_LOCAL_ATTRS`; `mac_limit` in `_OVERWRITE_ATTRS`; `ALL_WIRED_CHECKS` 23→24→25 matches the verified current 23.
