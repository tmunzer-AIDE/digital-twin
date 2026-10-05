# Mist attribute and identification review

Reviewed October 5, 2026 against commit `6840ace` and the pinned Mist **2609.1.0** specification. Changes were simulated offline. No Mist configuration or live organization was modified.

The existing engine has useful issue detectors, but an allowlisted attribute does not establish a complete network simulation. This review adds narrow cosmetic permissions and closes reproduced identification/scope gaps. Full-stack completeness remains an implementation requirement, described below and in the [earlier audit](full-stack-twin-audit.md).

## Attribute ledger and scope

The [attribute ledger](evidence/mist-attribute-review.json) assigns a disposition to **every one of the 7,240 field occurrences in 34 selected configuration roots**, and **every one of the 22,048 documented input occurrences across 473 organization/site mutation operations**. It retains operation, media type, structural path, schema variant and an index into the [original inventory](evidence/mist-oas-coverage-audit.json), which contains the descriptions/types. Repeated branches and object contexts are separate occurrences, not unique features or a coverage percentage.

Specification SHA-256: `22f55432535ab38f6c0539392a729b8fd515a9ccae9df693fbd4ff23d40b8fac`. The ledger also binds the inventory's content digest. Regenerate it with:

```bash
uv run --frozen python -m tools.review_mist_attributes
```

The ledger gives 21 selected-root occurrences a cosmetic direct-update disposition. Other dispositions distinguish server metadata in its specific resource context, admitted fields that require checks, atomically admitted lists requiring element validation, unsupported mechanisms, unsupported objects/actions, and open boundaries. These are conservative scope decisions, not per-field behavioral calibration. An undocumented body is still a gap.

All currently supported update contexts received explicit review:

| Object | Registered cosmetic fields | Fields requiring care |
| --- | --- | --- |
| Switch device | `notes`, `image1_url`/`image2_url`/`image3_url`; usage `description` and existing UI helper; inline port descriptions; local port `note` | Device `name` selects switch-matching rules. Model/MAC/type are device identity, not global metadata. Profiles, routing, auth, virtual chassis, CLI and operations need their own models. |
| Site setting | Usage `description` and existing UI helper; `vars_annotations.*.note`/`type` | Variable **values** still require ripple checks. RF, AP matching, auto-placement, schedules, upgrade/push settings, routing policies and external services are operational inputs. |
| Network template | Root display `name`; usage `description` and existing UI helper | Matching rule `name=default` is reserved; map keys identify profiles/networks; rule selection changes configuration. These names are not covered by the display-name permission. |
| Gateway template | Root display `name`; `port_config.*.description` | Root `type` selects SRX/SSR semantics. Port `name` derives interface configuration; path names select WAN/VPN paths. No blanket name/description exception. |
| Site template | Root display `name`; inherited usage labels/UI helper and port descriptions in the existing compiler contract | Its pinned schema is incomplete. Cosmetic additions do not validate the undocumented switch/gateway surfaces. Variable annotations are not promoted here because placement is undocumented. |
| WLAN | No additional cosmetic permission | `thumbnail` describes portal media; SSID, selection, onboarding/security, VLAN/tunnel bindings, schedules, RF/compatibility and telemetry exclusions can affect service. No generic WLAN `name` field is documented. |
| NAC rule | Existing root `name` | Matching, order, action and returned tags are semantic; labels/tags can resolve site-variable VLANs. Dry-run/unknown matchers remain opaque. |

The other selected roots remain explicit unsupported object contexts: site, sitegroup, org setting, AP/gateway devices, all three device-profile families, AP/RF/WLAN templates, WXLAN rules/tags/tunnels, NAC tags/portals, PSKs, security policy, organization networks, services/service policies, VPN, Mist Edge/cluster/tunnel, EVPN topology and IDP profiles. Their attributes are present in the ledger. Adding a display-like leaf cannot authorize a previously unsupported endpoint, assignment, import, command or platform. Existing delete-specific contracts remain separate from update-body cosmetic permissions.

## Cosmetic allowlist changes

Added usage descriptions for switch/site/network/site-template contexts, local switch port notes, switch image URLs, site variable annotations, root template display labels, and gateway/site-template port descriptions. Existing device notes, inline descriptions, NAC rule labels and the direct-update UI helper remain registered cosmetic facts. `image1_url` is now explicitly admitted and exported as a change instead of being silently excluded as device status.

The only schema addition is the pinned `site_setting.vars_annotations` definition. Its source/version/digest is recorded in `oas/VERSION`; this is not a general refresh of the older extracts.

Cosmetic means the reviewed **effective leaf** does not change the current network behavior. Payloads must still preserve other values in a replaced root. Creating a new device-level profile override with only a description can replace inherited forwarding values, so it is not equivalent to annotating an existing effective profile. All normal raw, structural, derived, companion-change and verdict checks still run. Invalid values, unsupported sibling fields, inheritance changes or independently harmful changes do not inherit `SAFE`.

