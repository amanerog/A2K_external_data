# A2K — Agent-to-Knowledge Protocol Suite
## Overview, Positioning, and Architecture

**Version:** 0.6-draft
**Contract string:** `0.6-baseline`
**Status:** Working draft / baseline for implementation
**Date:** 2026-07-07
**Audience:** Enterprise AI platform teams, agent developers, KB-owning teams, security and IAM architects, data governance teams, compliance and model-risk teams, legal/risk stakeholders, and MCP/A2A server authors.

Normative keywords **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, **MAY**, and **OPTIONAL** follow RFC 2119 / RFC 8174 when used in all capitals.

---

## How to read this suite

A2K is specified in four aligned documents:

| Document | Defines |
|---|---|
| **A2K-Overview** (this document) | Problem statement, positioning, architecture, layers, design principles, conformance levels, security tiers, banking overlay, adoption, glossary, roadmap |
| **A2K-KBCard-Schema** | The KB Card: version axes, identity, ownership, authority, lifecycle, access and security tiers, knowledge profile, operations, auth, policies, audit, regulated extension, conformance, signature |
| **A2K-KRP** | The Knowledge Resolution Protocol: `resolve`, `register`, `getCard`; Catalog responsibilities; authority assertion; eligibility and ranking; routing quality; governance flags; pagination; hierarchy; access-leakage rules |
| **A2K-KCP** | The Knowledge Consumption Protocol: `search`, `ask`, `explain`, `getDocument`; cited-response envelope; claims; citations; grounding; freshness; access decisions; audit; conflicts; referrals; regulated controls; streaming proof footer |

This document is both an architectural decision record and the entry point to the baseline.

---

## 1. Abstract

> **A2K is a transport-neutral protocol suite that lets enterprise AI agents discover, select, query, cite, audit, and reconcile knowledge sources — with scoped authority, lifecycle management, authorization-safe discovery, cited responses, and optional regulated controls — over existing transports such as MCP, A2A, and HTTPS.**

A2K defines no new transport. Its two protocols are application-level contracts over existing wire protocols, in the same sense that HTTP is a contract over TCP: they specify request/response shape and semantics, not connection framing, sequencing, or transport-level delivery.

## 2. What is A2K?

A2K is a **two-protocol, one-schema** suite:

| Artifact | Role | Layering analogy |
|---|---|---|
| **A2K-KRP** (Knowledge Resolution Protocol) | Discovery and resolution | DNS + RDAP |
| **A2K-KCP** (Knowledge Consumption Protocol) | Governed, cited retrieval | HTTP |
| **A2K KB Card** | Shared schema used by both protocols | Resource record + data catalog entry |

The suite can equivalently be read as **two layers**:

**Layer A — Governance and metadata standard.** The KB Card (what a KB knows, who owns it, whether it is authoritative, how current it is, who may access it, which operations it supports); the Catalog (indexes cards, asserts authority from enterprise governance and IAM, filters discovery by authorization, flags ungoverned, stale, orphaned, or colliding sources); the authority, lifecycle, attestation, and access model; and conformance levels that double as implementation tranches. Layer A is operationalized by A2K-KRP.

**Layer B — Application-level cited-response contract.** A small set of read-only operations (`search`, `ask`, `explain`, `getDocument`) with common request fields, pagination, and a structured error model; a cited-response envelope binding answers, passages, claims, citations, source spans, grounding, freshness, access decisions, and audit metadata; a conflict-report artifact for cross-KB disagreement and escalation; and a regulated extension for OBO assertions, strict grounding, robust selectors, data lineage, immutable audit, signed responses, and streaming proof footers. Layer B is defined by A2K-KCP and is transport-neutral: the same operations and envelope apply identically over MCP, A2A, and HTTPS.

A2K verifies provenance, ownership, freshness, access decisions, evidence availability, and — where required — response integrity. It does **not** prove that a claim is true.

## 3. The problem A2K solves

In a large enterprise, knowledge lives in overlapping and unevenly governed places: Confluence, SharePoint, Notion, OneDrive, ServiceNow, Jira, GitHub, data catalogs, policy portals, ticket histories, HR systems, legal repositories, sales enablement material, support macros, product docs, team notes, and personal documents.

