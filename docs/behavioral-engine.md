# Behavioral engine: implementation milestone 1

Implemented on **2026-10-01**, alongside the existing check engine. The importable
`digital_twin.behavioral` package now provides immutable configuration snapshots,
organization/site-scoped batches, exact structural coverage receipts, symbolic
transfer programs, property evaluation, and bounded rollout exploration. It also
contains bounded EX, SRX, and SSR primitives informed by the Juniper research.

**This is the foundation, not a complete live Mist full-stack twin.** There is no
production Mist-to-behavior compiler yet. The existing CLI/MCP verdict path still
uses the existing engine. The new package adds no dependencies, creates no API
session on import, and never applies a change to Mist.

## Run the executable example

From the repository, using its existing Python 3.14 environment:

```sh
uv sync --frozen
uv run --frozen python -m digital_twin.behavioral.demo
```

The example uses a deliberately small **synthetic reference grammar**, not Mist
configuration. An organization-wide supplied admission gate feeds two sites;
each site composes a WLAN VLAN assignment and EX egress carriage. The property
ends at VLAN carriage, rather than claiming successful authentication or internet
access. A WLAN move from VLAN 10 to 20 and its matching wired change produce:

| Configuration state | Site A carriage | Site B carriage |
| --- | --- | --- |
| Baseline | satisfied | satisfied |
| Both changes active | satisfied | satisfied |
| WLAN change alone active | violated | satisfied |
| Wired change alone active | violated | satisfied |

The intermediate failures emerge from the composed transfers. There is no
detector specifically named for that change combination. Changing the shared
admission gate also changes both sites; tests verify that propagation.

## Import and use the comparison API

```python
from digital_twin.behavioral import Batch, ObjectKey, Operation, Twin
from digital_twin.behavioral.demo import ReferenceCompiler, fixture

snapshot, queries = fixture()
wlan = snapshot.get(ObjectKey("demo", "wlan", "wlan", "a"))
port = snapshot.get(ObjectKey("demo", "ex_port", "ex_port", "a"))
batch = Batch(snapshot.revision, (
    Operation.update(wlan, {"vlan_id": 20}),
    Operation.update(port, {"tagged_vlans": [20]}),
))
result = Twin.compile(snapshot, ReferenceCompiler()).simulate(
    batch, queries=queries, rollout="mixed", max_states=10_000, max_stages=64,
)
assert not result.proposed.proven_violations
assert all(stage.assessment.proven_violations for stage in result.stages)
```

An application supplies a compiler implementing `compile(snapshot) -> Compilation`.
It must return a `Program` and `Coverage`. Compilation belongs outside collection:
it must be deterministic for the supplied snapshot and must not fetch additional
state, apply changes, consult credentials, or silently infer missing facts.
The engine trusts compiler authors' semantics; receipts are not an independent
verification of a plugin's correctness.

`ObjectKey` always includes the organization, object kind, identity, and optional
site. An empty site denotes organization scope. VLAN IDs and packet contexts are
fields within a scoped program; the engine does not globally join equal VLAN
numbers across sites. Explicit nodes/edges determine those relationships.

Snapshots serialize source bodies to canonical JSON at construction and return
fresh trees through `Record.body()`. Snapshot revisions include object identities,
body revisions, source/schema/platform/release metadata, and input acquisition
windows. No baseline object is modified while applying a batch or exploring a
stage.

Each operation carries its own key and expected **rolling** body revision.
`UPDATE_ROOTS` replaces supplied root values; omitted roots persist. Mist deletion
markers use `{"-root": ""}` and cannot conflict with an update of that root.
`Operation.create(record)` supplies explicit provenance; deletes require a
revision and contain no payload. Duplicate IDs, cross-organization operations,
stale snapshots, and conflicting revisions raise `ValueError`; a missing snapshot
lookup raises `KeyError`. This offline contract does not replace Mist API schema
validation or controller-renderer equivalence checks.

## Meaning of results and coverage

Every query names an entry, a target, a typed packet/client class, a population,
and an intent (`must_reach=True` for delivery or `False` for isolation).
An empty or duplicate obligation list is rejected.