`port_config.*.critical` was removed from the cosmetic permission: the specification says it controls port up/down alarms. An alarm-generation change needs operational coverage even when forwarding is unchanged. An unchanged boolean alarm flag does not block a forwarding edit on that port, including an expanded port range. Scheduling, reboot PoE, RADIUS retry timing, QoS, RF, authentication, routing and telemetry suppression remain outside an always-inert permission.

Gateway IP edits have a bounded dependency exception: explicit static mode and an unchanged valid IPv4 mask can be retained when both usable addresses belong to the same subnet. These changes still require `REVIEW` for endpoint, next-hop and established-session impact. Mode/mask edits, implicit or DHCP addressing, subnet changes, invalid addresses and opaque sibling settings remain coverage gaps. This does not add type or netmask to the editable allowlist or implement a complete gateway routing model.

## Findings corrected

| Priority | Reproduced issue | Result after correction |
| --- | --- | --- |
| P1 | Renaming `sw-a` to `sw-b` selects a different switch-matching rule with a different management IP, but the compiler only projects the rule's ports. The old verdict was `SAFE`. | Compare uncompiled settings in the full selected rules. Changed IP/STP/mirroring or other omitted settings produce a coverage-gap `UNKNOWN`. An unsupported matcher grammar also blocks a selector-change proof. Known port-only selection still runs normally. |
| P1 | `effective_update` preserved device identity fields in every object. A gateway-template `type: srx` → `ssr` edit disappeared before scope screening; global raw exclusions hid it too. | Require an explicit object context and preserve device identity only on devices. Real template platform changes reach the field gate and config diff and are rejected as unsupported. Device-only model/status exclusions do not spread into NAC/WLAN/template contexts. |
| P1 | Dotted paths conflate a literal map key with nesting. `office.storm_control` plus an unknown `percentage` leaf could match an allowed nested storm-control path. BGP `**` also admitted deeper unknown neighbor children. | Deltas retain original structural tokens. Raw, derived, device-profile and override comparisons use those tokens. Both wildcard spellings consume one original map key. Actual dotted names/IP keys work; fabricated nesting cannot borrow an allowed leaf. Display paths remain compatible. |
| P1 | Duplicate normalized client MACs silently kept the first observation and earned complete client visibility even when the second identified another port/SSID/VLAN. | Conflicting known identities become bounded client-telemetry gaps and revoke complete client capability. A shutdown of the hidden second port cannot be `SAFE`. Consistent duplicates still coalesce. The first observation remains in the current single-attachment IR; the engine does not model every possible attachment. |
| P1 | A `no_local_overwrite` flip screened only top-level local attributes; an unknown child under an admitted object such as storm control was invisible. | Screen all structural leaves of the activated local entry, including nested unknowns. |
| P1 | Changing only a port usage pointer or protocol selector could activate existing opaque settings without changing their raw/effective leaves. | A bounded dependency screen inspects relevant baseline and proposed rows, selected/dynamic target usages, network references, inline port entries and authentication backends. Local-only ports share the resolver's override semantics. Editing a dynamic target follows reverse rule reachability to rescreen affected ports, including chained references. Unsupported QoS, isolation, protocol authentication/timers/options or other relevant fragments produce coverage-gap `UNKNOWN`. Unrelated configuration and cosmetic-only edits do not become global blockers. This is not a complete mechanism dependency compiler. |
| P1 | Atomic array admission accepted unknown children in dynamic rules and RADIUS server objects. Dynamic evaluation also coerced malformed comparison values or ignored unsupported rule children. | Inspect admitted list contents at the raw and derived boundaries, including existing relevant dependencies. Dynamic rules require a supported object/comparison shape, including on a down port. Unknown nested fields and non-string members in primitive arrays cannot inherit their parent's permission. Known RADIUS item fields remain subject to the backend-change checks; this does not verify external AAA behavior. |
| P1 | A proposed rename resolved historical name-only LLDP against proposed labels, losing or reassigning observed links. Duplicate managed names resolved by dictionary insertion order. Resolved dynamic profiles reused an old system name. | Bind historical switch/AP LLDP to the baseline inventory. Only unique names resolve; ambiguous observed identities revoke topology completeness and create a gap for changes that need it. Notes-only edits remain eligible for `SAFE`. Renames affecting baseline or newly activated LLDP-name dynamic profiles expire the proposed runtime result and produce `UNKNOWN`, including observed old names with a foreign Virtual Chassis hardware ID. Replay save/load preserves observation identity. MAC-based claims remain usable when duplicate labels are irrelevant to them. |
| P1 | Disabling/deleting a WLAN with zero currently observed clients passed with only a caveat. Narrowing its scope could similarly pass when current clients remained covered. | Reduced modeled SSID/AP coverage without an observed proven outage requires `REVIEW` for future/disconnected clients. Proven active-client coverage loss still establishes `UNSAFE`. An equivalent modeled scope remains eligible for the existing checks; complete authentication/onboarding equivalence is still outside this model. |
| P2 | Network ERROR/CRITICAL findings entered `UNSAFE` without checking their own confidence, despite the documented proven-breakage contract. | Only HIGH-confidence network errors establish `UNSAFE`; uncertain errors require `REVIEW`, or `UNKNOWN` when accompanied by a coverage gap. An independent proven error retains `UNSAFE`. |
| P2 | README described whole-object replacement and unconditional UNKNOWN precedence, conflicting with the implemented root-update and coverage-gap rules. | Document supplied-root replacement and hard-UNKNOWN / proven-UNSAFE / coverage-UNKNOWN precedence accurately. |

