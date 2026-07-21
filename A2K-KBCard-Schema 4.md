# A2K KB Card Schema

**Version:** 0.6-draft
**Contract string:** `0.6-baseline`
**Status:** Working draft / baseline for implementation
**Date:** 2026-07-07
**Audience:** KB-owning teams, Catalog and agent implementors, schema validators.

Normative keywords **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, **MAY**, and **OPTIONAL** follow RFC 2119 / RFC 8174 when used in all capitals.

---

## 1. Purpose

The Enterprise KB Card is the shared data structure of both A2K protocols. **A2K-KRP** uses it as the unit of registration and the result of discovery queries. **A2K-KCP** uses it as the capability advertisement an agent holds before querying a KB. It is also the unit of onboarding for a KB-owning team.

A KB Card describes what a knowledge base knows, who owns it, whether it is authoritative, how current it is, who may access it, which operations it supports, and which governance controls apply.

## 2. Version identifiers

A2K uses four distinct version axes, deliberately separated to prevent silent contract drift. Implementations MUST NOT conflate them.

| Axis | Where it appears | This baseline | Meaning |
|---|---|---|---|
| Document version | Front matter (`Version:`) | `1.0-draft` | Editorial maturity of a specification document. |
| Contract version (`a2kVersion`) | Every KB Card, request, and response | `0.6-baseline` | The A2K contract a card or message implements. This is the wire-level compatibility token. |
| Profile / extension identifier | `conformance.profile`, A2A extension `uri`, schema `$id` | `a2k-enterprise-v0.6-baseline`, `urn:a2k:enterprise:profile:1.0`, `urn:a2k:enterprise:schemas:*:0.6-baseline` | Identifies this profile and its schemas; tracks the contract version. |
| Card content version (`kbCardVersion`) | `audit.kbCardVersion` in responses | Per-card (e.g. `1.1.0`) | A KB owner's own revision number for an individual card's content. Independent of the contract version. |

Rules:

1. `a2kVersion` MUST equal `0.6-baseline` for cards and messages claiming conformance to this baseline.
2. Profile identifiers and schema `$id`s MUST track the contract version, not the document version.
3. `kbCardVersion` is owner-defined and MUST NOT be assumed to follow the contract version.
4. A consumer receiving an unrecognized `a2kVersion` SHOULD fail with `UNSUPPORTED_VERSION` rather than guessing.

## 3. Top-level structure