An agent answering a question needs to determine: what a source knows; who owns it; whether it is authoritative; whether it is current; who may access it; whether it is approved for enterprise use; whether it may be cited; whether another source disagrees; and whether the answer can be audited later. Without a shared contract for those questions, agents fail in seven recurring ways:

1. **Wrong-source confidence.** The agent answers from a stale or team-local page and presents it as canonical.
2. **Over-broad disclosure.** The agent surfaces content, citations, or even the existence of a KB the user is not cleared to know about.
3. **Orphaned knowledge.** A source appears authoritative but has no accountable owner or has not been reviewed recently.
4. **Authority collision.** Several sources appear canonical for the same topic and quietly disagree.
5. **Uncitable answers.** The agent gives a plausible answer but cannot show exactly which document, passage, policy, or version supports it.
6. **Audit failure.** After a decision, the enterprise cannot reconstruct which user, agent, KB, source version, access decision, and citations were involved.
7. **Vertical lock-in.** Each team builds an agent hard-wired to its one or two KBs, so no agent can answer questions whose evidence spans desks or departments, and every new agent re-solves discovery, authorization, and citation from scratch. A2K replaces N×M point integrations with one contract.

A2K is organized around fixing those failures in that order.

## 4. Why MCP and A2A alone are not enough

MCP and A2A solve important interoperability problems, but they do not solve enterprise knowledge governance.

| Requirement | MCP / A2A contribution | What remains missing without A2K |
|---|---|---|
| Connect an agent to a data source or tool | MCP is a good binding for tools, resources, prompts, and data access. | MCP does not standardize KB ownership, authority, lifecycle, attestation, freshness, conflict reporting, or KB-specific citation envelopes. |
| Let agents delegate work to other agents | A2A is a good binding for peer agents, tasks, artifacts, skills, streaming, and multi-agent collaboration. | A2A Agent Cards describe agent capabilities, not whether a KB is the approved system of record for a topic, whether a source is stale, or whether the user may even discover the KB. |
| Discover something that can answer a question | MCP and A2A have discovery mechanisms. | Enterprise discovery must be authorization-scoped, authority-ranked, lifecycle-aware, and leakage-safe. Different users may legitimately see different KBs. |
| Decide which internal source to trust | MCP/A2A can expose metadata if teams add it. | Enterprises need a common authority model: system-of-record scope, owner, approval proof, review deadline, attestation state, freshness, and collisions. |
| Return an answer with evidence | A server or agent can return text and artifacts. | A2K defines a standard cited-response envelope with claims, citations, spans, source hashes, freshness, access decisions, and audit metadata. |
| Prevent privilege escalation through agents | Protocols can carry auth metadata. | A2K makes on-behalf-of authorization mandatory for non-public knowledge — signed and validated at higher classifications — and treats discovery, citations, conflicts, caching, and referrals as leakage surfaces. |
| Manage cross-KB contradictions | A2A can fan out to agents; MCP can call sources. | A2K defines conflict reports, reconciliation inputs, and escalation telemetry so disagreement becomes a governance signal rather than hidden model behavior. |
| Regulated audit and model-risk evidence | Protocols can transport blobs and signatures. | A2K standardizes strict grounding, robust selectors, lineage, WORM/immutable audit hooks, signed responses, and trailing proof footers for high-control workflows. |

A2K is **MCP-compatible, not MCP-competitive**, and **A2A-compatible, not an A2A replacement**. It layers on both.

## 5. Positioning: when federation beats a central index — and when it doesn't

The honest architectural comparison for A2K is not "no governance" but a **unified, ACL-trimmed central search index**. For an enterprise with permissive data flows, a central index is often simpler, faster, and cheaper per query, and A2K is not the right first tool.

A2K's federated architecture is justified when co-mingling sources in one index is prohibited or impractical:

- **Ethical walls and MNPI controls** forbid indexing restricted material alongside general content, and forbid revealing its existence to uncleared callers.
- **Legal-entity and jurisdiction boundaries** impose residency and separation constraints a single index cannot honor.
- **Accountable per-source enforcement** is required: the system that owns the data must make the access decision, under its own audit trail.
- **Post-hoc reconstruction** is a compliance obligation, not a nice-to-have.

These conditions are the norm in banking, insurance, legal, and pharma; there, federation with per-KB enforcement is frequently the only permissible architecture. A2K also interoperates with central indexes rather than opposing them: an index over the *unrestricted* corpus can register as one large KB, with restricted domains federated beside it.

