# A2K-KRP — Knowledge Resolution Protocol

**Version:** 0.6-draft
**Contract string:** `0.6-baseline`
**Status:** Working draft / baseline for implementation
**Date:** 2026-07-07
**Audience:** Enterprise AI platform teams, Catalog implementors, agent developers, security and IAM architects.

Normative keywords **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, **MAY**, and **OPTIONAL** follow RFC 2119 / RFC 8174 when used in all capitals.

---

## 1. Purpose and scope

A2K-KRP is the **knowledge resolution protocol**. Given a query intent and a caller identity, it returns the set of knowledge bases that are authoritative, accessible, and appropriate for that caller and question. Its role in the A2K stack is analogous to DNS in the internet stack: an agent calls `resolve` before calling `ask`, as a browser resolves a hostname before fetching.

A2K-KRP defines:

- The `resolve`, `register`, and `getCard` operations
- The Catalog's responsibilities and authority-assertion model
- Eligibility gates, ranking signals, and routing-quality obligations
- Governance flags computed per KB Card
- Cursor-based pagination for discovery
- Hierarchical resolution (division → group)
- Access-leakage rules that make discovery safe
- The registration and authority-approval workflow

A2K-KRP does **not** define: how to query knowledge from a KB (A2K-KCP); the content of KB responses; a new IAM or transport protocol; or the internal implementation of any Catalog.

The Catalog is the reference implementation of A2K-KRP. An enterprise MAY run multiple Catalogs in a hierarchy (§7).

## 2. The DNS analogy — and its normative limits

Where the analogy holds: A2K-KRP is infrastructure that runs before the application protocol; the Catalog is an authoritative name service for enterprise knowledge; KB Cards are resource-record-like; results are cacheable and hierarchical and operationally independent of the knowledge being retrieved.

Where it ends — and these differences are normative:

1. **Resolution is identity-scoped.** DNS resolution is access-blind; anyone can resolve a hostname. A2K-KRP responses MUST be filtered by the caller's OBO identity. Cache keys MUST include effective authorization context; a response served to one subject MUST NOT be served to a subject with lesser access.
2. **Resolution is semantic.** The Catalog performs topic matching between query intent and card metadata. Routing is therefore a *quality* problem, not just a lookup problem — hence the routing-quality obligations of §6.12, which have no DNS counterpart.
3. **Resolution is policy-bearing.** The Catalog evaluates authorization during discovery, not only at the KB endpoint; it is IAM-aware by construction, and `resolve` carries OBO context just as `ask` does.

Implementors sizing a Catalog against DNS operational assumptions (stateless, globally cacheable, access-blind) are sizing the wrong system.

## 3. Operations

### 3.1 `resolve`

`resolve` is the primary operation. Given a query intent and OBO context, it returns the ranked, access-filtered, eligibility-gated set of KB Cards for that caller and question.

**Request:**

```json
{
  "a2kVersion": "1.0-baseline",
  "operation": "resolve",
  "query": "Can employees expense business-class flights?",
  "onBehalfOf": {
    "subject": "user:employee@example.com",
    "subjectIdP": "okta",
    "department": "Sales",
    "location": "US",
    "roles": [
      "account-executive"
    ],
    "entitlements": [
      "finance.policy.read"
    ],
    "clearanceLevel": "internal",
    "purpose": "answer_user_question",
    "oboAssertionToken": "<signed-entitlement-assertion>"
  },
  "filters": {
    "requiredDomains": [
      "finance",
      "employee-policy"
    ],
    "requiredTopics": [
      "expenses",
      "travel"
    ],
    "requiredOperations": [
      "ask"
    ],
    "minSystemOfRecord": "scoped-guidance",
    "maxStalenessHours": 2160,
    "jurisdiction": "US",
    "dateAsOf": "2026-07-07",
    "allowedClassifications": [
      "public",
      "internal"
    ],
    "requireApprovedSources": true,
    "riskLevel": "medium"
  },
  "pagination": {
    "limit": 5,
    "cursor": null
  },
  "requestMetadata": {
    "requestId": "resolve-req-001",
    "traceId": "trace-abc"
  }
}
```

**Request fields:**

