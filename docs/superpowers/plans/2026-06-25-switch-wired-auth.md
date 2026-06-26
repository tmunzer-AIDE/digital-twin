# Switch Wired-Auth (802.1X / MAC-auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the whole switch wired-auth surface (14 leaves) in-scope under a policy-floor model — any auth-config change floors REVIEW, observed connected clients escalate (capped at REVIEW) — so these changes simulate instead of returning UNKNOWN.

**Architecture:** A `PortAuth` value object on `Port` captures the effective auth config (None ⇔ whole surface default/absent); switch ingest threads the 14 attrs from `local_port_config` + `port_usages` only; a `wired.auth.access_change` check floors REVIEW on any change and escalates from `ClientEnrichment` (made an enrich/cap consumer); the field gate allowlists the leaves via `_MODELED_USAGE_ATTRS`.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col, E/F/I), mypy-strict.

**Spec:** `docs/superpowers/specs/2026-06-25-switch-wired-auth-design.md`

## Global Constraints

- **No false-SAFE:** every in-scope auth change is modeled (floors REVIEW); an auth change never resolves SAFE.
- **No normalization that drops an allowlisted auth change:** `Port.auth = None` ⇔ the ENTIRE auth surface is default/absent (not merely `port_auth is None`); a lone `persist_mac`/`reauth_interval`/etc. change must wake the check.
- **Auth maps:** the 14 attrs are on `local_port_config` + `port_usages` ONLY — never `port_config`/`port_config_overwrite`. Resolver applies them from `local` only (+ usage via `usage_definition`); gate allowlists `port_usages.*` (site/device/networktemplate) + `local_port_config.*` (device only).
- **Cap at REVIEW:** the check never emits ERROR/UNSAFE — RADIUS/NAC outcomes are unknowable; escalation only adds evidence/confidence. `ClientEnrichment` is enrich/cap only, degrades gracefully when absent, never in `diff_ir`.
- **Gate after every task:** `uv run pytest -q && uv run ruff check . && uv run mypy src` — all green before commit (mypy not enforced on tests/; test-only Pyright noise — duck-typed providers, unused `_x`, stale-index, `**kw` — is not a gate failure).
- **Commits** end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Worktree:** work ONLY in `/Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth`; run git as `git -C <that-path> ...` and verify HEAD is on `worktree-feat+switch-wired-auth` with the expected parent before committing. NEVER touch the main checkout.

---

## File Structure

- **Modify** `src/digital_twin/ir/entities.py` — `PortAuth` dataclass, `Port.auth` field, `requires_auth`/`tightens` helpers, `ClientEnrichment` contract docstring. (Tasks 1, 3)
- **Modify** `src/digital_twin/ir/model.py` — `IR.client_enrichment` contract comment. (Task 3)
- **Modify** `src/digital_twin/adapters/mist/ingest/ports.py` — `_AUTH_ATTRS` added to `_LOCAL_ATTRS`. (Task 2)
- **Modify** `src/digital_twin/adapters/mist/ingest/switch.py` — `_port_auth` (+ `_reauth`) helper; wire `auth=` into the switch `Port`. (Task 2)
- **Create** `src/digital_twin/checks/wired/auth_change.py` — the check. (Task 3)
- **Modify** `src/digital_twin/checks/wired/__init__.py` — register. (Task 3)
- **Modify** `tests/test_public_api.py` — bump `len(ALL_WIRED_CHECKS) == 22 → 23`. (Task 3)
- **Modify** `src/digital_twin/scope/allowlist.py` — `_AUTH_ATTRS` into `_MODELED_USAGE_ATTRS`. (Task 4)
- **Tests:** `tests/ir/test_port_auth.py` (new), `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`, `tests/checks/test_auth_access_change.py` (new), `tests/scope/test_allowlist.py`, `tests/scope/test_field_gate.py`, `tests/engine/test_pipeline.py`, `docs/ROADMAP.md`.

---

## Task 1: IR `PortAuth` value object + `Port.auth` + `tightens`

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (add `PortAuth` near `Port`; add `Port.auth` field; add `requires_auth`/`tightens`)
- Test: `tests/ir/test_port_auth.py` (new)

**Interfaces:**
- Produces: `PortAuth` (frozen dataclass, 14 fields, defaults = OAS defaults); `Port.auth: PortAuth | None = None`; `requires_auth(a) -> bool`; `admitted_methods(a) -> frozenset[str] | None`; `_fallbacks(a) -> frozenset[str]`; `tightens(old, new) -> bool` (auth newly required OR newly mac-auth-only OR a fallback network removed).

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_port_auth.py`:

```python
from digital_twin.ir.entities import PortAuth, admitted_methods, requires_auth, tightens


def test_default_portauth_equality():
    assert PortAuth() == PortAuth()


def test_requires_auth():
    assert requires_auth(None) is False
    assert requires_auth(PortAuth()) is False
    assert requires_auth(PortAuth(port_auth="dot1x")) is True
    assert requires_auth(PortAuth(mac_auth=True)) is True
    assert requires_auth(PortAuth(mac_auth_only=True)) is True