```json
{
  "a2kVersion": "0.6-baseline",
  "profile": "enterprise",
  "id": "urn:a2k:acme:legal:contract-templates",
  "name": "Legal — Standard Contract Templates",
  "description": "Canonical approved contract templates and clause library.",
  "url": "https://kb.internal.acme.com/legal-templates/a2k",
  "transport": "https-json",
  "enterprise": {
    "ownership": {
      "ownerTeam": "Legal Knowledge Management",
      "businessOwner": {
        "type": "group",
        "id": "grp:legal-knowledge-stewards",
        "identityProvider": "okta",
        "name": "Legal Knowledge Stewards",
        "contact": "legal-km@example.com"
      },
      "technicalOwner": {
        "type": "group",
        "id": "grp:ai-platform-kb-ops",
        "identityProvider": "okta",
        "name": "AI Platform KB Operations",
        "contact": "ai-platform@example.com"
      },
      "maintainers": [
        "legal-km-admins@example.com"
      ],
      "supportChannel": "#legal-km-help",
      "escalationPolicy": "Contact businessOwner for policy disputes or stale content."
    },
    "authority": {
      "systemOfRecord": "canonical",
      "sorScope": [
        "contract-templates",
        "standard-clauses"
      ],
      "authorityScope": "ACME standard contract templates, all regions",
      "relationship": "system-of-record",
      "publisher": "Group Legal",
      "proof": {
        "method": "governance-approval",
        "approvedBy": "Legal Policy Council",
        "approvalReference": "GRC-1842",
        "approvedAt": "2026-05-10T00:00:00Z"
      }
    },
    "lifecycle": {
      "status": "active",
      "createdAt": "2025-01-01T00:00:00Z",
      "lastUpdated": "2026-06-01T08:00:00Z",
      "reviewedAt": "2026-06-01T08:00:00Z",
      "reviewBy": "2026-12-01",
      "attestation": {
        "lastAttested": "2026-06-01T08:30:00Z",
        "nextAttestationDue": "2026-12-01T08:30:00Z",
        "attestedBy": "legal-km@example.com",
        "actionOnExpiry": "downgrade-to-unverified"
      },
      "deprecationDate": null,
      "supersededBy": null,
      "replacementKbId": null,
      "changeLogUrl": "https://kb.internal.example.com/legal-templates/changelog"
    },
    "access": {
      "visibility": "role-scoped",
      "dataClassification": "confidential",
      "audiences": [
        "employees",
        "legal",
        "sales-ops"
      ],
      "requiredScopes": [
        "legal.templates.read"
      ],
      "allowedDepartments": [
        "Legal",
        "Sales",
        "Procurement"
      ],
      "restrictedDepartments": [],
      "crossDepartmentUse": true,
      "externalUseAllowed": false,
      "requiresUserContext": true,
      "rowLevelSecurity": false,
      "ethicalWallSensitive": false
    }
  },
  "knowledgeProfile": {
    "domains": [
      "legal",
      "contracts"
    ],
    "topics": [
      "NDAs",
      "MSAs",
      "indemnification clauses"
    ],
    "coverage": {
      "scope": "ACME standard contract templates, all regions",
      "departments": [
        "Legal",
        "Sales",
        "Procurement"
      ],
      "geography": [
        "global"
      ],
      "businessUnits": [
        "all"
      ],
      "products": [],
      "temporalRange": {
        "from": "2019-01-01",
        "to": "present"
      },
      "completeness": "comprehensive"
    },
    "generation": "human-reviewed",
    "freshness": {
      "updateCadence": "weekly",
      "lastUpdated": "2026-06-01T08:00:00Z",
      "reviewedAt": "2026-06-01T08:00:00Z",
      "nextReviewDue": "2026-12-01T08:00:00Z",
      "stalenessPolicy": "flag stale if source >30 days old"
    },
    "languages": [
      "en"
    ],
    "sourceSystems": [
      "Confluence",
      "SharePoint"
    ]
  },
  "operations": [
    {
      "name": "search",
      "requiredLevel": 1,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "ask",
      "requiredLevel": 2,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "explain",
      "requiredLevel": 2,
      "inputModes": [
        "answerRef",
        "claimIds"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "getDocument",
      "requiredLevel": 1,
      "inputModes": [
        "documentId"
      ],
      "outputModes": [
        "document"
      ]
    }
  ],
  "auth": {
    "schemes": [
      "oauth2",
      "enterprise-iam",
      "oboAssertion"
    ],
    "oauth2": {
      "scopes": [
        "legal.templates.read"
      ]
    },
    "delegatedUserRequired": true,
    "serviceAccountAllowed": true,
    "oboAssertionRequired": true,
    "signedRequestRequired": false
  },
  "policies": {
    "allowedUse": [
      "answering",
      "summarization",
      "citation",
      "internal-search"
    ],
    "disallowedUse": [
      "external-disclosure",
      "model-training",
      "bulk-export"
    ],
    "citationPolicy": "required",
    "redistribution": "internal-citations-only",
    "queryRetention": "limited",
    "piiHandling": "redacted",
    "humanReviewRequiredFor": [
      "external-response",
      "legal-commitment"
    ],
    "sensitiveDecisionUse": "human-review-required"
  },
  "audit": {
    "loggingRequired": true,
    "immutableLogRequired": false,
    "logTarget": "enterprise-siem",
    "logRetention": "standard",
    "includeUserId": true,
    "includeAgentId": true,
    "includeCitationIds": true,
    "includeProofFooter": false
  },
  "regulatedExtension": {
    "supported": true,
    "features": [
      "obo-assertion",
      "strict-grounding",
      "text-quote-selectors",
      "data-lineage",
      "immutable-audit",
      "signed-card",
      "signed-response",
      "conflict-escalation",
      "streaming-proof-footer"
    ],
    "regulatedDomains": [
      "finance",
      "compliance",
      "legal"
    ],
    "minimumAuditRetention": "7y",
    "immutableAuditRequired": true
  },
  "conformance": {
    "level": 2,
    "profile": "a2k-enterprise-v0.6-baseline",
    "features": [
      "kb-card",
      "ownership",
      "authority",
      "lifecycle",
      "access-policy",
      "search",
      "ask",
      "citations",
      "freshness",
      "audit"
    ]
  },
  "signature": null
}
```

| Field | Required | Type | Description |
|---|---:|---|---|
| `a2kVersion` | Yes | string | MUST be `0.6-baseline` (§2). |
| `profile` | Yes | string | MUST be `enterprise`. |
| `id` | Yes | string | Globally unique within the organization. SHOULD be a URN: `urn:a2k:{org}:{domain}:{slug}`. |
| `name` | Yes | string | Human-readable KB name. Subject to discovery leakage rules. |
| `description` | Yes | string | Short description. Subject to discovery leakage rules. |
| `url` | Yes | string | Base endpoint, MCP server reference, A2A endpoint, or canonical gateway route. |
| `transport` | Yes | string | Wire protocol used to reach `url`. MUST be one of `https-json`, `mcp`, `a2a`. |
| `enterprise` | Yes | object | Ownership, authority, lifecycle, and access metadata. All four members REQUIRED. |
| `knowledgeProfile` | Yes | object | Domains, topics, scope, coverage, generation, freshness, languages, source systems. |
| `operations` | Yes | array | Supported A2K-KCP operations. |
| `auth` | Yes | object | Authentication and authorization metadata (tier-bound rules, §7). |
| `policies` | Recommended | object | Usage, retention, redistribution, PII, and citation policies. |
| `audit` | Recommended | object | Audit behavior and logging metadata. Required at tier S1+. |
| `regulatedExtension` | Optional | object | Regulated-mode features supported by this KB. |
| `conformance` | Recommended | object | Declared capability level; the Catalog verifies it. |
| `signature` | S2 / Level 4 | object/null | Detached signature or enterprise attestation over the card. |