**Cost expectation.** Federated resolve → fan-out → per-KB synthesis is slower and costlier per query than one central retrieval pass. Deployments SHOULD reserve multi-KB fan-out for queries that need it and SHOULD route single-domain queries to a single resolved KB.

## 6. When A2K is worth adopting

A2K is not worth doing as a separate initiative if the enterprise has a small number of curated sources, no meaningful access boundaries, no overlapping systems of record, and no compliance or audit requirement; a well-designed set of MCP servers plus a curated source list may be sufficient.

A2K is worth adopting when at least three of the following are true:

1. Knowledge lives in many wikis, portals, systems, and team spaces.
2. Multiple sources overlap or contradict each other.
3. Some sources are official while others are draft, local, personal, derived, stale, or vendor-provided.
4. Agents answer as a particular employee, department, geography, product, or legal entity.
5. Some KBs are confidential, restricted, regulated, or ethical-wall sensitive.
6. The organization needs citations, audit trails, model-risk evidence, or post-hoc reconstruction.
7. KB ownership and review status are currently weak or unknown.
8. Teams have built disconnected vertical agents that need to interoperate, or the enterprise wants to prevent "the agent found a stale page and sounded confident" failures.

## 7. Non-goals

A2K does **not** define: a new wire protocol; a new agent-to-agent protocol; a new tool-execution protocol; a new authentication or IAM system; a vector database, embedding, chunking, ranking, or indexing implementation; mutation or write operations; autonomous agent orchestration; public web search ranking; cross-enterprise registry federation (see roadmap); model-training enforcement; long-term memory storage; truth guarantees; deterministic legal, medical, financial, or compliance advice; or downstream transactional enforcement systems.

A2K rides on existing enterprise infrastructure: IAM, SSO/OIDC/OAuth2 token exchange, mTLS, service identity, data catalogs, governance-risk-and-control workflows, SIEM/audit stores, MCP, A2A, HTTPS, and internal gateways. It does, however, define application-level message schemas and operation semantics — the cited-response contract — which are a contract over existing transports, not a wire protocol.

## 8. Architecture

```
Internet:  Resolver → Name server → IP → TCP → HTTP → content
A2K:       Agent → A2K-KRP → KB Card(s) → transport → A2K-KCP → cited response
```

```text
                         +------------------------------+
                         |        CATALOG (A2K-KRP)      |
                         |  - indexes KB Cards           |
                         |  - asserts authority          |
                         |  - filters discovery by auth  |
                         |  - flags stale/orphan/collide |
                         |  - enforces attestation       |
                         |  - measures routing quality   |
                         +---------------+--------------+
                          register card  |  resolve(query, obo)
                  +----------------------+-----------------------+
                  |                      |                       |
          +-------v-------+      +-------v-------+       +-------v-------+
          |   KB Legal    |      |    KB HR      |  ...  |   KB IT/Team  |
          |  (A2K-KCP)    |      |  (A2K-KCP)    |       |   (A2K-KCP)   |
          +-------^-------+      +-------^-------+       +-------^-------+
                  | search/ask/explain/getDocument on behalf of user
                  |
          +-------+----------------------------------------------------+
          |                    KNOWLEDGE AGENT                         |
          | 1. discover best KBs for this user and purpose             |
          | 2. query selected KBs on behalf of the user                |
          | 3. return answer/citations the user may see                |
          | 4. refuse stale/orphaned/draft/unauthorized sources        |
          | 5. surface material conflicts instead of hiding them       |
          +------------------------------------------------------------+
```

**A2K-KRP (resolution).** The agent presents a query intent and caller identity; the Catalog returns the KB Cards that are authoritative, accessible, and appropriate for that caller and question. Resolution is authorization-scoped (different callers see different KBs), cacheable only per identity context, hierarchical (division → group), and governed: authority is asserted by the Catalog, never self-promoted by a KB.

*Where the DNS analogy ends:* DNS is access-blind, stateless, and globally cacheable; A2K-KRP is none of these. It is a policy-aware, identity-scoped semantic router. The analogy explains the layering (resolve before consume), not the operational profile; A2K-KRP §2 makes the differences normative, and implementors sizing a Catalog against DNS assumptions are sizing the wrong system.