def test_admitted_methods():
    assert admitted_methods(None) is None              # no auth -> all admitted
    assert admitted_methods(PortAuth()) is None
    assert admitted_methods(PortAuth(port_auth="dot1x")) == frozenset({"dot1x"})
    assert admitted_methods(PortAuth(port_auth="dot1x", mac_auth=True)) == frozenset({"dot1x", "mac"})
    # mac-auth-only rejects dot1x supplicants
    assert admitted_methods(PortAuth(port_auth="dot1x", mac_auth_only=True)) == frozenset({"mac"})


def test_tightens_newly_requires_auth():
    assert tightens(None, PortAuth(port_auth="dot1x")) is True
    assert tightens(PortAuth(), PortAuth(mac_auth=True)) is True
    # already required -> still required: not a tightening (no new requirement)
    assert tightens(PortAuth(port_auth="dot1x"), PortAuth(port_auth="dot1x", persist_mac=True)) is False
    # loosened: dropped auth
    assert tightens(PortAuth(port_auth="dot1x"), None) is False


def test_tightens_mac_auth_only_enabled():
    # dot1x -> dot1x + mac_auth_only: dot1x supplicants now rejected -> tightened
    assert tightens(PortAuth(port_auth="dot1x"),
                    PortAuth(port_auth="dot1x", mac_auth_only=True)) is True


def test_tightens_fallback_removed():
    # losing a guest/server-fail/server-reject fallback is a tightening
    assert tightens(PortAuth(port_auth="dot1x", guest_network="guest"),
                    PortAuth(port_auth="dot1x")) is True
    # gaining a fallback is NOT a tightening
    assert tightens(PortAuth(port_auth="dot1x"),
                    PortAuth(port_auth="dot1x", guest_network="guest")) is False


def test_persist_mac_only_is_non_default():
    # the false-SAFE guard: a persist_mac-only surface is NOT equal to the
    # all-default surface, so a change to it is detectable downstream
    assert PortAuth(persist_mac=True) != PortAuth()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ir/test_port_auth.py -q`
Expected: FAIL — `ImportError: cannot import name 'PortAuth'`.

- [ ] **Step 3: Implement `PortAuth` + helpers**

In `src/digital_twin/ir/entities.py`, add (immediately before the `Port` dataclass; defaults are the OAS defaults so `PortAuth()` is the canonical all-default surface):

```python
@dataclass(frozen=True)
class PortAuth:
    """Effective wired-auth config for a switch port (SP3). Frozen + comparable:
    change-detection is plain inequality. Defaults are the OAS defaults, so
    PortAuth() is the canonical all-default surface — Port.auth is None ONLY when
    the whole surface is default/absent (a lone persist_mac/reauth change is a
    non-default PortAuth, never collapsed to None)."""

    port_auth: str | None = None          # "dot1x" | None
    mac_auth: bool = False                 # enable_mac_auth
    mac_auth_only: bool = False
    mac_auth_preferred: bool = False
    mac_auth_protocol: str = "eap-md5"     # OAS default
    allow_multiple_supplicants: bool = False
    dynamic_vlan_networks: tuple[str, ...] = ()
    server_fail_network: str | None = None
    server_reject_network: str | None = None
    guest_network: str | None = None
    bypass_auth_when_server_down: bool = False
    bypass_auth_when_server_down_for_unknown_client: bool = False
    persist_mac: bool = False
    reauth_interval: str | None = None     # canonical (see ingest _reauth)


def requires_auth(a: PortAuth | None) -> bool:
    """The port forces clients to authenticate (dot1x or MAC-auth)."""
    return a is not None and (a.port_auth == "dot1x" or a.mac_auth or a.mac_auth_only)


def admitted_methods(a: PortAuth | None) -> frozenset[str] | None:
    """The auth methods the port admits. None = no auth required (all clients
    admitted). Else a subset of {"dot1x", "mac"}."""
    if not requires_auth(a):
        return None
    assert a is not None
    m: set[str] = set()
    if a.mac_auth or a.mac_auth_only:
        m.add("mac")
    if a.port_auth == "dot1x" and not a.mac_auth_only:
        m.add("dot1x")
    return frozenset(m)


def _fallbacks(a: PortAuth | None) -> frozenset[str]:
    if a is None:
        return frozenset()
    return frozenset(
        n for n in (a.guest_network, a.server_fail_network, a.server_reject_network) if n
    )


def tightens(old: PortAuth | None, new: PortAuth | None) -> bool:
    """Admission became more restrictive in a way that could block currently-
    admitted clients: auth newly required, OR became MAC-auth-only (dot1x
    supplicants now rejected), OR a fallback network (guest/server_fail/
    server_reject) was removed."""
    new_only = new.mac_auth_only if new is not None else False
    old_only = old.mac_auth_only if old is not None else False
    return (
        (requires_auth(new) and not requires_auth(old))
        or (new_only and not old_only)
        or bool(_fallbacks(old) - _fallbacks(new))
    )