| Status | Meaning within the declared program and input bounds |
| --- | --- |
| `satisfied` | Every explored modeled behavior meets that property's intent, with no uncertainty or applicable coverage gap. |
| `violated` | A definite modeled counterexample exists and configuration coverage has no gap. |
| `unknown` | Missing/unsupported semantics, acquisition gaps, unresolved facts, or resource limits prevent a definite conclusion. |

These are **per-property** statements. A report never labels an entire organization
universally safe. `Assessment.proven_violations` excludes candidate failures whose
coverage is incomplete. Their traces remain available as diagnostics.

Receipts bind an exact structural path to the object's body revision, source,
schema version, platform, release, model/version, evidence identifiers, and
dependencies. Paths include list indices, explicit nulls, empty lists, empty
objects, and an empty object's root. Parent/wildcard receipts cannot cover
unconsumed descendants. Unknown fields and enum branches must remain unconsumed
or explicitly opaque. Non-interference requires a stated reason.

This first implementation checks the **entire captured configuration inventory**
before allowing definite comparison results. An unchanged unsupported field can
therefore make every query unknown. This deliberate conservative bound needs a
future property-specific non-interference contract before it can become precise.
Dependency traversal follows receipt edges across sites and handles cycles.
`affected_objects` conservatively includes changed records and baseline/proposed
program and receipt dependencies; it is not a minimal list of affected clients.

Incomplete acquisition windows block definite results. Supply both an aware `at`
and `max_age` to `simulate` to enforce freshness; stale and future windows become
gaps. Without those arguments, no maximum-age assertion is made. Collection
windows do not imply an atomic controller/device snapshot.

## Symbolic behavior and limits

The evaluator represents integer/IP intervals and finite/cofinite typed sets.
It splits classes at ordered-rule boundaries and computes all represented
branches. IPv4 and IPv6 are disjoint; boolean `True` never coerces to integer `1`.
Missing facts propagate uncertainty, including both matching and nonmatching
possibilities. Repeated predicates preserve constraints on the same field.

Literal rewrites and singleton copies are modeled. Writes read the incoming
state simultaneously. A copy of a nonsingleton domain into another field loses
an equality relationship in this Cartesian representation; such paths are
explicitly uncertain. In particular, SRX first-packet analysis of broad classes
that require original/translated tuple correlations may remain unknown. General
relational constraints are a subsequent precision improvement.

Repeated forwarding states produce loop traces; missing destinations produce
unknown traces. `max_states` bounds visited nodes and predicate/transfer work,
including large fan-out. Exhaustion contributes an unknown trace covering
unfinished exploration. This implementation has not been benchmarked for a large
organization or dimensioned to a particular maximum number of sites/clients.

`rollout="prefixes"` explores proper ordered prefixes. `"mixed"` explores proper
nonempty subsets when operations target distinct objects. Initial/final states
are evaluated separately; `max_stages` caps intermediate compilation/evaluation.
Repeated-object mixed activation and budget exhaustion are reported explicitly.
All rollout modes exclude device commit timing, retained sessions, timers and
transient protocol state. The returned rollback revision identifies the baseline
configuration; it does not promise restoration of runtime/session state.

## Implemented Juniper primitive bounds

These primitives consume **supplied native facts**, not raw Mist objects. The
documentation examples are regression fixtures, not independent device validation.

| Primitive | Implemented subset | Required exclusions/unknowns |
| --- | --- | --- |
| EX ingress/egress | Single-tag VLAN admission, native classification, egress tagging, explicit ELS LLDP/LACP exception | Tagged access requires an explicit platform profile; priority tags and missing ELS native logical membership are opaque. No MAC learning, STP convergence, voice discovery, dynamic VLAN or full switch fabric model. |
| EX LACP | Active/passive eligibility and explicit force-up | No bundle negotiation, member health, hashing or failure timing. |
| Supplied routes | VRF-specific longest-prefix and preference selection, IPv4/IPv6 | Unknown resolution and unmodeled ECMP are opaque. Does not generate OSPF/BGP/EVPN routes. |
| SRX first packet | Static NAT before DNAT, route before security policy, reverse static NAT before SNAT; specific rule-set selection before ordered rules | Literal selectors/rewrites only. No NAT pools, ALG, screens, AppID, IPsec, dynamic routing or device defaults inferred from Mist. |
| SRX return association | Exact translated five-tuple plus VRF and reverse translation to the original tuple | No timers, TCP state, policy rematch, existing-session updates or zone/interface session lookup profiles. |
| SSR FIB | One prefix per service, supplied route updates, tenant hierarchy/VRF selection, transport overlap, lexical precedence, best-match-only/any-match | Broader-route expansion and unresolved/multiple native route candidates are uncertain; generated cloud policy and service-route/peer selection are not implemented. |
| SSR SLA selection | Explicit connected-path facts, preference and best-effort new-session eligibility | Unknown health remains unknown. No learned health timeline or existing-session migration. |