| Field | Required | Description |
|---|---:|---|
| `a2kVersion` | Yes | MUST be `1.0-baseline`. |
| `operation` | Yes | MUST be `resolve`. |
| `query` | Recommended | Natural-language query intent used to rank KBs by topic match. |
| `onBehalfOf` | Yes for any resolution that may return non-public KBs | Caller identity and entitlements. |
| `onBehalfOf.oboAssertionToken` | Required for S1+ disclosure | Signed OBO assertion (A2K-KCP §14.1). |
| `filters` | Optional | Constraints on domains, topics, operations, authority level, freshness, classification, jurisdiction, risk. |
| `filters.minSystemOfRecord` | Optional | Minimum SoR level by rank (A2K-KBCard-Schema §4.2). |
| `filters.maxStalenessHours` | Optional | Exclude KBs whose `lastUpdated` is older than this. |
| `filters.riskLevel` | Optional | One of `low`, `medium`, `high`. Influences eligibility for regulated-only KBs. |
| `pagination` | Optional | Cursor-based pagination (§5). |
| `requestMetadata` | Optional | Tracing and logging. |

**OBO rule.** The `oboAssertionToken` MUST be present and valid for the response to include any tier S1+ KB. If the token is absent or invalid, the Catalog MUST behave as if S1+ KBs do not exist for this caller (existence concealment, §8). Plain claims (`department`, `roles`, `entitlements`) are routing hints for S0 filtering only; disclosure decisions at S1+ MUST rest on the validated assertion, never on self-asserted attributes.

**Response:**

```json
{
  "a2kVersion": "1.0-baseline",
  "ok": true,
  "operation": "resolve",
  "results": [
    {
      "kbCard": {
        "id": "urn:a2k:enterprise:finance-expense-policy",
        "name": "Finance Expense Policy KB",
        "url": "mcp://finance-policy-kb",
        "transport": "mcp",
        "operations": [
          "search",
          "ask",
          "getDocument"
        ]
      },
      "catalogAssertions": {
        "systemOfRecord": "canonical",
        "authorityScope": "Global employee expense reimbursement policy",
        "assertedAt": "2026-06-01T08:00:00Z",
        "assertedBy": "enterprise-catalog"
      },
      "signals": {
        "scopeMatch": 0.96,
        "topicMatch": 0.94,
        "authorityLevel": "canonical",
        "lifecycleStatus": "active",
        "freshnessStatus": "current",
        "attestationStatus": "current",
        "accessCompatible": true,
        "classificationCompatible": true,
        "citationSupport": "available",
        "strictGroundingSupport": true,
        "ownerPresent": true,
        "latencyEstimateMs": 650
      },
      "governanceFlags": [],
      "eligible": true,
      "ineligibilityReasons": []
    }
  ],
  "pageInfo": {
    "nextCursor": "eyJvYm8iOiJ1c2VyOmVtcGxveWVlIiwicCI6NX0",
    "hasMore": false,
    "pageLimit": 5
  },
  "resolvedAt": "2026-07-07T12:00:00Z",
  "error": null
}
```

**Response fields:**

| Field | Required | Description |
|---|---:|---|
| `results` | Yes | Resolved KB entries, ranked by eligibility and signals. |
| `results[].kbCard` | Yes | Partial or full KB Card. MUST include `id`, `url`, `transport`, `operations`. |
| `results[].catalogAssertions` | Yes | The Catalog's asserted authority — never the KB's self-declared value. |
| `results[].signals` | Yes | Raw ranking signals (§3.1.1). MUST NOT be collapsed into an opaque score. |
| `results[].governanceFlags` | Yes | Computed flags (§6.11). Empty array if none. |
| `results[].eligible` | Yes | Whether this KB passes the eligibility gates (§4.1) for this request. |
| `results[].ineligibilityReasons` | When `eligible: false` | Reasons, subject to leakage rules (§8.4). |
| `pageInfo` | For paginated results | Cursor pagination metadata (§5). |
| `resolvedAt` | Recommended | Timestamp of resolution; useful for cache TTL decisions. |
| `error` | Yes | Structured error if `ok: false`; otherwise null. |

#### 3.1.1 Signals dictionary (normative)

Every emitted signal MUST carry these semantics; a Catalog MUST NOT emit signals with undefined meaning.