### 3.1 Card discovery endpoints

For `https-json`, a card SHOULD be served at:

```text
GET https://{kb-host}/.well-known/a2k-card.json
```

For sensitive (S1+) KBs, the well-known card MAY be a minimal public stub and the full card MUST require authentication; the stub MUST NOT include fields that violate the leakage rules for anonymous callers.

For `mcp`, a card SHOULD be exposed as the resource `a2k://card`. For `a2a`, A2K metadata SHOULD appear as an Agent Card extension, with sensitive metadata behind authenticated extended-card access or catalog-mediated discovery.

Every production KB MUST register its card with the Catalog before production use by enterprise agents. The well-known endpoint is the KB-local source; the Catalog holds the indexed, authority-annotated copy.

## 4. The `enterprise` block

The `enterprise` block is REQUIRED and all four members (`ownership`, `authority`, `lifecycle`, `access`) are REQUIRED. They travel together because discovery, authority assertion, and access filtering each depend on more than one of them at once — the Catalog cannot rank or safely disclose a KB knowing only who owns it but not what it claims authority over, whether it is still in review, or who may see it. A card missing any member is not discoverable at Level 0.

### 4.1 Ownership

```json
{
  "ownership": {
    "ownerTeam": "Legal Knowledge Management",
    "businessOwner": {
      "type": "group",
      "id": "grp:legal-knowledge-stewards",
      "identityProvider": "okta",
      "name": "Legal Knowledge Stewards",
      "contact": "legal-km@example.com"
    },
    "technicalOwner": {
      "type": "group",
      "id": "grp:ai-platform-kb-ops",
      "identityProvider": "okta",
      "name": "AI Platform KB Operations",
      "contact": "ai-platform@example.com"
    },
    "maintainers": [
      "legal-km-admins@example.com"
    ],
    "supportChannel": "#legal-km-help",
    "escalationPolicy": "Contact businessOwner for policy disputes or stale content."
  }
}
```

Every discoverable enterprise KB MUST declare ownership. The block MUST include `ownerTeam`; at least one of `businessOwner` / `technicalOwner` (preferably both); at least one monitored contact or support channel; and owner identifiers that resolve to active principals in the enterprise identity provider.

**For production KBs (any KB eligible for agent traffic), `businessOwner.type` MUST be `group`.** Individual ownership is permitted only for cards with `personal` or `draft` authority — individual owners create orphan risk when employees leave.

The Catalog MUST mark as `orphaned` any KB whose owners fail IAM resolution and MUST exclude orphaned S1+ KBs from discovery until ownership is restored (A2K-KRP §6).

### 4.2 Authority

Enterprise authority is scoped: a KB MUST NOT imply authority outside its declared `authorityScope` or `sorScope`.

```json
{
  "authority": {
    "systemOfRecord": "canonical",
    "sorScope": [
      "contract-templates",
      "standard-clauses"
    ],
    "authorityScope": "ACME standard contract templates, all regions",
    "relationship": "system-of-record",
    "publisher": "Group Legal",
    "proof": {
      "method": "governance-approval",
      "approvedBy": "Legal Policy Council",
      "approvalReference": "GRC-1842",
      "approvedAt": "2026-05-10T00:00:00Z"
    }
  }
}
```

`systemOfRecord` MUST be one of:

| Value | Rank | Meaning |
|---|---:|---|
| `canonical` | 6 | Enterprise-wide system of record for `sorScope`. SHOULD be exactly one per scope; the Catalog flags collisions. |
| `scoped-canonical` | 5 | System of record within a defined scope (team, region, product, or business-unit instance). |
| `scoped-guidance` | 4 | Maintained guidance within a defined scope; not a system of record. |
| `vendor` | 3 | Provided by an approved vendor or partner under enterprise agreement. |
| `derived` | 2 | Derived from another system of record. |
| `personal` | 1 | Individual or small-group working knowledge. Useful but not authoritative. |
| `draft` | 0 | Work in progress. MUST NOT be treated as authoritative for decisions. |
| `none` | — | Makes no SoR claim. |
| `unverified` | — | No verified authority relationship. |

