# Juniper documentation findings for the Mist behavioral twin

Research date: **2026-10-01**. This is a reference and implementation proposal. A [bounded primitive subset](behavioral-engine.md) now has offline regression tests; this does not establish independent validation against devices or production Mist-to-native translation. Production inputs remain **Mist APIs only**. The local Mist specification is **2609.1.0**; field presence establishes an input surface, not behavioral coverage.

The documentation provides useful, reusable device semantics. It supports the [behavioral twin architecture](full-stack-twin-design.md): implement configuration-to-behavior models, compose them across devices and sites, and evaluate connectivity, isolation, onboarding, and continuity. It does **not** eliminate the need for those models or make undocumented future features predictable.

## Findings that change the design

The entries below distinguish a documented fact from our proposed use of it. Every model needs a platform, software release, address-family, and operating-mode applicability boundary.

| Area | Documented behavior | Implication for the twin |
| --- | --- | --- |
| EX VLAN handling | ELS trunks recognize some untagged control traffic even when untagged data cannot enter. Native VLAN configuration governs data classification and tagging. [Bridging and VLANs](https://www.juniper.net/documentation/us/en/software/junos/multicast-l2/topics/topic-map/bridging-and-vlans.html) | Model ingress admission and egress tag transformations separately. Seeing LLDP on an AP uplink does not establish that AP management or client data can traverse it. |
| EX LACP | An ordinary LACP exchange needs an active participant; two passive peers do not establish the bundle. Force-up is an explicit exceptional configuration. [LACP for switches](https://www.juniper.net/documentation/us/en/software/junos/interfaces-ethernet-switches/topics/topic-map/aggregated-ethernet-lacp-switches.html) | Compute logical-link eligibility from both peers and member state. A physical link being up is insufficient. Keep force-up, static aggregation, and negotiated aggregation distinct. |
| EX spanning tree | Loop protection can keep a port blocked after expected BPDUs stop arriving. The documented examples include operational verification. [Loop protection](https://www.juniper.net/documentation/us/en/software/junos/stp-l2/topics/topic-map/spanning-tree-loop-protection.html) | Derive a forwarding topology for the selected protocol and VLAN/instance, then simulate relevant events. Configuration alone does not supply an external bridge's current state. |
| EX filters | Filters have terms, match conditions, actions, and modifiers for IP and non-IP traffic. The guide records SKU-specific behavior, including fragment matching differences. [EX filter reference](https://www.juniper.net/documentation/us/en/software/junos/routing-policy/topics/concept/firewall-filter-ex-series-match-conditions-description.html) | Retain packet family, fragment status, filter attachment, and platform. A switch filter and an SRX stateful security policy need different semantics. Unsupported matches cannot silently become an ordinary IP ACL. |
| EX authentication fallback | RADIUS timeout can permit, deny, sustain prior admission, or select a fallback VLAN. Reject handling and tagged voice handling are separate; recovery can trigger reauthentication. [Server fail fallback](https://www.juniper.net/documentation/us/en/software/junos/user-access/topics/topic-map/server-fail-fallback.html) | Model new clients, existing admitted clients, voice/data traffic, timeout, rejection, and recovery as different states/events. Compose the resulting VLAN with switching and routing. |
| Junos OSPF policy | Import policies affect external routes; internal route flooding does not obey the same policy mechanism. Default export rejection concerns redistribution from other protocols. [Default routing policies](https://www.juniper.net/documentation/us/en/software/junos/routing-policy/topics/concept/policy-routing-policies-actions-defaults.html) | Separate adjacency, link-state propagation, external-route admission, and redistribution. A generic import/export filter applied to every OSPF route would give incorrect results. |
| Junos route selection | The default preference table differentiates connected, static, OSPF internal/external, and BGP routes. [Route preference](https://www.juniper.net/documentation/us/en/software/junos/routing-overview/bgp/topics/concept/routing-protocols-default-route-preference-values.html) | Select eligible active routes within a routing context and prefix before packet longest-prefix lookup. Preserve overrides and route kind; do not transfer Junos defaults to SSR. |
| Junos BGP policy | Policy chains and attachment hierarchy matter; attribute modifications and terminating decisions have different effects. [Basic routing policies](https://www.juniper.net/documentation/us/en/software/junos/bgp/topics/topic-map/basic-routing-policies.html) | Compile ordered route-policy programs and the effective neighbor/group/global attachments. Recompute propagation across sites, including routes withdrawn because another route becomes active. |
| SRX NAT | First-packet processing orders static NAT, destination NAT, route lookup, security-policy lookup, reverse static mapping, then source NAT. Rule-set specificity precedes ordered rule matching. [NAT overview](https://www.juniper.net/documentation/us/en/software/junos/nat/topics/topic-map/security-nat-overview.html) | Evaluate the proper tuple at each stage and retain reverse-session translations. Destination translation can change the route, destination zone, and application-port match. An allow rule does not establish usable translated connectivity. |
| SRX policy updates | Policy rematch can reevaluate active sessions. The `extensive` option performs a new lookup that can preserve a session under another permitting policy. [Policy rematch](https://www.juniper.net/documentation/us/en/software/junos/cli-reference/topics/ref/statement/security-edit-policy-rematch.html) | Evaluate established sessions separately from fresh connections. A final-state reachability result cannot establish continuity during a policy replacement. |
| SSR tenants | A service grant to a parent tenant can include descendants; the reverse inclusion does not follow. Tenant information also matters on receiving routers. [Tenancy design](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/bcp_tenants/index.html) | Use hierarchical tenant classification and authorization across routers. Exact string equality or a VLAN-only identity would lose meaning. Validate how Mist network identities map to native tenants. |
| SSR VRF mapping | Service routes are resolved in the routing table mapped to the tenant, or the global table when no mapping exists. A missing route prevents entries for that service/tenant. [VRF support](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/config_vrf_learning/) | Keep tenant, VRF, service, and interface context distinct. A route in another VRF is not evidence that an authorized service can forward. |
| SSR forwarding construction | Service-prefix and transport overlap affect which forwarding entries receive next hops. `fib-service-match` changes this construction; overlap can also suppress entries by service-name ordering. [How the FIB is constructed](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/concepts_fib_construction/index.html) | Implement a service-aware FIB builder, rather than an IP routing table followed by an independent allow/deny check. Service definitions can change other services' behavior. |
| SSR SLA fallback | Native service-policy `best-effort` can continue forwarding on a path below SLA; disabling it can cause traffic to be dropped when all paths fail SLA. [Configuration element reference](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/config_reference_guide/index.html) | Treat SLA eligibility and degraded-service fallback as explicit policy. A failed latency threshold does not invariably mean a connectivity failure. |
| SSR health/state | Health learning uses observations, enforcement intervals, and hold-down. The documented non-SVR mechanism keeps existing sessions on their original path while changing eligibility for subsequent sessions. [Service health learning](https://www.juniper.net/documentation/us/en/software/session-smart-router/docs/config_service_health/) | Represent observed health, time, and session age. Scope this behavior to the documented mechanism; do not assume every SVR failover feature behaves identically. |

The [EX port/VLAN/router filter example](https://www.juniper.net/documentation/us/en/software/junos/routing-policy/topics/example/firewall-filter-ex-series-configuring.html) supplies configurations and verification steps that can become reference cases. Its specific EX3200 topology is not proof of a uniform processing pipeline for every EX SKU.

## Public schemas: a useful second contract

Juniper publishes native configuration, RPC, and state YANG modules by Junos family and release, alongside standard models. Family schemas are not a per-SKU feature guarantee. [Obtaining Juniper YANG modules](https://www.juniper.net/documentation/us/en/software/junos/netconf/topics/task/netconf-yang-module-obtaining-and-importing.html)

The public [Juniper/yang repository](https://github.com/Juniper/yang) is therefore a useful static reference input. At inspected commit `96ad7badc2603aa1033476103aeded2632db9a9a`, the `25.4/25.4R1/native/conf-and-rpcs` directory includes `junos-ex` and `junos-es` families. This is an available reference release, not a proposed fleet upgrade or a claim that it matches the user's organization.

Proposed use:

1. Pin the reference package matching the observed device family and software release.
2. Extract hierarchy, types, enumerations, ordering, defaults, and constraints **where actually encoded**. Treat vendor extensions and incomplete constraints explicitly.
3. Map Mist fields to generated native configuration and attach source/version provenance to each translation.
4. Use documentation for forwarding and state-transition behavior; use independent observations or vendor verification cases to calibrate those models.

This can reduce handwritten structural validation. YANG does not calculate hypothetical routes, build SSR service forwarding, evaluate RADIUS identities, or reproduce hardware resource allocation. No YANG parser, semantic extraction, or schema compilation was implemented during this research. The publication directory was inspected; individual module constraints were not audited. These Junos assets also do not establish SSR schema support.

## Mapping to the available Mist inputs

The [research evidence inventory](evidence/juniper-semantics-research.json) records exact paths selected from the existing OAS inventory and a separate exact-property-name search over the whole pinned specification. These are candidate compiler inputs, not claims that endpoint permissions, responses, effective values, or model behavior were verified against an organization.

| Model | Representative Mist schema surfaces | Required interpretation |
| --- | --- | --- |
| EX VLANs and links | `device_switch.port_config`, `port_usages`, `networks`; `ae_lacp_passive`, `ae_disable_lacp`, `ae_lacp_force_up`; STP fields | Resolve template/profile/device layers, network labels, members, and peer configuration before deriving eligibility and tag behavior. |
| EX access admission | `port_usages.{key}.port_auth`, `enable_mac_auth`, `mac_auth_preferred`, `bypass_auth_when_server_down`, `bypass_auth_when_server_down_for_unknown_client`, `server_fail_network`, `server_reject_network`, `server_fail_retry_interval` | Prove the mapping to native admission/fallback options. The existence of these fields does not prove complete RADIUS policy or client trust state is observable. |
| EX segmentation | `acl_policies`, `acl_tags`, including protocol/port, EtherType, subnet, role and GBP selectors | Translate the effective attachment and hardware capability. Role/GBP values may need authentication or fabric context. |
| EX/SRX routing | `bgp_config`, `ospf_config`, `ospf_areas`, `routing_policies`, `extra_routes`, `extra_routes6` where present in the relevant component | Apply the native platform's route-policy scope, attachment precedence, selection, and next-hop resolution. Schema surfaces differ between switches and gateways. |
| SRX NAT and policy | `network.internet_access.destination_nat`, `static_nat`; `vpn_access` NAT fields; gateway `port_config.{key}.wan_source_nat`; `service_policies` | Resolve network/zone/service mapping and ordered generated rules. Preserve original and translated tuples and connection state. |
| SSR services and paths | `service.addresses`, `specs`, `max_latency`, `max_jitter`, `max_loss`, `failover_policy`; gateway `service_policies`, `path_preferences`, `vrf_instances` | Compile native service/tenant/VRF semantics and path construction. Mist `service_policy` and native SSR `service-policy` are different contracts; do not join them by similar names. |

The exact property names `fib_service_match`, `fib-service-match`, `policy_rematch`, `policy-rematch`, `best_effort`, `best-effort`, `path_quality_filter`, and `path-quality-filter` were not found as JSON keys in this specification. **That is a bounded spelling check, not proof that Mist cannot set or generate equivalent behavior.** For example, `best_effort` exists as a traffic-class enum value, which does not establish the native SLA fallback setting. Defaults and generated policy must be established independently.

Two existing API mechanisms can help calibrate the baseline:

- `GET /api/v1/sites/{site_id}/devices/{device_id}/config_cmd` exposes generated configuration commands. Its description explicitly discusses adopted switches whose pre-existing configuration is not automatically overwritten. Generated intent must not be represented as a complete applied running configuration.
- `POST /api/v1/sites/{site_id}/devices/{device_id}/show_route` describes route diagnostics for SSR, SRX, and switches, delivered through the command WebSocket stream with session demultiplexing. A future collector can use it within the approved Mist-only boundary, subject to permissions, command availability, freshness, and output parsing. It was not invoked here.

Derived GET objects and generated baseline commands do not provide a hypothetical compiler for a proposed snapshot. Implement and validate that translation locally; record uncertainty where the cloud renderer's behavior has not been established.

## Minimum missing-fact reporting

| Missing or insufficiently established fact | Effect on a simulation |
| --- | --- |
| Complete applied configuration, especially for partially managed/adopted devices | Extra filters, defaults, or native settings can invalidate a positive prediction. Report the affected device and mechanism. |
| Generated native SSR service/tenant mapping and hidden forwarding/SLA options | Ordinary IP reachability is insufficient. Branch over supported alternatives or return an unknown result for the affected service. |
| Existing SRX/SSR sessions, translations, and effective update behavior | Report fresh-session reachability separately; continuity of existing sessions remains conditional. |
| External routing peers, current advertisements, and recursive next-hop state | Bound results by explicit boundary assumptions. Do not freeze learned routes through a change that could withdraw them. |
| Detailed client supplicant, certificate/trust, and identity-provider state | A modeled network admission path cannot prove that an individual client will successfully authenticate. |
| Traffic demand, path-health history, resource use, and platform limits | Detect deterministic configured contradictions; report SLA, congestion, exhaustion, and convergence predictions as conditional when inputs are missing. |

Actual availability must be checked endpoint by endpoint; this research did not exhaustively audit every telemetry response. A newly exposed fact should improve a mechanism's coverage rather than create a new incident-specific detector.

## Validation cases to derive from the documentation

These cases are **proposed**, not newly implemented tests. Keep their initial state, platform/release, packet/client class, events, expected trace, and source together. Documentation examples provide hypotheses; synthetic tests alone do not establish device fidelity.

| ID | Scenario and expected distinction |
| --- | --- |
| EX-01 | Trunk admits LLDP while dropping untagged management data; native VLAN changes alter only the appropriate data classification/tagging. |
| EX-02 | Active/passive, passive/passive, static, and explicit force-up aggregation produce different link eligibility. |
| EX-03 | A BPDU-loss event with and without loop protection changes forwarding topology and resulting reachability/loop behavior. |
| EX-04 | Filter a non-IP admission/discovery frame; vary fragment status and SKU. Unsupported hardware semantics must remain visible. |
| EX-05 | RADIUS timeout versus rejection, prior admission versus new client, and tagged voice versus untagged data yield different states/VLANs. |
| RT-01 | Apply an OSPF import policy to external and internal routes; distinguish redistribution from normal link-state flooding. |
| RT-02 | Change active route preference, recursive next-hop reachability, or a BGP policy chain; recompute dependent advertisements. |
| SRX-01 | Destination translation changes the destination port and egress zone; routing and security matching use the appropriate transformed tuple. |
| SRX-02 | Overlap static, destination, and source NAT candidates; preserve rule-set specificity, precedence, and reverse translation. |
| SRX-03 | Modify/replace a permitting policy under each validated rematch mode; compare a fresh connection with an established session. |
| SSR-01 | A parent tenant grant reaches descendants; a child grant does not grant the parent. Validate any narrower exclusions independently. |
| SSR-02 | Equal service prefixes in distinct tenant VRFs resolve differently; an unmapped tenant uses the documented global-table behavior. |
| SSR-03 | Reproduce the vendor's service/transport overlap example with both `fib-service-match` modes; a RIB route alone must not yield delivery. |
| SSR-04 | Competing services overlap in one transport; verify which other entries are suppressed, including naming/ordering effects. |
| SSR-05 | All paths fail SLA under each established fallback setting; separate degraded forwarding from a policy-driven drop. |
| SSR-06 | Health enforcement and recovery over time; distinguish existing non-SVR sessions from new session selection. |
| BATCH-01 | Change WLAN/NAC assignment and trunk carriage together. Evaluate baseline, intermediate stages, final state, and rollback. |
| BATCH-02 | Combine an organization service edit, route policy, and remote-site firewall edit. Recompute the union of baseline/proposed dependencies across sites. |

## Interpretation and implementation priorities

Documentation is a source of semantics, not an executable specification. Two inspected examples illustrate why extraction needs review: the SRX NAT page's image alternative text places reverse static mapping differently from its numbered sequence and explanatory body; the SSR configuration reference's `max-loss` text juxtaposes `0.5` with `0.05%`. Do not turn ambiguous prose into a silently asserted default. Record the ambiguity and resolve it with a release-specific authoritative reference or independent verification.

The feature documentation repeatedly directs readers to Feature Explorer for platform/release applicability. The particular YANG Feature Explorer link inspected returned a page-not-found section and login prompt, so no SKU compatibility matrix was verified there. Release notes can supplement applicability: [SRX 24.2R1 known issues](https://www.juniper.net/documentation/us/en/software/junos/release-notes/24.2/junos-release-notes-24.2r1/topics/open-issues/srx-open-issues-24.2r1.html) records defects in otherwise supported mechanisms. This is an example source, not a current defect verdict for the user's devices.

Recommended next implementation sequence:

1. **Pin applicability and translation evidence.** Use observed models/releases, Mist schema provenance, managed-configuration completeness, and native render examples. Enforce the [coverage receipt gate](full-stack-twin-audit.md) before positive conclusions.
2. **Build separate reusable mechanism models.** EX admission/tagging/filtering and protocol topology; Junos route propagation; SRX ordered NAT/policy/session processing; SSR tenant/service FIB and path/session selection. Share packet sets, routing contexts, events, and provenance without erasing vendor differences.
3. **Compose the models and validate outcomes.** Begin with the documented counterexamples above, then exercise cross-domain batches and cross-site effects. Keep the core importable and offline. Selective vendor execution, if later available, is a calibration option rather than a requirement to virtualize the whole network.

The resulting engine can discover new combinations of failures using already implemented mechanisms. A new device feature still requires a known translation and behavior model. Nothing found in this documentation justifies promising all future configuration changes or all user-experience outcomes from Mist data alone.