| Signal | Type | Meaning |
|---|---|---|
| `scopeMatch` | number 0–1 | Match between query context (jurisdiction, department, product, temporal) and `coverage`. The computation method is Catalog-internal; the value MUST be monotonic in match quality and comparable across results *within one response*. |
| `topicMatch` | number 0–1 | Semantic match between query text and `domains`/`topics`/`coverage.scope`. Same comparability rule. |
| `authorityLevel` | enum | Catalog-asserted SoR value. |
| `lifecycleStatus` | enum | Current lifecycle status. |
| `freshnessStatus` | `current` \| `stale` \| `unknown` | Against declared cadence and any `maxStalenessHours`. |
| `attestationStatus` | `current` \| `overdue` \| `none` | Attestation state. |
| `accessCompatible` | boolean | Caller satisfies the card's access metadata. |
| `classificationCompatible` | boolean | Card classification within `allowedClassifications`. |
| `citationSupport` | `available` \| `unavailable` | Level 1+ envelope support. |
| `strictGroundingSupport` | boolean | Strict grounding supported. |
| `ownerPresent` | boolean | Ownership resolves in IAM. |
| `latencyEstimateMs` | integer | Advisory latency estimate. |

Cross-response comparability of `scopeMatch`/`topicMatch` (across different resolves, or across Catalogs) is NOT guaranteed; agents MUST NOT persist raw match scores as thresholds across Catalog versions.

### 3.2 `register`

`register` submits a KB Card to the Catalog for schema validation, authority approval, and indexing. It initiates the registration workflow.

**Request:**

```json
{
  "a2kVersion": "1.0-baseline",
  "operation": "register",
  "kbCard": {
    "a2kVersion": "1.0-baseline",
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
      "profile": "a2k-enterprise-v1.0-baseline",
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
  },
  "requestedAuthorityLevel": "canonical",
  "approvalEvidence": {
    "method": "governance-approval",
    "approvedBy": "Finance Policy Council",
    "approvalReference": "GRC-1842",
    "approvedAt": "2026-05-10T00:00:00Z"
  },
  "submittedBy": {
    "id": "user:kb-admin@example.com",
    "team": "Finance Operations"
  }
}
```

Registration SHOULD include: card URL or submitted card content; owner identity resolution; requested authority level and scope; approval evidence for SoR claims; access classification mapping; conformance claim; supported operations; lifecycle and attestation deadlines; and regulated controls if applicable.

**Response:**

```json
{
  "a2kVersion": "1.0-baseline",
  "ok": true,
  "operation": "register",
  "registrationId": "reg-2026-001",
  "status": "pending-approval",
  "assertedAuthorityLevel": null,
  "validationResult": {
    "schemaValid": true,
    "ownershipValid": true,
    "authorityClaimStatus": "under-review",
    "governanceFlags": []
  },
  "nextStep": "Awaiting authority approval from Governance/Data Office.",
  "error": null
}
```

`status` SHOULD be one of: `registered`, `pending-approval`, `approved`, `downgraded`, `rejected`, `quarantined`.

Registration validation MUST run the schema package plus the named cross-field rules (`A2K-VAL-*`, A2K-KBCard-Schema §14): SoR claims without acceptable proof are downgraded to `unverified`; S1+ cards with `oboAssertionRequired: false` are rejected; production cards without group ownership are rejected.

A KB MUST NOT receive production agent traffic until `status` is `registered` or `approved` and the Catalog has indexed the card.

### 3.3 `getCard`

`getCard` retrieves the Catalog's authoritative, annotation-enriched version of a KB Card by ID.

**Request:**

```json
{
  "a2kVersion": "1.0-baseline",
  "operation": "getCard",
  "kbId": "urn:a2k:enterprise:finance-expense-policy",
  "onBehalfOf": {
    "subject": "user:employee@example.com",
    "entitlements": [
      "finance.policy.read"
    ]
  }
}
```

**Response:**

```json
{
  "a2kVersion": "1.0-baseline",
  "ok": true,
  "operation": "getCard",
  "kbCard": {
    "a2kVersion": "1.0-baseline",
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
      "profile": "a2k-enterprise-v1.0-baseline",
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
  },
  "catalogAssertions": {
    "systemOfRecord": "canonical",
    "authorityScope": "Global employee expense reimbursement policy",
    "assertedAt": "2026-06-01T08:00:00Z",
    "assertedBy": "enterprise-catalog"
  },
  "governanceFlags": [],
  "error": null
}
```

A caller MUST NOT receive a card for a KB it is not authorized to discover. Where concealment policy applies (§8.2), the Catalog MUST return `NOT_FOUND` rather than the card or `ACCESS_DENIED`.

## 4. Eligibility and ranking

Source selection has three layers: normative eligibility gates, policy-defined ranking over exposed signals, and an informative default.

### 4.1 Eligibility gates (normative)

The Catalog MUST exclude, or mark `eligible: false`, any KB failing a hard gate:

1. **Access** — the caller does not satisfy access metadata, or (for S1+) the OBO assertion is absent or invalid. Failing this gate at S2 means *exclusion with concealment*, never `eligible: false` with the card visible.
2. **Classification** — the card is outside `allowedClassifications`.
3. **Schema validity** — the `schema-invalid` flag is set.
4. **Lifecycle** — `suspended` always; `archived` unless explicitly requested; `quarantined` always.
5. **Registration** — status is not `registered`/`approved`.
6. **Requested operations** — the card lacks a member of `requiredOperations`.
7. **Staleness** — `lastUpdated` older than `maxStalenessHours` where supplied.
8. **Authority floor** — below `minSystemOfRecord` where supplied.
9. **Risk** — regulated-only KBs are excluded for callers or requests not meeting the KB's declared regulated constraints.

`ineligibilityReasons` are returned only where they do not violate §8.

### 4.2 Ranking (policy, with exposed signals)

Ordering of eligible results is enterprise policy, expressed over the signals dictionary. The Catalog MUST expose raw signals and MUST NOT collapse them into a single opaque trust score; A2K defines comparable signals, not a universal ranking formula. Agents and gateways apply their own selection logic on top.

### 4.3 Default ranking function (informative)

Deployments without a house policy SHOULD start from lexicographic ordering on: **(1)** `scopeMatch` band (high/medium/low at 0.8/0.5 cuts), **(2)** catalog-asserted authority rank, **(3)** governance health (no flags before flagged), **(4)** `freshnessStatus`, **(5)** `topicMatch`. This encodes *scope beats rank* and *governance beats freshness*. It is a starting point to be tuned against the routing harness (§6.12), not a mandate.

## 5. Pagination

Pagination is OPTIONAL and applies to operations returning collections that can exceed a single page: `resolve` here, and `search` in A2K-KCP, which follows these rules identically. Pagination uses opaque forward cursors rather than numeric offsets, because results are authorization-filtered and authority-ranked per identity, which makes offsets unstable and prone to leaking result-set sizes.

Request `pagination` object:

| Field | Required | Type | Description |
|---|---:|---|---|
| `limit` | Optional | integer | Maximum items in the page. The server MAY return fewer and MAY cap the value. |
| `cursor` | Optional | string/null | Opaque continuation token from a prior `pageInfo.nextCursor`. Null or absent requests the first page. |

Response `pageInfo` object:

| Field | Required | Type | Description |
|---|---:|---|---|
| `nextCursor` | When more pages exist | string/null | Opaque token for the next page, or null when there are no further pages. |
| `hasMore` | Recommended | boolean | Whether further pages are available. |
| `pageLimit` | Optional | integer | Effective page size applied by the server. |

Rules:

1. `nextCursor` MUST be opaque. Clients MUST NOT parse or construct it.
2. A cursor MUST be bound to the original `onBehalfOf` context and query. Replaying a cursor under a different identity MUST NOT succeed — it would otherwise be a path to escalate access or enumerate hidden results.
3. The server MUST re-evaluate access on every page; a caller's entitlements may have changed mid-pagination.
4. A response MUST NOT expose a total result count for non-public results, because counts leak the existence and size of restricted source sets. A `totalCount` field, if present, MUST be access-filtered.
5. Cursors SHOULD be short-lived. A server MAY reject an expired or invalid cursor with `PAGINATION_CURSOR_INVALID`.
6. Each page is a distinct request and SHOULD carry its own `requestId` for audit; page boundaries SHOULD be reconstructable from audit metadata.
7. A server that does not support pagination MUST ignore `cursor`, MAY honor `limit`, and SHOULD omit `pageInfo` or return `hasMore: false` with `nextCursor: null`.

## 6. Catalog responsibilities

The Catalog MUST or SHOULD:

1. **Index registered KB Cards** and keep them synchronized with the KB's published card.
2. **Validate schema conformance and cross-field rules** on registration and continuously where feasible.
3. **Resolve owners against IAM** to detect orphaned KBs; exclude orphaned S1+ KBs from discovery.
4. **Assert authority** by confirming or downgrading self-claimed SoR status through the governed approval workflow (§9). Clients MUST trust the Catalog's asserted authority over the KB's self-declared value.
5. **Authorization-filter discovery** — never return cards a caller is not permitted to see.
6. **Compute governance flags** per KB Card (§6.11).
7. **Expose raw ranking signals** per the dictionary; no opaque trust scores.
8. **Detect authority collisions** between KBs claiming overlapping canonical scope.
9. **Enforce lifecycle automatically.** On `nextAttestationDue` passing, execute `actionOnExpiry` without human intervention; on `reviewBy` passing, set `stale-governance`. This is a MUST: metadata earns trust through enforcement, and expiry must have consequences even when nobody is watching.
10. **Record registration and approval workflows** with audit metadata.
11. **Support regulated verification metadata** (signed cards, attestations) where applicable.
12. **Measure and publish routing quality and catalog health:**
    - Maintain a labelled evaluation set of query→expected-KB routings, seeded from real agent traffic (target: at least 100 labelled queries before any agent moves from hard-wired bindings to `resolve` in production, growing with incident postmortems).
    - Report routing precision/recall per release of the matching logic. A Catalog change that regresses routing quality on the harness MUST NOT ship without sign-off.
    - Publish catalog-health metrics at least monthly: percentage of production cards with current attestation, orphan rate, open `sor-collision` count, `conformance-mismatch` count, and card-vs-endpoint access-drift findings.

The Catalog is the mandatory control plane for resolution: authority assertion and authorization-filtered discovery cannot be delegated to individual KBs, which is why a KB cannot self-promote. A **Gateway** is a separate, optional mediation layer (A2K-Overview §8.1) and does not replace the Catalog; a baseline deployment runs a Catalog plus a shared client library before adopting a gateway.

### 6.11 Governance flags

| Flag | Condition | Recommended agent behavior |
|---|---|---|
| `orphaned` | Owner does not resolve to a live active principal. | Do not treat as authoritative; excluded from discovery at S1+. |
| `stale-governance` | `reviewBy` has passed. | Down-rank; warn; refuse in high-risk contexts. |
| `attestation-overdue` | `nextAttestationDue` has passed. | `actionOnExpiry` has been auto-executed; treat per the resulting state. |
| `superseded` | Lifecycle is `superseded`. | Redirect to `supersededBy` or replacement. |
| `deprecated` | Lifecycle is `deprecated`. | Avoid for current answers unless explicitly requested. |
| `suspended` | Lifecycle is `suspended`. | Do not use unless emergency override is authorized. |
| `sor-collision` | Overlapping `canonical` claims for the same scope. | Treat topic as disputed; surface to authorized users only. |
| `freshness-drift` | Observed updates violate declared cadence or staleness policy. | Down-rank; owner notified. |
| `schema-invalid` | Card fails schema-package validation. | Hard eligibility gate; excluded from production discovery. |
| `conformance-mismatch` | Declared conformance level is not satisfied. | Catalog asserts the highest level actually satisfied. |
| `access-policy-missing` | Required access metadata absent. | Non-public KBs excluded until fixed. |
| `access-policy-drift` | Reconciliation finds card metadata inconsistent with endpoint enforcement. | Down-rank; security review; S2 KBs excluded until reconciled. |

## 7. Hierarchical resolution

A2K-KRP supports two-tier hierarchical resolution for enterprises with divisional Catalogs:

```
[Division / Department Catalog]  →  [Group / Enterprise Catalog]
        (local KBs)                     (canonical KBs)
```

Rules:

1. A division Catalog MAY delegate to the group Catalog for topics outside its indexed scope.
2. The group Catalog's authority assertion overrides a division Catalog's assertion for the same KB when they conflict.
3. A KB's self-assessment (if it exposes one) is informational only; the Catalog's assertion is authoritative.
4. Agents SHOULD prefer the highest-authority Catalog they have access to for a given topic.

Cross-enterprise (partner/vendor) catalog federation is out of scope for this baseline (see roadmap); it requires legal and key-management machinery this baseline does not define. External sources enter the graph as `vendor`-authority KBs registered directly in the group Catalog under enterprise agreement.

## 8. Access-leakage rules

The dominant enterprise risk in discovery is disclosure of sensitive information — including the mere existence of a KB.

### 8.1 Discovery results are identity-scoped

The Catalog MUST filter discovery results by the caller's OBO entitlements. Two employees may legitimately receive different results for the same query. This is correct behavior, not a bug.

### 8.2 `NOT_FOUND` versus `ACCESS_DENIED`

For S2 KBs (`restricted`, `highly-restricted`, `regulated`), returning `ACCESS_DENIED` on a named card confirms the KB exists. Where enterprise policy requires concealment, the Catalog MUST return `NOT_FOUND` rather than `ACCESS_DENIED` — "you cannot see it" MUST be indistinguishable from "it does not exist."

### 8.3 What MUST NOT leak