**Rank** defines the ordering used by `minSystemOfRecord` in A2K-KRP discovery filters: a higher rank is a stronger claim, and a filter of `minSystemOfRecord: "scoped-guidance"` admits `scoped-guidance` and above. `none` and `unverified` make no ordered claim and never satisfy a `minSystemOfRecord` floor. Rank orders the strength of the claim only; scope match is evaluated before rank (§4.2.1).

`relationship` SHOULD be one of: `system-of-record`, `authorized-mirror`, `official-translation`, `derived-from-system-of-record`, `department-guidance`, `team-guidance`, `commentary`, `working-notes`, `vendor-source`, `unknown`.

**Authority is asserted by the Catalog, not the card.** A KB declaring `"systemOfRecord": "canonical"` proves nothing by itself. The flow is: 

- (1) the card requests an SoR level; 

- (2) the Catalog confirms, downgrades, or rejects the request through a registration/approval workflow keyed to enterprise governance and IAM facts; 

- (3) clients MUST trust the Catalog's asserted authority over the card's self-declared value when they differ.

**Authority proof.** A claim of `canonical`, `scoped-canonical`, or `scoped-guidance` MUST include a `proof` object acceptable to enterprise policy — recording the method (e.g. governance approval, IAM group entitlement, signed attestation), the approving body, an approval reference, and a timestamp. A claim without acceptable proof MUST be downgraded to `unverified` at registration. Approval evidence is cheap for legitimate claimants; requiring it prevents authority inflation.

#### 4.2.1 Scope beats rank

A higher SoR level does not automatically win if its scope does not match the query. A global canonical expense policy may be authoritative for global defaults, but an EMEA scoped-canonical policy may outrank it for a question about EMEA-specific reimbursement rules. Reconciliation MUST check scope, geography, business unit, jurisdiction, product, and temporal match before authority level.

#### 4.2.2 Collisions

When two or more KBs claim overlapping `canonical` authority for the same scope, the Catalog MUST surface a governance exception to the relevant owners and MUST NOT silently pick a winner. Until resolved, agents SHOULD treat the topic as disputed and surface the collision, subject to access-leakage rules.

### 4.3 Lifecycle

```json
{
  "lifecycle": {
    "status": "active",
    "createdAt": "2025-01-01T00:00:00Z",
    "lastUpdated": "2026-06-01T08:00:00Z",
    "reviewedAt": "2026-06-01T08:00:00Z",
    "reviewBy": "2026-12-01",
    "attestation": {
      "lastAttested": "2026-06-01T08:30:00Z",
      "nextAttestationDue": "2026-12-01T08:30:00Z",
      "attestedBy": "legal-km@example.com",
      "actionOnExpiry": "downgrade-to-unverified"
    },
    "deprecationDate": null,
    "supersededBy": null,
    "replacementKbId": null,
    "changeLogUrl": "https://kb.internal.example.com/legal-templates/changelog"
  }
}
```

`status` SHOULD be one of: `active`, `deprecated`, `superseded`, `draft`, `archived`, `suspended`, `unknown`.
`actionOnExpiry` SHOULD be one of: `notify-owner`, `mark-stale`, `downgrade-to-unverified`, `quarantine-isolate`, `revoke-discovery`.

Lifecycle rules:

1. `canonical` KBs MUST carry approval metadata and MUST NOT remain catalog-asserted `canonical` past their review or attestation due date unless enterprise policy permits a defined grace period.
2. `deprecated` KBs SHOULD declare `replacementKbId` or `supersededBy` where available.
3. `archived` KBs MUST NOT be used for current answers unless explicitly requested.
4. KBs past `reviewBy` MUST be flagged `stale-governance`.
5. **KBs past `nextAttestationDue` MUST have `actionOnExpiry` executed automatically by the Catalog** — expiry has consequences without human intervention (A2K-KRP §6.9). For SoR-claiming cards, `actionOnExpiry` MUST be `downgrade-to-unverified` or stronger; `notify-owner` alone is insufficient for authority-bearing cards.
6. `suspended` KBs MUST NOT be used unless an explicit, audited emergency-override policy allows it.

### 4.4 Access

```json
{
  "access": {
    "visibility": "role-scoped",
    "dataClassification": "confidential",
    "audiences": [
      "employees",
      "legal",
      "sales-ops"
    ],
    "requiredScopes": [
      "legal.templates.read"
    ],
    "allowedDepartments": [
      "Legal",
      "Sales",
      "Procurement"
    ],
    "restrictedDepartments": [],
    "crossDepartmentUse": true,
    "externalUseAllowed": false,
    "requiresUserContext": true,
    "rowLevelSecurity": false,
    "ethicalWallSensitive": false
  }
}
```