Tests cover the documented [SRX NAT ordering](https://www.juniper.net/documentation/us/en/software/junos/nat/topics/topic-map/security-nat-overview.html)
and [SSR FIB counterexamples](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/concepts_fib_construction/index.html).
The EX tagged-access guard reflects the platform/release differences visible in
the [VLAN guide](https://www.juniper.net/documentation/us/en/software/junos/multicast-l2/topics/topic-map/bridging-and-vlans.html)
and [22.2R3-S3 release notes](https://supportportal.juniper.net/sfc/servlet.shepherd/document/download/069Dp00000AwagOIAR/%3FoperationContext%3DS1).
The [research inventory](juniper-device-semantics-research.md) remains the broader
backlog; these tests do not complete its entire proposed validation matrix.

## Mist API capture boundary

`digital_twin.adapters.mist.behavioral_capture.capture` consumes an existing
`StateProvider`. The caller owns authentication and explicitly selects sites:

```python
from digital_twin.adapters.mist.behavioral_capture import capture

# provider is the application's existing Mist API provider or an offline replay provider.
snapshot = capture(provider, org_id=org_id, site_ids=site_ids, include_nac=True)
```

It captures site/settings, assigned network/site/gateway templates, device
configuration, site WLANs, organization networks, and optional NAC rules/tags.
Shared records are deduplicated by scoped identity, contradictory captures are
flagged, and missing assigned templates or required successful-fetch markers
produce incomplete input windows. Device platform/release metadata uses supplied
Mist configuration/statistics, and cached site acquisition times are preserved.
Raw bodies may contain sensitive configuration: keep them within the application's
existing storage/redaction boundary; this module adds no persistence or logging.

**The capture is intentionally marked as an incomplete behavioral inventory.**
Selected sites do not prove organization-wide membership; complete Mist Edge and
effective policy sources and behavioral observations are absent. A successful API
call is not full-stack behavioral coverage. Raw Mist captures cannot currently
produce definite full-stack results through this library. Failed sites, missing
identities, inconsistent shared objects, wrong scopes and partial NAC tag capture
remain visible instead of becoming empty, authoritative configuration.

The next integration work is to gather complete Mist-only organization membership,
Mist Edge/policy/config sources and timestamped topology/client observations, then
compile actual Mist inheritance and rendering into these models with path receipts.
NAC decision/onboarding, WLAN security and client compatibility, Mist Edge tunnel
termination, DHCP/DNS dependencies, and routing/control-plane generation remain
unimplemented. Where Mist does not expose a necessary fact, the corresponding
property must remain unknown and name the missing fact.

The provider package now loads `MistApiProvider` lazily when an application
explicitly imports that class; its existing public export is preserved. Importing
the new capture boundary and pure provider contracts does not load the SDK.

## Verification

```sh
uv run --frozen pytest tests/behavioral
uv run --frozen pytest
uv run --frozen ruff check src tests experiments tools/audit_oas_coverage.py
uv run --frozen mypy src
uv build --no-sources
```

The first milestone includes differential domain-algebra checks, whole-class
counterexamples, immutable/revisioned batches, exact coverage boundaries, cross-site
dependencies, incomplete/stale captures, coupled rollout states and bounded vendor
examples. Default tests exclude the existing live tests. An installed-wheel smoke
test outside the checkout verifies import and execution without vendor SDK imports.
No request to the owner's live Mist organization or configuration mutation has
been performed for this implementation. See the [verification record](evidence/behavioral-foundation-verification.json).