**A2K-KCP (consumption).** Holding a resolved card, the agent speaks A2K-KCP to the KB's endpoint carrying the caller's on-behalf-of (OBO) context. The KB returns the cited-response envelope. Consumption is always on-behalf-of, always cited, always auditable, and read-only.

**The KB Card** is the shared structure that makes the protocols interoperate: the unit of registration in the Catalog and the unit of capability advertisement to agents.

### 8.1 Components

**Catalog (mandatory control plane).** Implements A2K-KRP. Indexes cards, asserts authority through a governed approval workflow, filters discovery by OBO identity, computes governance flags, enforces attestation expiry automatically, measures routing quality, and publishes catalog-health metrics. Authority assertion and authorization-filtered discovery cannot be delegated to KBs — this is why a KB cannot self-promote. For a single enterprise, a central group Catalog is strongly preferred: one trust authority, one policy surface. Division Catalogs MAY exist under the hierarchy rules of A2K-KRP §7.

**Knowledge bases.** Independently owned sources implementing A2K-KCP at their declared conformance level, enforcing access against their own native ACLs on every request.

**Knowledge agents.** Resolve → select → query on behalf of the user → return a cited answer the user is authorized to see → refuse stale, orphaned, draft, or unauthorized sources where policy requires → surface conflicts rather than silently choosing.

**Gateway (optional, specified).** A mediation layer between agents and KBs. A Gateway MAY perform registry lookup, access-policy enforcement, fan-out to multiple KBs, conflict detection, citation checking, audit logging, response filtering, answer synthesis, and escalation routing. It is a topology, not a transport: it speaks `https-json`, `mcp`, or `a2a`, and a gateway-fronted KB declares `transport` as the protocol the gateway exposes.

Because a Gateway can become a governance blind spot, its obligations are normative. A Gateway **MUST** log every KB queried per request, the policy decisions applied, conflicts detected, and citations returned; **MUST** preserve the per-KB response envelopes (or a policy-defined sufficient subset) so the final answer remains reconstructable; and **MUST NOT** suppress conflicts, citations, access decisions, or freshness failures from the caller-visible result. A Gateway synthesizing across KBs assumes the agent-side duties of A2K-KCP §12 (conflict reports) and §18 (leakage rules).

**Catalog versus Gateway.** The Catalog is non-optional. The Gateway is optional and useful for centralized fan-out, policy enforcement, conflict detection, or synthesis. A first implementation SHOULD start with a Catalog plus a shared client library before introducing a heavyweight Gateway.

## 9. Conformance levels and security tiers

A2K separates two axes that are easy to conflate: **capability** (what a KB has implemented) and **obligation** (how strictly it must be protected). Conformance levels describe capability. Security tiers, derived from data classification, describe obligation. Feature maturity never lowers the security bar; classification never forces feature work.

### 9.1 Conformance levels (capability)

| Level | Name | A KB at this level provides | Realistic population |
|---:|---|---|---|
| **0** | Discoverable and governed | Valid KB Card, enterprise block, catalog registration, ownership, authority request, lifecycle, access metadata. No query endpoints. | Most KBs. |
| **1** | Cited retrieval | Level 0 + `search` returning the cited-response envelope with resolvable citations; `getDocument` where permitted. | Many production KBs. |
| **2** | Governed answers | Level 1 + `ask` (and recommended `explain`) with citations, freshness, access decisions, audit metadata, claims, and grounding. | High-value KBs. |
| **3** | Managed | Level 2 + approval metadata, review cadence, attestation lifecycle, lifecycle enforcement, catalog-verified conformance, governance workflow hooks. | Official KBs. |
| **4** | Verifiable | Level 3 + strict grounding mode, robust citation selectors, data lineage where applicable, immutable-audit target support, signed cards/responses or equivalent attestations, conflict escalation, streaming proof footer where streaming is used. | Regulated/high-control KBs. |

Per-level requirements:

- **Level 0** MUST provide: a valid KB Card; `enterprise.ownership`; an `enterprise.authority` request; `enterprise.lifecycle`; `enterprise.access`; `knowledgeProfile`; an `operations` declaration; an `auth` declaration; and Catalog registration.
- **Level 1** MUST satisfy Level 0 and implement `search` with citations; SHOULD implement `getDocument` where access policy permits.
- **Level 2** MUST satisfy Level 1 and implement `ask`; Level 2 `ask` responses MUST include answer, citations, freshness, access decision, audit metadata, claims, and grounding. `explain` is RECOMMENDED.
- **Level 3** MUST satisfy Level 2 and include approval metadata where applicable, review cadence, attestation metadata, lifecycle enforcement, owner and maintainer metadata, catalog-verified conformance, and governance workflow hooks. A Level 3 KB MUST NOT remain catalog-asserted `canonical` past its review or attestation due date unless enterprise policy permits a defined grace period.
- **Level 4** MUST satisfy Level 3 and support the verifiability capabilities listed in the table, according to enterprise policy. A2K does not require public cryptographic infrastructure where equivalent enterprise trust infrastructure exists (internal PKI, catalog attestation, immutable audit).

Level 0 requires only Layer A. Levels 1–4 progressively implement Layer B.

### 9.2 Security tiers (obligation)

The tier is **computed from `enterprise.access.dataClassification`**, never declared, and cannot be lowered by card content (A2K-KBCard-Schema §3.4.1):

| Tier | Classifications | Key obligations (normative text in A2K-KRP §8, A2K-KCP §3 and §18) |
|---|---|---|
| **S0** | `public`, `internal` | Plain-claim OBO context plus the transport bearer token is acceptable; standard leakage rules; standard audit. |
| **S1** | `confidential` | A **signed OBO assertion MUST be validated** before returning content; identity-keyed caching MUST; full leakage rules; audit MUST record both represented subject and agent identity. |
| **S2** | `restricted`, `highly-restricted`, `regulated` | S1 + existence concealment MUST (`NOT_FOUND` semantics for denials); immutable/WORM audit where enterprise policy designates responses as records; signed responses SHOULD; ethical-wall validation MUST where flagged. |

`ethicalWallSensitive: true` forces S2 regardless of classification; `unknown` classification is treated as S1 minimum. A Level-1 KB holding confidential data carries the full S1 floor; a public FAQ can reach Level 4 capability without S2 ceremony.

## 10. Design principles

1. **Enterprise governance first.** Agents need to know who owns a source, whether it is approved, and whether it is current.
2. **MCP-compatible, not MCP-competitive; A2A-compatible, not an A2A replacement.** A2K is layered on existing protocols.
3. **Knowledge-first metadata.** A KB Card describes what the KB knows and how it should be used, not merely what endpoint it exposes.
4. **Read-only by default.** A2K operations retrieve, answer, explain, and fetch documents; they never mutate enterprise state. Lower blast radius, simpler authorization, faster adoption.
5. **Access control is external but visible.** A2K does not replace IAM, but it declares required scopes, audiences, classifications, user context, and access decisions, and it carries the access decision back in responses.
6. **Discovery is access-controlled.** The fact that a KB exists may itself be sensitive; unauthorized users must not learn sensitive KB names, descriptions, scopes, citations, or conflict details.
7. **Authorization is on behalf of the user.** The agent must not become a privilege-escalation path; authorization is evaluated against the represented subject.
8. **Security scales with classification, not capability.** Obligations follow the data, via security tiers.
9. **Authority is asserted by the Catalog, not self-declared.** A KB may request system-of-record status; the Catalog confirms, downgrades, or rejects it. Clients trust the assertion over the claim.
10. **Authority is scoped, and scope beats rank.** A KB is never globally authoritative; a local scoped-canonical KB can outrank a global canonical KB for a query whose jurisdiction or scope matches the local source better.
11. **Citations are mandatory for trusted use, and citations come before synthesis.** Cited retrieval is the first useful production milestone; grounded synthesis is valuable but secondary.
12. **Ownership is mandatory.** No enterprise KB should be discoverable for agent use without an accountable owner.
13. **Freshness and attestation matter — and metadata earns trust through enforcement.** Enterprise knowledge decays; attestation expiry has automatic consequences, and catalog health is measured and published.
14. **Disagreement is useful signal.** Conflicts between HR, Legal, Finance, Support, Sales, Product, or Engineering knowledge should be detected and surfaced rather than hidden by model behavior.
15. **Auditability over blind trust.** Agents record which KBs were queried, which policy decisions applied, and which citations supported the answer.
16. **Regulated controls are optional but standard, and incremental adoption is a design goal.** Teams publish basic cards first and add citations, answers, conflicts, and regulated features later; Level 4 controls are never imposed on every wiki.