`dataClassification` SHOULD be one of: `public`, `internal`, `confidential`, `restricted`, `highly-restricted`, `regulated`, `unknown`.

`visibility` SHOULD be one of: `public-internal`, `role-scoped`, `need-to-know`, `hidden`.

A client, gateway, or agent MUST NOT query a KB unless the effective OBO identity satisfies the KB's access requirements. A KB response SHOULD include the access decision applied to the request (A2K-KCP §10).

**Declarative metadata versus enforcement.** The card's `access` block is declarative metadata for discovery filtering; it does not replace the KB's own enforcement. The KB endpoint remains the authoritative enforcement point against its native ACLs on every request. Deployments SHOULD periodically reconcile card metadata against actual endpoint behavior; drift raises the `access-policy-drift` governance flag (A2K-KRP §6.11).

#### 4.4.1 Derived security tier

| `dataClassification` | Tier |
|---|---|
| `public`, `internal` | **S0** |
| `confidential` | **S1** |
| `restricted`, `highly-restricted`, `regulated` | **S2** |
| `unknown` | Treated as **S1** minimum; **S2** if `ethicalWallSensitive` |

The tier is computed, never declared, and cannot be lowered by card content. `ethicalWallSensitive: true` forces S2 regardless of classification. Tier obligations: A2K-Overview §9.2, A2K-KRP §8, A2K-KCP §3 and §18.

## 5. Knowledge profile

The `knowledgeProfile` object describes what the KB knows.

```json
{
  "knowledgeProfile": {
    "domains": [
      "legal",
      "contracts"
    ],
    "topics": [
      "NDAs",
      "MSAs",
      "indemnification clauses"
    ],
    "coverage": {
      "scope": "ACME standard contract templates, all regions",
      "departments": [
        "Legal",
        "Sales",
        "Procurement"
      ],
      "geography": [
        "global"
      ],
      "businessUnits": [
        "all"
      ],
      "products": [],
      "temporalRange": {
        "from": "2019-01-01",
        "to": "present"
      },
      "completeness": "comprehensive"
    },
    "generation": "human-reviewed",
    "freshness": {
      "updateCadence": "weekly",
      "lastUpdated": "2026-06-01T08:00:00Z",
      "reviewedAt": "2026-06-01T08:00:00Z",
      "nextReviewDue": "2026-12-01T08:00:00Z",
      "stalenessPolicy": "flag stale if source >30 days old"
    },
    "languages": [
      "en"
    ],
    "sourceSystems": [
      "Confluence",
      "SharePoint"
    ]
  }
}
```

**Domains and topics.** `domains` MUST be a non-empty array of broad categories, e.g.: `hr`, `finance`, `legal`, `security`, `engineering`, `product`, `sales`, `support`, `operations`, `procurement`, `compliance`, `risk`, `data-governance`, `enterprise-policy`, `technical-documentation`. `topics` SHOULD be narrower labels, e.g.: `expense-policy`, `employee-benefits`, `incident-response`, `contract-approval`, `sales-discounting`, `customer-escalation`, `data-retention`, `access-review`.

**Coverage.** `coverage.scope` MUST describe the KB's intended scope in human-readable form. `coverage.completeness` SHOULD be one of `comprehensive`, `partial`, `sampled`, `experimental`, `unknown`. A KB MUST NOT imply comprehensive coverage outside its declared scope.

**Generation.** `generation` SHOULD be one of `human`, `human-reviewed`, `automated`, `mixed`, `unknown`. Clients SHOULD prefer `human` or `human-reviewed` sources for regulated or high-impact decisions, without over-weighting the signal as LLM-assisted authoring becomes common.

**Freshness.** `freshness.updateCadence` SHOULD be one of `realtime`, `hourly`, `daily`, `weekly`, `monthly`, `quarterly`, `irregular`, `static`, `unknown`. `lastUpdated`, `reviewedAt`, `nextReviewDue`, and `stalenessPolicy` SHOULD be present for Level 1+ and MUST be present for Level 3+. The Catalog MAY compare declared cadence against observed updates and raise `freshness-drift`.

**Routing note.** `domains`, `topics`, and `coverage.scope` are the primary inputs to Catalog topic matching. KB teams SHOULD treat them as routing-critical: vague or inflated scope text degrades resolution quality for everyone, and the Catalog's routing-quality harness (A2K-KRP §6.12) will attribute misroutes to the offending card.

## 6. Operations

```json
{
  "operations": [
    {
      "name": "search",
      "requiredLevel": 1,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "ask",
      "requiredLevel": 2,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "explain",
      "requiredLevel": 2,
      "inputModes": [
        "answerRef",
        "claimIds"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "getDocument",
      "requiredLevel": 1,
      "inputModes": [
        "documentId"
      ],
      "outputModes": [
        "document"
      ]
    }
  ]
}
```