Regressions cover cosmetic add/edit/remove and visible diffs, no modeled delta for label-only edits, assigned template fan-out, profiled devices, invalid types, exact dotted-key authorization, selector activation, platform changes, nested/local-only activation, normalized client conflicts, consistent duplicates, reverse dynamic dependencies, atomic-list contents, LLDP identity/rename handling, future-client uncertainty, replay round trips, and coverage gaps combined with an independent proven WLAN outage. Path tests exercise the production structural matcher; the unused display-path matcher is removed. The committed ledger must exactly match regeneration from the pinned inventory and current permission registry. Proposed observation screening reuses already compiled device configs, and the derived/dependency gates share their leaf diff.

The real-fixture switch rename now requires `UNKNOWN` because an observed downstream dynamic profile reads its old name; a notes-only change stays `SAFE`. The snooping/untrusted-uplink fixture retains `REVIEW` with its unchanged alarm flag. A gateway address move across subnets remains `UNKNOWN`; an explicit static move within the same valid subnet is `REVIEW`.

## Remaining blind spots and next work

1. **P1 — General activation completeness.** The new dependency/list screens close the reproduced port-usage and protocol-row gaps. They remain a bounded production backstop rather than exact receipts for every relevant mechanism, open schema/union boundary, selector and cross-feature interaction. Next: integrate the behavioral engine's receipt contract with the Mist compiler, preserve opaque fragments, and gate the complete union of baseline/proposed dependency closures.
2. **P1 — Identity and proposed observations.** Name-based LLDP now stays bound to observation-time identities and affected dynamic outcomes expire. VLANs/routes still require bridge-domain/VRF/fabric/site context. Client mobility, Virtual Chassis aliases and other name-driven downstream consumers remain incomplete. Current clients have one attachment per normalized MAC; conflicting identities revoke completeness but are not explored as alternative attachments. Next: contextual identities, broader causal observation expiry and possible-attachment/cohort evaluation.
3. **P1 — Full routing, policy and composition.** Existing OSPF/BGP checks do not compute a RIB/FIB or policy fixed point; EVPN, VRFs, ordered firewall/NAT, SSR session paths, Mist Edge transport and combined NAC/WLAN/wired onboarding are incomplete or absent. Next: independently calibrated mechanism compilers with known working/failing and interaction fixtures. These attributes remain default-denied rather than being promoted from their schema descriptions.
4. **P1 — Population and business intent.** Reduced WLAN coverage now requires review even with no observed affected clients. That floor is not a model of future/disconnected clients, required services or replacement-WLAN authentication/onboarding compatibility. Next: explicit required cohorts, onboarding/application obligations and approved decommission intent.
5. **P1 — Deployment/time/state.** A clean final-state model cannot certify mixed pushes, schedules/reboots, reauthentication, existing leases, retained NAT/SSR sessions or rollback behavior. The offline foundation explores bounded configuration stages, not these runtime dynamics. Next: activation and retained-state contracts with coverage outcomes for exhausted/unsupported scenarios.
6. **P2 — Specification and model calibration.** Older extracts, incomplete site-template documentation, locally patched switch BGP `disabled`, implicit defaults, generated CLI and cloud/device version differences require tracked reconciliation. The ledger is pinned to one snapshot; a new schema hash cannot silently inherit the review. RF, capacity, PoE budgets and external AAA/DNS/DHCP/portal availability require evidence beyond configuration validity.

The behavioral foundation from commit `6840ace` adds scoped immutable records, exact receipts, bounded symbolic behavior and rollout primitives. Production Mist-to-behavior compilation remains open; it does not make the current verdict engine a validated full-stack twin.

## Verification

Run the normal offline suite, lint and strict source typing:

```bash
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen mypy src
```

CodeRabbit CLI 0.8.2 is installed, but its authentication status reports re-authentication required, so the external review was unavailable. The findings and corrections above come from local source review and offline regressions. Live tests remain unexecuted; the changes do not establish vendor/cloud equivalence or universal safety.

Final offline verification after the PR review follow-up: **2,301 tests passed, 2 live tests deselected**; Ruff, strict MyPy (169 source files) and `git diff --check` passed.