## 11. Banking overlay

For deployments in banking groups, existing A2K constructs map to regulatory obligations as follows. The overlay adds no new wire fields.

- **Ethical walls / MNPI.** KBs holding wall-side or MNPI-adjacent material MUST set `ethicalWallSensitive: true`, sit at tier S2, require validated OBO assertions, and apply existence concealment to all denials, citations, referrals, and conflict surfacing (A2K-KCP §14.2, §18).
- **Books and records / retention.** Audit records for S2 KBs are candidate books-and-records artifacts. Deployments SHOULD map `logTarget`/`logRetention` to WORM-capable archives and compliance-set retention schedules, and MUST NOT let Gateway mediation break the reconstruction chain (§8.1).
- **Model-risk evidence.** Audit metadata (KCP §11), citations with source hashes and versions (KCP §7), and data lineage (KCP §7.3) together are designed to satisfy "reconstruct what the model was shown and why" documentation demands. Model-risk teams SHOULD consume the conformance corpus and routing-quality metrics (KRP §6.12).
- **Purpose limitation.** Resolution queries can reveal sensitive intent (e.g., a restructuring plan). Catalogs MUST apply KRP §12; resolution logs are access-controlled artifacts in their own right.

## 12. Transport bindings (summary)

Normative binding detail lives in A2K-KCP §2 and A2K-KBCard-Schema §2.

**MCP**

A KB MAY expose A2K through MCP: 

```text
Resource:
  a2k://card

Tools:
  a2k.search
  a2k.ask
  a2k.explain
  a2k.getDocument

Optional regulated tools/resources:
  a2k.validateCitation
  a2k.reportConflict
  a2k.streamAsk
  a2k.getAuditRecord
```

The Catalog MAY expose A2K-KRP as an MCP resource and tool set. MCP provides the integration layer; A2K provides the governance contract.

**A2A** 

A KB that is itself agentic, opaque, long-running, or workflow-oriented MAY be exposed as an A2A agent declaring the extension:

```json
{
  "uri": "urn:a2k:enterprise:profile:1.0",
  "required": false
}
```

The public Agent Card SHOULD reveal only non-sensitive capabilities; sensitive KB metadata SHOULD be available only through an authenticated extended card or catalog-mediated discovery.

**HTTPS + JSON** 

Car at:

```text
GET https://{kb-host}/.well-known/a2k-card.json
```

Operations:

```text
POST https://{kb-host}/a2k/{operation}
```

For sensitive KBs the well-known card MAY be a minimal public stub and the full card MUST require authentication.

## 13. Release gate

This baseline is not releasable without three machine-validation artifacts:

1. **Modular JSON Schema package** — one 2020-12 schema per contract object (KB Card, request-common, each operation, cited-response envelope, citation, claim, discovery query/response, conflict report, regulated extension, signature, error, proof footer), wired by `$id`/`$ref` under `urn:a2k:enterprise:schemas:*:0.6-baseline`, with a single controlled-vocabulary `$defs` registry so every enum in the suite is defined once.
2. **Conformance corpus** — every in-document JSON example plus negative cases, validated in CI, so version, enum, and schema drift cannot silently occur. The corpus is authoritative over prose examples on any discrepancy.
3. **Signature test vectors** — JCS RFC 8785 canonicalization and EdDSA detached-JWS vectors for card and response signatures.

The `schema-invalid` governance flag (KRP §6.11) is only meaningful once item 1 exists.

## 14. Adoption

### 14.1 Rollout sequence

1. **Catalog + KB Cards + governance metadata.** Stand up the Catalog; register all existing KBs at Level 0 (cards only). Collision detection and orphan flagging light up immediately with zero endpoint work.
2. **Authorization-scoped discovery.** Enable `resolve` so agents see only the KBs their caller may discover.
3. **Cited retrieval.** Add `search` with the cited-response envelope to the highest-value KBs (Level 1). This is the first useful production milestone.
4. **A proof-of-value knowledge agent.** Migrate one existing vertical agent from hard-wired bindings to resolve → search → cited answer, including refusal and conflict-surfacing paths.
5. **Grounded synthesis and conflict reporting.** Add `ask`, grounding, and cross-KB conflict handling for high-value KBs (Level 2+); introduce the Gateway for fan-out where needed.
6. **Regulated hardening.** Apply S2/Level 4 controls only to high-control domains — Legal, HR, Security, Finance, Compliance, Risk, and wall-side desks. Cryptographic signatures, fragment hashes, streaming proof footers, and data-lineage arrays are progressive controls, not day-one requirements. The baseline product is Level 0–1.