Valid `name` values: `search`, `ask`, `explain`, `getDocument`. All A2K-KCP operations are read-only.

## 7. Authentication metadata

The `auth` object declares what the KB expects. A2K does not define the underlying IAM protocol; it mandates the *property* that subject identity and entitlements reach the KB verifiably where the tier requires it.

```json
{
  "auth": {
    "schemes": [
      "oauth2",
      "enterprise-iam",
      "oboAssertion"
    ],
    "oauth2": {
      "scopes": [
        "legal.templates.read"
      ]
    },
    "delegatedUserRequired": true,
    "serviceAccountAllowed": true,
    "oboAssertionRequired": true,
    "signedRequestRequired": false
  }
}
```

Supported `schemes` MAY include: `none`, `apiKey`, `oauth2`, `oidc`, `mtls`, `signedRequest`, `enterprise-iam`, `oboAssertion`. `none` means internally public.

Rules:

1. Non-public KBs MUST support an identity-bearing scheme capable of carrying effective OBO context.
2. **At tier S1+, `oboAssertionRequired` MUST be `true`** and the KB MUST validate the signed assertion per A2K-KCP §14.1. A card at S1+ declaring `oboAssertionRequired: false` fails registration validation.
3. `signedRequestRequired` is optional and typical at S2.

## 8. Policies

```json
{
  "policies": {
    "allowedUse": [
      "answering",
      "summarization",
      "citation",
      "internal-search"
    ],
    "disallowedUse": [
      "external-disclosure",
      "model-training",
      "bulk-export"
    ],
    "citationPolicy": "required",
    "redistribution": "internal-citations-only",
    "queryRetention": "limited",
    "piiHandling": "redacted",
    "humanReviewRequiredFor": [
      "external-response",
      "legal-commitment"
    ],
    "sensitiveDecisionUse": "human-review-required"
  }
}
```

- `citationPolicy` SHOULD be one of `required`, `recommended`, `not-required`, `prohibited`.
-  `queryRetention` SHOULD be one of `none`, `session`, `limited`, `standard`, `extended`, `unknown`; KBs at classification 
- `confidential` and above SHOULD default to `none`, `session`, or `limited`. 
- `piiHandling` SHOULD be one of `not-allowed`, `allowed`, `redacted`, `contractual`, `unknown`. 
- `sensitiveDecisionUse` SHOULD be one of `allowed`, `human-review-required`, `not-allowed`, `unknown`.

## 9. Audit declaration

```json
{
  "audit": {
    "loggingRequired": true,
    "immutableLogRequired": false,
    "logTarget": "enterprise-siem",
    "logRetention": "standard",
    "includeUserId": true,
    "includeAgentId": true,
    "includeCitationIds": true,
    "includeProofFooter": false
  }
}
```

At S1+, `loggingRequired`, `includeUserId`, and `includeAgentId` MUST be `true`. 

At S2, where enterprise policy designates responses as records, `immutableLogRequired` SHOULD be `true` with a WORM-capable `logTarget` (e.g. `enterprise-worm-archive`) and a compliance-set `logRetention`.

 A2K does not mandate a specific storage system: WORM storage, immutable object storage, append-only ledgers, SIEM, GRC, or audit platforms all qualify.

## 10. Regulated extension

A KB Card MAY declare regulated capabilities. A client or gateway MAY require specific regulated features based on risk level, domain, user, purpose, jurisdiction, or enterprise policy.

```json
{
  "regulatedExtension": {
    "supported": true,
    "features": [
      "obo-assertion",
      "strict-grounding",
      "text-quote-selectors",
      "data-lineage",
      "immutable-audit",
      "signed-card",
      "signed-response",
      "conflict-escalation",
      "streaming-proof-footer"
    ],
    "regulatedDomains": [
      "finance",
      "compliance",
      "legal"
    ],
    "minimumAuditRetention": "7y",
    "immutableAuditRequired": true
  }
}
```

## 11. Conformance declaration

```json
{
  "conformance": {
    "level": 2,
    "profile": "a2k-enterprise-v0.6-baseline",
    "features": [
      "kb-card",
      "ownership",
      "authority",
      "lifecycle",
      "access-policy",
      "search",
      "ask",
      "citations",
      "freshness",
      "audit"
    ]
  }
}
```

The Catalog verifies the declared level against the KB's actual capabilities. If the KB does not satisfy the declared level, the Catalog MUST set the `conformance-mismatch` governance flag and MUST assert the highest level actually satisfied. The declared level describes capability only; it never modifies the security tier of §4.4.1.

## 12. Card signature (S2 / Level 4)