```

Add the field to `Port` (next to the other config-intent fields, e.g. after `disabled`):
```python
    auth: PortAuth | None = None  # SP3: effective wired-auth surface; None = all-default
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/ir/test_port_auth.py -q`
Expected: PASS.

- [ ] **Step 5: Gate + commit**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS (goldens unchanged — nothing populates `Port.auth` yet).

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth add \
  src/digital_twin/ir/entities.py tests/ir/test_port_auth.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth commit -m "$(cat <<'EOF'
feat(ir): PortAuth value object + Port.auth + requires_auth/tightens (SP3)

PortAuth captures the effective wired-auth surface (OAS defaults); Port.auth is
None ONLY when the whole surface is default/absent, so a persist_mac-only change
is detectable. tightens() = auth newly required.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Ingest — resolver threading + `_port_auth` normalization

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py` (`_AUTH_ATTRS` → `_LOCAL_ATTRS`)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_reauth`, `_port_auth`; wire `auth=`)
- Test: `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`

**Interfaces:**
- Consumes: Task 1's `PortAuth`/`Port.auth`; the SP2 resolver layers (`_LOCAL_ATTRS` applied from `local`, usage via `usage_definition`).
- Produces: `_AUTH_ATTRS` (the 14 names); `_port_auth(usage: dict) -> PortAuth | None`; `_reauth(v) -> str | None`.

- [ ] **Step 1: Write failing tests**

Add to `tests/adapters/mist/test_ingest_ports.py`:

```python
def test_auth_resolves_from_usage_and_local_not_port_config():
    # auth flows from the named usage and from local_port_config; a port_config
    # (or overwrite) auth key is NOT honored (OAS: not on those maps)
    eff = _eff(
        port_usages={"office": {"mode": "access", "port_network": "corp"},
                     "secure": {"mode": "access", "port_network": "corp", "port_auth": "dot1x"}},
        port_config={"ge-0/0/1": {"usage": "secure"},
                     "ge-0/0/2": {"usage": "office", "port_auth": "dot1x"},  # ignored (pc)
                     "ge-0/0/3": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/3": {"enable_mac_auth": True}},
    )
    r = _resolved(eff)
    assert r["ge-0/0/1"][0].get("port_auth") == "dot1x"          # from usage
    assert r["ge-0/0/2"][0].get("port_auth") is None             # port_config auth ignored
    assert r["ge-0/0/3"][0].get("enable_mac_auth") is True       # from local
```

Add to `tests/adapters/mist/test_ingest_switch.py` (mirror the `test_l1_config_*` scaffold — `eff` dict + `IngestContext(raw=raw_site(devices=(...,)), site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder())`):

```python
def test_port_auth_normalization_and_none_when_default():
    from digital_twin.adapters.mist.ingest.switch import _port_auth, _reauth
    from digital_twin.ir.entities import PortAuth
    # all-default surface -> None
    assert _port_auth({"mode": "access", "port_network": "corp"}) is None
    # persist_mac-only -> non-None (false-SAFE guard)
    assert _port_auth({"persist_mac": True}) == PortAuth(persist_mac=True)
    # reauth: 36000 (int) and "36000" (numeric str) canonicalize equal; "" -> None;
    # object -> stable token (never silently None)
    assert _reauth(36000) == _reauth("36000") == "36000"
    assert _reauth("") is None and _reauth(None) is None
    assert _reauth({"x": 1}) is not None  # stable token, NOT collapsed to None


def test_reauth_65000_int_equals_str():
    from digital_twin.adapters.mist.ingest.switch import _reauth
    assert _reauth(65000) == _reauth("65000") == "65000"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py -k auth tests/adapters/mist/test_ingest_switch.py -k "port_auth or reauth" -q`
Expected: FAIL (auth not threaded; `_port_auth`/`_reauth` undefined).

- [ ] **Step 3: Thread `_AUTH_ATTRS` through the resolver (local only)**

In `src/digital_twin/adapters/mist/ingest/ports.py`, **replace the existing
`_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled")` line with these two
definitions, IN THIS ORDER** (`_AUTH_ATTRS` MUST be defined before `_LOCAL_ATTRS`
references it, or the module import `NameError`s):

```python
# Wired-auth attrs (SP3): OAS-present on local_port_config + port_usages ONLY
# (never port_config / port_config_overwrite). Applied from local here; usage-
# level auth flows via usage_definition. NOT added to _USAGE_OVERRIDE_ATTRS
# (that is the port_config inline layer).
_AUTH_ATTRS = (
    "port_auth", "enable_mac_auth", "mac_auth_only", "mac_auth_preferred",
    "mac_auth_protocol", "allow_multiple_supplicants", "dynamic_vlan_networks",
    "server_fail_network", "server_reject_network", "guest_network",
    "bypass_auth_when_server_down", "bypass_auth_when_server_down_for_unknown_client",
    "persist_mac", "reauth_interval",
)
_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled", *_AUTH_ATTRS)
```

- [ ] **Step 4: Add `_reauth` + `_port_auth` and wire into the switch Port**

In `src/digital_twin/adapters/mist/ingest/switch.py`, add next to `_l1_config` (import `PortAuth` from `digital_twin.ir.entities` — it's likely already importing `Port`; add `PortAuth`):

```python
def _reauth(v: Any) -> str | None:
    """Canonical reauth_interval: None for null/empty; the decimal string for an
    int or numeric string (so 65000 == "65000"); a stable token otherwise — never
    silently collapse an unparseable non-empty value to None (it must stay
    change-detecting). bool is not a valid interval."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return s if s.isdigit() else (f"raw:{s}" if s else None)
    return f"raw:{v!r}"


def _port_auth(usage: dict[str, Any]) -> PortAuth | None:
    """Effective wired-auth surface from the resolved usage attrs. None iff the
    whole surface is default/absent (== PortAuth())."""
    a = PortAuth(
        port_auth=usage.get("port_auth") or None,
        mac_auth=bool(usage.get("enable_mac_auth")),
        mac_auth_only=bool(usage.get("mac_auth_only")),
        mac_auth_preferred=bool(usage.get("mac_auth_preferred")),
        mac_auth_protocol=usage.get("mac_auth_protocol") or "eap-md5",
        allow_multiple_supplicants=bool(usage.get("allow_multiple_supplicants")),
        dynamic_vlan_networks=tuple(usage.get("dynamic_vlan_networks") or ()),
        server_fail_network=usage.get("server_fail_network") or None,
        server_reject_network=usage.get("server_reject_network") or None,
        guest_network=usage.get("guest_network") or None,
        bypass_auth_when_server_down=bool(usage.get("bypass_auth_when_server_down")),
        bypass_auth_when_server_down_for_unknown_client=bool(
            usage.get("bypass_auth_when_server_down_for_unknown_client")
        ),
        persist_mac=bool(usage.get("persist_mac")),
        reauth_interval=_reauth(usage.get("reauth_interval")),
    )
    return a if a != PortAuth() else None
```
In the switch `Port(...)` construction (the `_switch_ports_and_l3` loop), compute before `ctx.builder.add_port(`:
```python
            auth = _port_auth(usage)
```
and add the kwarg inside `Port(...)` (next to `autoneg_disabled=...`):
```python
                    auth=auth,
```

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS (goldens unchanged — no check consumes `Port.auth` yet).

- [ ] **Step 6: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth add \
  src/digital_twin/adapters/mist/ingest/ports.py \
  src/digital_twin/adapters/mist/ingest/switch.py \
  tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth commit -m "$(cat <<'EOF'
feat(ingest): resolve wired-auth into Port.auth (local + usage only)

_AUTH_ATTRS applied from local_port_config (+ usage via usage_definition), never
port_config/overwrite. _port_auth normalizes the surface (None iff all-default);
_reauth canonicalizes (65000=="65000", ""->None, object->stable token, no silent
collapse).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Check `wired.auth.access_change` + registration + ClientEnrichment contract

**Files:**
- Create: `src/digital_twin/checks/wired/auth_change.py`
- Modify: `src/digital_twin/checks/wired/__init__.py`, `tests/test_public_api.py`
- Modify: `src/digital_twin/ir/entities.py` (`ClientEnrichment` docstring), `src/digital_twin/ir/model.py` (`IR.client_enrichment` comment)
- Test: `tests/checks/test_auth_access_change.py` (new)

**Interfaces:**
- Consumes: `Port.auth`, `admitted_methods`, `tightens` (entities); `Client`, `PortAuth` (types); `clients_by_port`; `ctx.baseline.ir.client_enrichment.get(client.id)`; `CheckContext`, `min_confidence`.
- Produces: `AuthAccessChangeCheck` (`id="wired.auth.access_change"`); codes `.policy_change`, `.clients_at_risk`.

- [ ] **Step 1: Write the failing check tests**

Create `tests/checks/test_auth_access_change.py`:

```python
"""wired.auth.access_change: any in-scope auth change floors REVIEW (admission
impact depends on RADIUS/NAC, not modeled). Observed connected clients escalate
detail/confidence when admission tightens — capped at REVIEW, never UNSAFE.
No enrichment -> still REVIEW (floor)."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.auth_change import AuthAccessChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import ClientEnrichment, PortAuth
from tests.factories import sw, wired_client


def _ir(auth, *, client=None, enrich=None):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, auth=auth))
    if client is not None:
        b.add_client(client)
        if enrich is not None:
            b.set_client_enrichment({client.id: enrich})
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return AuthAccessChangeCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def test_gaining_dot1x_floors_review():
    r = _run(_ir(None), _ir(PortAuth(port_auth="dot1x")))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.auth.access_change.policy_change"
    assert f.severity is Severity.WARNING


def test_persist_mac_only_change_floors_review():
    # the false-SAFE guard: persist_mac-only (no port_auth) still surfaces
    r = _run(_ir(None), _ir(PortAuth(persist_mac=True)))
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.auth.access_change.policy_change"


def test_no_change_is_silent():
    assert _run(_ir(PortAuth(port_auth="dot1x")), _ir(PortAuth(port_auth="dot1x"))).findings == ()
    assert _run(_ir(None), _ir(None)).findings == ()


def test_tightening_with_unauth_client_escalates_but_caps_at_review():
    c = wired_client("cc:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="unauthenticated")
    base = _ir(None, client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert f.severity is Severity.WARNING       # capped at REVIEW, never ERROR
    assert "cc:01" in f.affected_entities or any("cc:01" in str(v) for v in f.evidence.values())
    assert r.status is Status.WARN              # never FAIL/UNSAFE


def test_no_enrichment_still_reviews_floor_only():
    c = wired_client("cc:02", "S:ge-0/0/1", vlan=10)
    base = _ir(None, client=c)            # client present, NO enrichment
    prop = _ir(PortAuth(port_auth="dot1x"), client=c)
    r = _run(base, prop)
    assert r.status is Status.WARN
    # degrades to the floor; no clients_at_risk without enrichment evidence
    assert all(f.code == "wired.auth.access_change.policy_change" for f in r.findings)


def test_base_only_port_auth_loss_surfaces():
    # a port present ONLY in baseline (e.g. its local port_auth entry was deleted)
    # must still surface the auth LOSS — union iteration, missing side = None
    base = _ir(PortAuth(port_auth="dot1x"))
    prop = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2).build()
    r = _run(base, prop)
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.auth.access_change.policy_change"


def test_mac_auth_only_drops_dot1x_client():
    # dot1x -> mac-auth-only: a client authenticated via dot1x is no longer
    # admitted (method dropped) -> escalates, capped at REVIEW
    c = wired_client("dd:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="authenticated", auth_method="dot1x")
    base = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x", mac_auth_only=True), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert "dd:01" in f.affected_entities
    assert f.severity is Severity.WARNING and r.status is Status.WARN  # capped


def test_guest_removal_with_guest_client_escalates():
    # removing a guest fallback while a guest-state client is connected -> at risk
    c = wired_client("ee:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="guest")
    base = _ir(PortAuth(port_auth="dot1x", guest_network="guest"), client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert "ee:01" in f.affected_entities
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_auth_access_change.py -q`
Expected: FAIL — `ModuleNotFoundError: ...auth_change`.

- [ ] **Step 3: Implement the check**

Create `src/digital_twin/checks/wired/auth_change.py`:

```python
"""wired.auth.access_change — a switch port's wired-auth admission policy changed.

Enabling/changing 802.1X or MAC-auth (and the RADIUS fallback / dynamic-VLAN
knobs) governs whether clients are admitted and onto which VLAN — outcomes that
depend on the RADIUS server and org NAC rules, which the twin cannot observe or
simulate. So this check does NOT predict pass/fail or the landed VLAN. It floors
REVIEW on any auth-surface change (policy_change), and — when admission TIGHTENS
and currently-connected wired clients are observed in a state the change would
block — escalates detail/confidence (clients_at_risk), capped at REVIEW (RADIUS
could still admit them). Never SAFE, never UNSAFE.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import Client, PortAuth, admitted_methods, tightens
from digital_twin.ir.indexes import clients_by_port

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_INFERRED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("admission outcome depends on RADIUS/NAC, which the twin cannot observe",),
)
# observed auth_state values that mean a client is NOT currently authenticated,
# so a newly-required auth would put it at risk
_UNAUTH_STATES = frozenset({"unauthenticated", "unauthorized", "rejected", "failed", "guest"})


def _norm_method(observed: str | None) -> str:
    """Normalize an observed ClientEnrichment.auth_method to {"dot1x","mac",""}."""
    s = (observed or "").lower()
    if "dot1x" in s or "802.1" in s:
        return "dot1x"
    if "mac" in s:
        return "mac"
    return ""


class AuthAccessChangeCheck:
    id = "wired.auth.access_change"
    title = "Wired-auth admission policy changed"
    domain = "wired.auth"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port") or diff.touches("client")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        wired = clients_by_port(base_ir)
        findings: list[Finding] = []
        # union of port ids: a base-only port (e.g. a local-only port whose
        # port_auth-bearing entry was deleted) must surface its auth LOSS too —
        # the missing side is None.
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            base_port = base_ir.ports.get(pid)
            prop_port = prop_ir.ports.get(pid)
            old = base_port.auth if base_port is not None else None
            new = prop_port.auth if prop_port is not None else None
            if old == new:
                continue  # no auth-surface change
            at_risk = self._clients_at_risk(ctx, pid, old, new, wired)
            findings.append(self._finding(ctx, pid, at_risk))
        worst = Status.WARN if findings else Status.PASS
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared per-port wired-auth surface baseline vs proposed",
        )

    def _clients_at_risk(
        self,
        ctx: CheckContext,
        pid: str,
        old: PortAuth | None,
        new: PortAuth | None,
        wired: dict[str, list[Client]],
    ) -> list[str]:
        """Currently-connected wired clients a tightening would block: observed
        un-authenticated (auth newly required), OR authenticated by a method the
        new config no longer admits (e.g. a dot1x client when the port moves to
        MAC-auth-only). Enrich/cap only — absence of enrichment degrades to []."""
        if not tightens(old, new):
            return []
        admitted = admitted_methods(new)  # None = no auth required (no one blocked by method)
        out: list[str] = []
        for c in wired.get(pid, []):
            ce = ctx.baseline.ir.client_enrichment.get(c.id)
            if ce is None:
                continue  # no observed evidence -> floor only
            unauth = (ce.auth_state or "").lower() in _UNAUTH_STATES
            method = _norm_method(ce.auth_method)
            method_dropped = admitted is not None and method != "" and method not in admitted
            if unauth or method_dropped:
                out.append(c.mac)
        return out

    def _finding(self, ctx: CheckContext, pid: str, at_risk: list[str]) -> Finding:
        cause = tuple(c for c in (ctx.delta_index.cause("port", pid),) if c is not None)
        if at_risk:
            return Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{self.id}.clients_at_risk",
                severity=Severity.WARNING,  # capped at REVIEW — RADIUS may still admit
                confidence=_HIGH,           # observed un-auth clients = direct evidence of risk
                message=(
                    f"port {pid}: wired-auth now required; {len(at_risk)} connected client(s) "
                    f"observed un-authenticated may be blocked (RADIUS/NAC outcome not modeled)"
                ),
                affected_entities=tuple(at_risk),
                subject=ObjectRef("port", pid),
                evidence={"port": pid, "clients_at_risk": at_risk},
                caused_by=cause,
            )
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=f"{self.id}.policy_change",
            severity=Severity.WARNING,
            confidence=_INFERRED,
            message=(
                f"port {pid}: wired-auth admission policy changed — access impact "
                "depends on RADIUS/NAC and is not modeled (review)"
            ),
            affected_entities=(pid,),
            subject=ObjectRef("port", pid),
            evidence={"port": pid},
            caused_by=cause,
        )
```

- [ ] **Step 4: Run the check tests**

Run: `uv run pytest tests/checks/test_auth_access_change.py -q`
Expected: PASS.

- [ ] **Step 5: Register the check + bump public API**

In `src/digital_twin/checks/wired/__init__.py`: add the import (alphabetical, near the top):
```python
from .auth_change import AuthAccessChangeCheck
```
Append to `ALL_WIRED_CHECKS` (after `AdminDisableCheck()` — sibling port-config check):
```python
    AuthAccessChangeCheck(),
```
Add to `__all__`:
```python
    "AuthAccessChangeCheck",
```
In `tests/test_public_api.py` change `assert len(ALL_WIRED_CHECKS) == 22` to `== 23` (verify it is currently 22 first).

- [ ] **Step 6: Update the ClientEnrichment contract**

In `src/digital_twin/ir/entities.py`, replace the `ClientEnrichment` docstring lines "Evidence ONLY — never read by verdict logic, never in diff_ir." with:
```python
    """OBSERVATIONAL per-client identity. Best-effort, non-diff-bearing (never in
    diff_ir). MAY enrich or cap a finding (e.g. wired.auth.access_change naming
    at-risk clients), but never ORIGINATES or floors a verdict, and its absence
    must degrade gracefully."""
```
In `src/digital_twin/ir/model.py`, update the comment on the `client_enrichment` field (line ~72) to match: `# observational, enrich/cap only (see ClientEnrichment); never in diff_ir`.

- [ ] **Step 7: Run tests + gate**

Run: `uv run pytest tests/checks/test_auth_access_change.py tests/test_public_api.py tests/checks/test_registry.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. (No golden churn — auth leaves still UNKNOWN at the gate until Task 4, so no auth delta reaches the check via the pipeline yet; the check only fires on IR auth deltas, which the goldens don't carry.)

- [ ] **Step 8: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth add \
  src/digital_twin/checks/wired/auth_change.py src/digital_twin/checks/wired/__init__.py \
  tests/test_public_api.py tests/checks/test_auth_access_change.py \
  src/digital_twin/ir/entities.py src/digital_twin/ir/model.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth commit -m "$(cat <<'EOF'
feat(checks): wired.auth.access_change — policy-floor + observed escalation

Any wired-auth surface change floors REVIEW (admission impact depends on
RADIUS/NAC, not modeled); tightening + observed un-authenticated connected
clients escalates detail/confidence, capped at REVIEW. ClientEnrichment is now
an enrich/cap consumer (graceful absence); contract docstrings updated.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Field gate — allowlist the auth leaves (precise scope)

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (`_AUTH_ATTRS` → `_MODELED_USAGE_ATTRS`)
- Test: `tests/scope/test_allowlist.py`, `tests/scope/test_field_gate.py`

**Interfaces:**
- Consumes: `_MODELED_USAGE_ATTRS` feeds `_USAGE_LEAVES` (port_usages → site/device/networktemplate) AND `_LOCAL_PORT_CONFIG_LEAVES` (→ `_DEVICE_PORT_LEAVES` → device only). `_PORT_CONFIG_ATTRS`/`_OVERWRITE_LEAVES` are NOT touched (auth absent there).
- Produces: `port_usages.*.<auth>` in scope for site/device/networktemplate; `local_port_config.*.<auth>` in scope for device only; `port_config.*`/`port_config_overwrite.*` auth stays UNKNOWN.

- [ ] **Step 1: Write failing scope tests (the user's pins)**

Add to `tests/scope/test_allowlist.py`:

```python
def test_auth_usage_leaves_in_scope_everywhere_usages_live():
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
    for coll in (RAW_ALLOWLIST["site_setting"], RAW_ALLOWLIST["device"],
                 RAW_ALLOWLIST["networktemplate"], EFFECTIVE_ALLOWLIST):
        s = set(coll)
        for a in ("port_auth", "enable_mac_auth", "dynamic_vlan_networks", "guest_network"):
            assert f"port_usages.*.{a}" in s, a


def test_auth_local_leaves_device_only():
    assert "local_port_config.*.port_auth" in set(RAW_ALLOWLIST["device"])
    # site_setting / networktemplate have NO local_port_config map
    assert "local_port_config.*.port_auth" not in set(RAW_ALLOWLIST["site_setting"])
    assert "local_port_config.*.port_auth" not in set(RAW_ALLOWLIST["networktemplate"])


def test_auth_not_on_port_config_or_overwrite():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_config.*.port_auth" not in dev
    assert "port_config_overwrite.*.port_auth" not in dev
```

Add to `tests/scope/test_field_gate.py`:

```python
def test_auth_in_port_config_or_overwrite_is_unknown():
    # auth attrs are NOT on port_config/overwrite (OAS) -> a change there is
    # out-of-scope (UNKNOWN), even though local/usage auth is modeled
    for payload in (
        {**SWITCH_CUR, "port_config": {"ge-0/0/0": {"usage": "office", "port_auth": "dot1x"}}},
        {**SWITCH_CUR, "port_config_overwrite": {"ge-0/0/0": {"port_auth": "dot1x"}}},
    ):
        r = screen_op("device", SWITCH_CUR, payload)
        assert isinstance(r, Rejection)
        assert any("port_auth" in reason for reason in r.reasons)


def test_auth_in_local_port_config_passes_for_device():
    payload = {**SWITCH_CUR, "local_port_config": {"ge-0/0/0": {"port_auth": "dot1x"}}}
    assert screen_op("device", SWITCH_CUR, payload) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/scope/test_allowlist.py -k auth tests/scope/test_field_gate.py -k auth -q`
Expected: FAIL (auth leaves not yet allowlisted; local auth currently rejected).

- [ ] **Step 3: Add `_AUTH_ATTRS` to `_MODELED_USAGE_ATTRS`**

In `src/digital_twin/scope/allowlist.py`, define a named tuple just above `_MODELED_USAGE_ATTRS` and splice it in:

```python
# Wired-auth attrs (SP3) — modeled by wired.auth.access_change (policy-floor).
# OAS-present on port_usages + local_port_config only, so routing them through
# _MODELED_USAGE_ATTRS puts port_usages.* in scope for site/device/networktemplate
# and local_port_config.* in scope for device only — and nothing on
# port_config/port_config_overwrite (auth is absent from those maps).
_AUTH_ATTRS: tuple[str, ...] = (
    "port_auth", "enable_mac_auth", "mac_auth_only", "mac_auth_preferred",
    "mac_auth_protocol", "allow_multiple_supplicants", "dynamic_vlan_networks",
    "server_fail_network", "server_reject_network", "guest_network",
    "bypass_auth_when_server_down", "bypass_auth_when_server_down_for_unknown_client",
    "persist_mac", "reauth_interval",
)
_MODELED_USAGE_ATTRS: tuple[str, ...] = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "poe_disabled",
    "mtu",
    "allow_dhcpd",
    "speed",
    "duplex",
    "disable_autoneg",
    *_AUTH_ATTRS,
)
```
Do NOT touch `_PORT_CONFIG_ATTRS` or `_OVERWRITE_LEAVES`.

- [ ] **Step 4: Run tests + gate; reconcile stragglers**

Run: `uv run pytest tests/scope/ -q`
Expected: PASS. If an existing test used an auth attr as an "unmodeled usage/local leaf" example and now fails, retarget it to a still-unmodeled attr (`mac_limit`), same as SP2's reconciliation — do NOT weaken the gate.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. If a golden churns, a real `site.json` usage carries an auth attr now in scope → the auth check fires → STOP and confirm the finding is a true auth change (REVIEW) before re-pinning.

- [ ] **Step 5: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth add \
  src/digital_twin/scope/allowlist.py tests/scope/test_allowlist.py tests/scope/test_field_gate.py
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth commit -m "$(cat <<'EOF'
feat(scope): allowlist wired-auth leaves (port_usages site/device/nettemplate; local device-only)

Via _MODELED_USAGE_ATTRS, so auth is in scope on port_usages.* (everywhere usages
live) and local_port_config.* (device only), and stays UNKNOWN on
port_config/port_config_overwrite (absent from those OAS maps). In scope now that
wired.auth.access_change models them.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: End-to-end pipeline + goldens + ROADMAP

**Files:**
- Test: `tests/engine/test_pipeline.py`
- Modify: `docs/ROADMAP.md`

**Interfaces:**
- Consumes: the pipeline e2e harness (`dc_replace`, `_raw`, `FakeProvider`, `_plan`, `_op`, `simulate`, `Decision`, `SWITCH`, `SETTING`) used by SP1/SP2 e2e tests.

- [ ] **Step 1: Write the failing e2e test**

Add to `tests/engine/test_pipeline.py`. The delta enables `port_auth=dot1x` on a port (via `local_port_config`, which is device-scoped and in-scope) that has a connected wired client → not UNKNOWN, `wired.auth.access_change.*` present, REVIEW:

```python
def test_local_port_auth_change_is_simulated_not_unknown():
    # enabling dot1x via local_port_config on a configured access port must
    # SIMULATE (REVIEW via wired.auth.access_change), not return UNKNOWN.
    # ge-0/0/0 must be locally-overridable up front (no_local_overwrite defaults
    # True, which would discard the local auth), so the local port_auth
    # deterministically reaches the resolver/check.
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw = dc_replace(_raw(), devices=(sw_a,))
    payload = {"local_port_config": {"ge-0/0/0": {"port_auth": "dot1x"}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(raw=raw),
    )
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert any(c.startswith("wired.auth.access_change") for c in codes), codes
    assert v.decision is Decision.REVIEW, v.decision
```

NOTE: use this file's real helpers (SP1/SP2 added device-update e2e tests — copy that scaffold). `SETTING`'s `office` usage is access/corp. The `no_local_overwrite: False` above is present in BOTH baseline (`sw_a`) and the effective proposed (the payload only adds `local_port_config`, leaving the port_config entry intact), so the only effective change is the added `local_port_config.ge-0/0/0.port_auth` — which the gate accepts (Task 4) and the resolver applies (Task 2).

- [ ] **Step 2: Run to verify it fails for the right reason**

Run: `uv run pytest tests/engine/test_pipeline.py -k local_port_auth -q`
Expected: FAIL on the ASSERTION (decision/finding), not import/setup. Fix scaffold wiring if it errors.

- [ ] **Step 3: Confirm it passes**

Tasks 1–4 deliver the behavior. Run: `uv run pytest tests/engine/test_pipeline.py -k local_port_auth -q`
Expected: PASS. If it fails on the assertion, debug whether the local auth leaf is in-scope (Task 4), resolved (Task 2), and reaches the check.

- [ ] **Step 4: Run the FULL golden suite; investigate churn before re-pinning**

Run: `uv run pytest tests/golden/ -q`
Expected: PASS. If a golden's `site.json` usage carries an auth attr, the check now fires REVIEW on any baseline-vs-proposed auth delta — but goldens with NO auth change should be unaffected (the check is silent on equal auth). **If a golden churns, STOP and diff it** — confirm it's a true auth change before updating expected output.

- [ ] **Step 5: ROADMAP entry**

In `docs/ROADMAP.md`, add under the most recent completed entries:
```markdown
- ✅ Switch wired-auth (802.1X / MAC-auth, whole surface, policy-floor) (SP3 of the port-config attribute-modeling program) — done 2026-06-25
```

- [ ] **Step 6: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth add \
  tests/engine/test_pipeline.py docs/ROADMAP.md
git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/sp3-auth commit -m "$(cat <<'EOF'
test(auth): e2e local_port_config port_auth change simulates (not UNKNOWN); roadmap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §1 IR PortAuth (None ⇔ all-default) + tightens → Task 1. ✓
- §2 ingest (resolver local-only threading; `_port_auth` normalization; `_reauth` canonical) → Task 2. ✓
- §3 check (policy-floor + observed escalation, cap at REVIEW, graceful absence) → Task 3. ✓
- §4 field gate (port_usages site/device/nettemplate; local device-only; not pc/overwrite) → Task 4. ✓
- §5 ClientEnrichment contract → Task 3 Step 6. ✓
- §6 registration + public-api 22→23; L0 no change → Task 3. ✓

**User-required pins:**
1. auth in port_config/overwrite stays UNKNOWN → `test_auth_in_port_config_or_overwrite_is_unknown` + `test_auth_not_on_port_config_or_overwrite` (Task 4). ✓
2. local.*.auth accepted device / rejected site+nettemplate → `test_auth_local_leaves_device_only` + `test_auth_in_local_port_config_passes_for_device` (Task 4). ✓
3. reauth 65000=="65000", `{}` no collapse → `test_reauth_65000_int_equals_str` + `test_port_auth_normalization_and_none_when_default` (Task 2). ✓
4. persist_mac-only change emits policy-floor → `test_persist_mac_only_change_floors_review` (Task 3) + `test_persist_mac_only_is_non_default` (Task 1) + ingest non-None (Task 2). ✓
5. no enrichment → still REVIEW; enrichment adds evidence/confidence, never UNSAFE → `test_no_enrichment_still_reviews_floor_only` + `test_tightening_with_unauth_client_escalates_but_caps_at_review` (Task 3). ✓

**Placeholder scan:** No TBD/TODO. The Task-5 NOTE points at the real SP1/SP2 e2e scaffold to mirror; surrounding code complete.

**Type consistency:** `PortAuth` 14 fields + defaults identical across entities.py / `_port_auth` / tests; `Port.auth: PortAuth | None`; `requires_auth`/`admitted_methods`/`_fallbacks`/`tightens` signatures consistent and fully annotated; the check's `run` (union iteration), `_clients_at_risk(ctx, pid, old, new, wired) -> list[str]`, and `_finding(ctx, pid, at_risk) -> Finding` are fully annotated (mypy-strict on src); finding codes `wired.auth.access_change.{policy_change,clients_at_risk}` match impl↔tests; `_AUTH_ATTRS` (14) identical in ports.py and allowlist.py and defined ABOVE `_LOCAL_ATTRS`; `ALL_WIRED_CHECKS` 22→23 matches the verified count; `_reauth` returns `str | None` everywhere.

**Review-round fixes:**
- **P1** — check iterates `base_ir.ports.keys() | prop_ir.ports.keys()` (base-only auth loss surfaces); regression `test_base_only_port_auth_loss_surfaces`.
- **P1** — `_AUTH_ATTRS` defined before `_LOCAL_ATTRS` (no import NameError).
- **P1** — `_clients_at_risk`/`_finding` fully type-annotated (mypy-strict).
- **P2** — `tightens` broadened (mac-auth-only / fallback removal) + `admitted_methods`/`_norm_method` method-drop escalation; tests `test_tightens_mac_auth_only_enabled`, `test_tightens_fallback_removed`, `test_mac_auth_only_drops_dot1x_client`, `test_guest_removal_with_guest_client_escalates`.
- **P3** — e2e sets `no_local_overwrite: False` up front so local auth deterministically reaches the check.