### 14.2 Per-team onboarding checklist

A KB-owning team brings a source online by completing:

1. **Claim scope.** Decide domains, topics, geography, department, product, temporal range, and completeness.
2. **Select authority level.** Request `canonical`, `scoped-canonical`, `scoped-guidance`, `vendor`, `derived`, `personal`, `draft`, `none`, or `unverified`.
3. **Assign owners.** Use a group owner and a monitored contact channel; individual owners create orphan risk.
4. **Set lifecycle.** Status, review deadline, attestation deadline, and expiry action.
5. **Classify data.** Map data classification and visibility to enterprise policy; this determines the security tier.
6. **Declare auth.** Schemes, scopes, and whether OBO assertions are required (mandatory at S1+).
7. **Publish card.** Serve the card locally and register it with the Catalog.
8. **Implement operations.** Start with `search`; add `ask` and `explain` only when ready.
9. **Return citations.** Stable document IDs, selectors, retrieval timestamps, and source metadata.
10. **Test access leakage.** Ensure unauthorized users cannot discover cards, citations, titles, URLs, document IDs, or conflict details. At S1+, this test is part of the mandatory security launch gate.
11. **Self-certify conformance.** Declare the target level and supported features.
12. **Catalog verification.** Let the Catalog validate schema, ownership, authority, lifecycle, and conformance.
13. **Pilot with real agent queries.** Validate source selection and refusals against live traffic.
14. **Add regulated controls only if required.** Do not impose Level 4 prematurely.

### 14.3 Component ownership and RACI (illustrative)

| Component | Typically built by | Description |
|---|---|---|
| Catalog service (A2K-KRP) | Platform / Infra | Index cards, assert authority, filter discovery, compute governance flags, run registration workflow, measure routing quality. |
| Shared client library | Platform / Infra | Card parsing, catalog lookup, envelope handling, OBO helper, citation verification, reconciliation helpers. |
| IAM integration | Security / IAM | OBO/token-exchange flow, owner resolution, classification-to-authorization mapping. |
| KB Cards + endpoints (A2K-KCP) | KB-owning teams | Their card, operations, citations, and conformance target. |
| Knowledge agent | Agent team | Discovery → OBO query → cited answer → refusal/conflict logic. |
| Authority approval workflow | Governance / Data office + Platform | Who may approve `canonical`, `scoped-canonical`, or `scoped-guidance` claims. |
| Security launch gate | Security / IAM | Access-leakage, OBO, caching, audit, privacy, and regulated-control review. |

| Activity | Platform | Security/IAM | KB teams | Agent team | Governance/Data office |
|---|:---:|:---:|:---:|:---:|:---:|
| Catalog build | R/A | C | I | I | C |
| Shared client library | R/A | C | C | C | I |
| OBO / token exchange | C | R/A | I | C | I |
| Card schema sign-off | R/A | C | C | I | C |
| Per-KB card + endpoints | I | I | R/A | I | I |
| SoR-claim approval | I | I | C | I | R/A |
| Knowledge agent | C | C | I | R/A | I |
| Security launch gate | I | R/A | C | C | C |

## 15. Terminology

**Profile** — A specification layered on existing protocols and infrastructure rather than a new protocol.

**Cited-response contract** — A2K's transport-neutral application-level contract (Layer B): read-only operations, common request fields, cited-response envelope, pagination, error model, conflict-report artifact, and streaming proof footer. It specifies request/response shape and semantics, not transport framing, so it applies identically over MCP, A2A, and HTTPS.

**Agent** — A software actor that uses an AI model or agentic system to complete tasks on behalf of a user, team, organization, or process.

**Knowledge Base (KB)** — An enterprise source of knowledge that can answer read-only queries, retrieve documents, or return grounded evidence.