```json
{
  "signature": {
    "alg": "EdDSA",
    "kid": "https://catalog.example.com/jwks.json#card-key-1",
    "canonicalization": "JCS-RFC8785",
    "jws": "<detached-signature>"
  }
}
```

The `signature` field itself MUST be excluded from the signed payload. Signed cards SHOULD be signed by the Catalog or enterprise PKI after authority approval. Canonicalization and verification test vectors are part of the release-gate package (A2K-Overview §13).

## 13. Complete example

```json
{
  "a2kVersion": "0.6-baseline",
  "profile": "enterprise",
  "id": "urn:a2k:enterprise:finance-expense-policy",
  "name": "Finance Expense Policy KB",
  "description": "Approved enterprise expense policy and reimbursement guidance.",
  "url": "mcp://finance-policy-kb",
  "transport": "mcp",
  "enterprise": {
    "ownership": {
      "ownerTeam": "Finance Operations",
      "businessOwner": {
        "type": "group",
        "id": "grp:finance-policy-council",
        "identityProvider": "okta",
        "name": "Finance Policy Council",
        "contact": "finance-policy@example.com"
      },
      "technicalOwner": {
        "type": "group",
        "id": "grp:ai-platform-team",
        "identityProvider": "okta",
        "name": "AI Platform Team",
        "contact": "ai-platform@example.com"
      },
      "maintainers": [
        "expense-policy-admins@example.com"
      ],
      "supportChannel": "#finance-policy-help",
      "escalationPolicy": "Contact businessOwner for policy disputes or stale content."
    },
    "authority": {
      "systemOfRecord": "canonical",
      "sorScope": [
        "employee-expenses",
        "air-travel-policy"
      ],
      "authorityScope": "Global employee expense reimbursement policy",
      "relationship": "system-of-record",
      "publisher": "Finance Operations",
      "proof": {
        "method": "governance-approval",
        "approvedBy": "Finance Policy Council",
        "approvalReference": "GRC-1842",
        "approvedAt": "2026-05-10T00:00:00Z"
      }
    },
    "lifecycle": {
      "status": "active",
      "createdAt": "2025-01-01T00:00:00Z",
      "lastUpdated": "2026-05-10T00:00:00Z",
      "reviewedAt": "2026-05-10T00:00:00Z",
      "reviewBy": "2026-08-10",
      "attestation": {
        "lastAttested": "2026-05-15T08:30:00Z",
        "nextAttestationDue": "2026-11-15T08:30:00Z",
        "attestedBy": "finance-policy@example.com",
        "actionOnExpiry": "downgrade-to-unverified"
      },
      "deprecationDate": null,
      "supersededBy": null,
      "replacementKbId": null,
      "changeLogUrl": "https://kb.example.com/finance-expense-policy/changelog"
    },
    "access": {
      "visibility": "public-internal",
      "dataClassification": "internal",
      "audiences": [
        "employees",
        "managers"
      ],
      "requiredScopes": [
        "finance.policy.read"
      ],
      "allowedDepartments": [
        "All"
      ],
      "restrictedDepartments": [],
      "crossDepartmentUse": true,
      "externalUseAllowed": false,
      "requiresUserContext": true,
      "rowLevelSecurity": false,
      "ethicalWallSensitive": false
    }
  },
  "knowledgeProfile": {
    "domains": [
      "finance",
      "employee-policy"
    ],
    "topics": [
      "expenses",
      "reimbursements",
      "travel-policy"
    ],
    "coverage": {
      "scope": "Global employee expense reimbursement policy",
      "departments": [
        "Finance",
        "People Operations"
      ],
      "geography": [
        "global"
      ],
      "businessUnits": [
        "all"
      ],
      "products": [],
      "temporalRange": {
        "from": "2025-01-01",
        "to": "present"
      },
      "completeness": "comprehensive"
    },
    "generation": "human-reviewed",
    "freshness": {
      "updateCadence": "quarterly",
      "lastUpdated": "2026-05-10T00:00:00Z",
      "reviewedAt": "2026-05-10T00:00:00Z",
      "nextReviewDue": "2026-08-10T00:00:00Z",
      "stalenessPolicy": "flag stale after nextReviewDue"
    },
    "languages": [
      "en"
    ],
    "sourceSystems": [
      "Confluence",
      "Workday"
    ]
  },
  "operations": [
    {
      "name": "search",
      "requiredLevel": 1,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "ask",
      "requiredLevel": 2,
      "inputModes": [
        "text"
      ],
      "outputModes": [
        "cited-response-envelope"
      ]
    },
    {
      "name": "getDocument",
      "requiredLevel": 1,
      "inputModes": [
        "documentId"
      ],
      "outputModes": [
        "document"
      ]
    }
  ],
  "auth": {
    "schemes": [
      "oauth2",
      "enterprise-iam"
    ],
    "oauth2": {
      "scopes": [
        "finance.policy.read"
      ]
    },
    "delegatedUserRequired": true,
    "serviceAccountAllowed": true,
    "oboAssertionRequired": false,
    "signedRequestRequired": false
  },
  "policies": {
    "allowedUse": [
      "answering",
      "summarization",
      "citation",
      "internal-search"
    ],
    "disallowedUse": [
      "external-disclosure",
      "model-training",
      "bulk-export"
    ],
    "citationPolicy": "required",
    "redistribution": "internal-citations-only",
    "queryRetention": "limited",
    "piiHandling": "redacted",
    "humanReviewRequiredFor": [
      "external-response",
      "legal-commitment"
    ],
    "sensitiveDecisionUse": "human-review-required"
  },
  "audit": {
    "loggingRequired": true,
    "immutableLogRequired": false,
    "logRetention": "standard",
    "includeUserId": true,
    "includeAgentId": true,
    "includeCitationIds": true
  },
  "regulatedExtension": {
    "supported": true,
    "features": [
      "obo-assertion",
      "strict-grounding",
      "text-quote-selectors",
      "conflict-escalation"
    ],
    "regulatedDomains": [
      "finance"
    ],
    "immutableAuditRequired": false
  },
  "conformance": {
    "level": 3,
    "profile": "a2k-enterprise-v0.6-baseline",
    "features": [
      "kb-card",
      "ownership",
      "authority",
      "lifecycle",
      "attestation",
      "access-policy",
      "search",
      "ask",
      "citations",
      "freshness",
      "audit"
    ]
  },
  "signature": null
}
```