1. The name, description, scope, URL, or owner of a KB the caller is not authorized to discover.
2. The existence of a collision between two KBs when one or both are unauthorized for the caller.
3. Total result counts that reveal the size of a restricted source set.
4. Cursor contents that encode restricted KB identifiers.
5. Governance flags that reveal internal disputes, review failures, or suspended status of KBs the caller cannot see.

### 8.4 Ineligibility reasons

When `eligible: false`, the Catalog MAY return `ineligibilityReasons`, limited to reasons that do not leak information about unauthorized KBs. "Source is stale for your requested freshness constraint" is safe; "source conflicts with a classified KB" is not.

## 9. Authority approval workflow

When a KB claims `canonical`, `scoped-canonical`, or `scoped-guidance` authority:

1. The KB team submits its card via `register` with `requestedAuthorityLevel` and `approvalEvidence`.
2. The Catalog validates schema, ownership resolution, lifecycle metadata, and the cross-field rules.
3. The Governance/Data Office reviews the claim against enterprise policy and existing authority assertions in the same scope.
4. If no collision exists and the evidence is acceptable, the Catalog sets `catalogAssertions.systemOfRecord` to the approved level.
5. If a collision exists, the Catalog surfaces a governance exception to all KB owners with overlapping scope claims and withholds authority assertion until resolved.
6. The Catalog periodically re-validates authority assertions against attestation deadlines and review dates.

The Catalog MUST NOT silently pick a winner in a collision. A collision is a governance event, not a ranking problem.

## 10. Error model

```json
{
  "ok": false,
  "error": {
    "code": "ACCESS_DENIED",
    "message": "Caller is not authorized to discover this knowledge base.",
    "retryable": false
  }
}
```

Standard error codes for A2K-KRP:

```text
INVALID_REQUEST
UNSUPPORTED_VERSION
AUTHENTICATION_REQUIRED
AUTHORIZATION_FAILED
OBO_ASSERTION_REQUIRED
OBO_ASSERTION_INVALID
ACCESS_DENIED
NOT_FOUND
REGISTRATION_REJECTED
REGISTRATION_PENDING
SCHEMA_VALIDATION_FAILED
AUTHORITY_COLLISION_DETECTED
PAGINATION_CURSOR_INVALID
RATE_LIMITED
TIMEOUT
INTERNAL_ERROR
```

Errors MUST follow the access-leakage rules; for S2 KBs, `NOT_FOUND` replaces `ACCESS_DENIED` where concealment applies.

## 11. Security considerations

**Overbroad authority claims.** Teams may overstate SoR level. Proof metadata is mandatory for SoR claims; claims without acceptable proof are downgraded at registration.

**Catalog compromise or misconfiguration.** The Catalog is the highest-value target in the design: it decides what every agent sees. Deployments MUST protect it accordingly — privileged-access management, change control on matching logic, and the §6.12 regression gate — and clients SHOULD preserve raw ranking signals to make misranking detectable.

**Identity-scoped caching.** Resolution responses cached without identity context create privilege-escalation paths. Cache keys MUST include effective OBO context.

**Cursor replay.** Cursors are identity-bound; replay under a different identity MUST be rejected (`PAGINATION_CURSOR_INVALID` or `AUTHORIZATION_FAILED`).

**Catalog availability.** A Catalog outage should degrade gracefully. Agents MAY use cached resolution results within their declared TTL; they SHOULD NOT proceed with unauthenticated or unvalidated KB Cards; S2 KBs SHOULD fail closed.

**Personal and draft KBs.** Sources with `personal` or `draft` authority may be useful but MUST NOT be treated as authoritative for decisions. The Catalog SHOULD keep them discoverable only in low-risk or explicitly scoped contexts, and agents SHOULD NOT rank them as systems of record.

**Unverified OBO claims.** Plain `onBehalfOf` attributes are routing hints at S0 only; disclosure at S1+ MUST rest on the validated assertion.

## 12. Privacy considerations

Resolution queries may themselves reveal sensitive intent. A query like "What is our severance policy for the London office?" may reveal a restructuring plan.

Catalogs MUST or SHOULD:

1. Log that a resolution query occurred without storing query content beyond policy-permitted retention (MUST).
2. Honor the `queryRetention` declared in KB Cards for downstream KBs (MUST).
3. Not forward the full query text to KBs during ranked scoring unless necessary (SHOULD).
4. Apply purpose limitation: resolution metadata is used for KB selection and routing-quality evaluation only (MUST), and the evaluation harness MUST use minimized or synthetic query text where the original is sensitive.
5. Treat resolution logs as access-controlled artifacts in their own right (SHOULD).