**Document** — Any addressable unit of enterprise knowledge a KB can return or cite, independent of format: a text or word-processing document, web page, wiki page, table or database row/result set, JSON document, PDF, code file, ticket, or an addressable fragment of any of these.

**KB Card (Enterprise KB Card)** — A machine-readable JSON document describing a KB's identity, scope, ownership, lifecycle, authority, access requirements, policies, operations, and citation behavior. The unit of registration in the Catalog.

**Catalog** — The enterprise service implementing A2K-KRP: indexes KB Cards, confirms or downgrades authority claims, filters discovery by authorization, computes governance flags, and exposes governance signals to agents.

**Gateway** — An optional enterprise component that queries one or more KBs on behalf of an agent or user, applies policy, detects disagreement, and returns governed results, under the transparency duties of §8.1.

**Knowledge Owner** — The accountable business owner for a KB's correctness, scope, lifecycle, and governance status.

**Technical Owner** — The team or system accountable for operating, maintaining, or integrating the KB.

**System of Record (SoR)** — The authoritative enterprise source for a defined scope of knowledge.

**Authority Scope** — The domain, department, process, jurisdiction, product, policy, or temporal scope for which a KB is authoritative.

**Catalog-asserted authority** — The authority level the Catalog assigns after reviewing a KB's claim; trusted over the self-declaration when they differ.

**On-Behalf-Of (OBO) context** — The request context indicating the human user, service identity, or process whose permissions the agent is acting under. Mandatory for non-public KBs.

**OBO assertion token** — A signed, enterprise-verifiable assertion binding a request to a delegated subject identity, agent identity, entitlements, purpose, scopes, and time window. Mandatory and validated at tier S1+.

**Security tier (S0/S1/S2)** — The obligation band derived from a KB's data classification (§9.2).
**Cited-response envelope** — The standard response shape binding answers, passages, claims, citations, source spans, grounding, freshness, access decisions, and audit metadata.

**Citation** — A machine-readable reference to enterprise evidence supporting or refuting a passage or claim.

**Claim** — A discrete assertion returned by a KB (factual, procedural, policy, quantitative, temporal, etc.).

**Grounded answer** — An answer whose material assertions are supported by citations.

**Ungrounded span** — A part of an answer not backed by citation evidence.

**Strict grounding** — A mode in which a KB MUST NOT return any material unsupported assertion; every claim must be citation-backed or the KB must return a structured refusal.

**Text-quote selector** — A robust citation selector identifying source text by exact quoted content with optional prefix/suffix context (W3C Web Annotation `TextQuoteSelector`).

**Data lineage** — The auditable chain of processing states from system-of-record ingestion to the current document, chunk, vector, or retrieval representation.

**Aware-conflict** — An optional, informative self-report by a KB that its answer disagrees with another named source.

**Cross-KB conflict report** — A computed artifact recording that two or more KBs returned conflicting or materially different answers to the same question, produced by an agent or gateway.

**Governance flag** — A Catalog-computed signal that a KB has a governance problem: orphaned, stale, suspended, collision, schema-invalid, etc.

**Catalog health metrics** — Required Catalog-published measures of governance-graph quality (KRP §6.12).

**Trailing proof footer** — A terminal streaming payload carrying claims, citations, support metadata, access decisions, audit metadata, and optional signatures, sent after the answer text has streamed.

## 16. Roadmap

Planned or possible future work, signaling direction rather than commitment: 

* formal MCP binding test suite; 
* formal A2A extension payload schema;
* OpenAPI profile for the HTTPS binding; registry governance workflow APIs; 
* knowledge-owner approval workflow schemas; 
* automated stale-content notification; 
* conflict-escalation workflow integrations; 
* alignment with schema.org, DCAT, DataHub, Collibra, or Atlan vocabularies;
* partner/cross-enterprise catalog federation and delegated-authorization profiles; 
* privacy-preserving discovery; 
* source-quality sampling and verification records; policy-as-code integration; 
* richer typed-claim schemas; deterministic grounded computation; 
* human-review workflow integration;
* bulk import adapters for Confluence, SharePoint, OneDrive, Jira, ServiceNow, GitHub, and data catalogs; 
* and richer citation selectors for scanned documents, images, audio/video, charts, spreadsheet formulas, and knowledge-graph triples.