Note: this card's `dataClassification` is `internal` (tier S0), so `oboAssertionRequired: false` is valid. A `confidential` card with the same auth block would fail registration validation.

## 14. Schema package and validation rules

The normative machine-readable contract is the modular JSON Schema package (A2K-Overview §13): one 2020-12 schema per contract object, wired by `$id`/`$ref` under `urn:a2k:enterprise:schemas:*:0.6-baseline`, with a single controlled-vocabulary `$defs` registry holding every enum in this document. The conformance corpus validates every in-document example plus negative cases in CI, and is authoritative over prose examples on any discrepancy.

Cross-field rules that JSON Schema cannot express are specified as named validation rules, implemented in the Catalog's registration validator, each with a negative case in the corpus:

| Rule | Constraint |
|---|---|
| `A2K-VAL-001` | Security tier derivation per §4.4.1; `ethicalWallSensitive` forces S2. |
| `A2K-VAL-002` | `oboAssertionRequired` MUST be `true` at tier S1+. |
| `A2K-VAL-003` | SoR claims (`canonical`, `scoped-canonical`, `scoped-guidance`) MUST carry acceptable `proof`; otherwise downgrade to `unverified`. |
| `A2K-VAL-004` | Production cards MUST have group `businessOwner`. |
| `A2K-VAL-005` | SoR-claiming cards MUST set `actionOnExpiry` to `downgrade-to-unverified` or stronger. |
| `A2K-VAL-006` | At S1+, `audit.loggingRequired`, `includeUserId`, and `includeAgentId` MUST be `true`. |

### 14.1 KB Card schema fragment (illustrative)

The following fragment constrains the top-level shape and highest-drift fields. It is illustrative; the schema package is the executable contract.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:a2k:enterprise:schemas:kb-card:0.6-baseline",
  "type": "object",
  "required": [
    "a2kVersion",
    "profile",
    "id",
    "name",
    "description",
    "url",
    "transport",
    "enterprise",
    "knowledgeProfile",
    "operations",
    "auth"
  ],
  "properties": {
    "a2kVersion": {
      "const": "0.6-baseline"
    },
    "profile": {
      "const": "enterprise"
    },
    "id": {
      "type": "string"
    },
    "name": {
      "type": "string"
    },
    "description": {
      "type": "string"
    },
    "url": {
      "type": "string"
    },
    "transport": {
      "enum": [
        "https-json",
        "mcp",
        "a2a"
      ]
    },
    "enterprise": {
      "type": "object",
      "required": [
        "ownership",
        "authority",
        "lifecycle",
        "access"
      ]
    },
    "knowledgeProfile": {
      "type": "object",
      "required": [
        "domains",
        "coverage",
        "generation",
        "freshness",
        "languages"
      ],
      "properties": {
        "domains": {
          "type": "array",
          "minItems": 1
        }
      }
    },
    "operations": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": [
          "name"
        ],
        "properties": {
          "name": {
            "enum": [
              "search",
              "ask",
              "explain",
              "getDocument"
            ]
          }
        }
      }
    },
    "auth": {
      "type": "object",
      "required": [
        "schemes"
      ]
    }
  }
}
```
