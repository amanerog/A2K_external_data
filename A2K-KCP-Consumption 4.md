# A2K-KCP — Knowledge Consumption Protocol

**Version:** 0.6-draft
**Contract string:** `0.6-baseline`
**Status:** Working draft / baseline for implementation
**Date:** 2026-07-07
**Audience:** KB-owning teams, agent developers, gateway implementors, security and IAM architects, regulated-workflow engineers.

Normative keywords **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, **MAY**, and **OPTIONAL** follow RFC 2119 / RFC 8174 when used in all capitals.

---

## 1. Purpose and scope

A2K-KCP is the **knowledge consumption protocol**. Its role in the A2K stack is analogous to HTTP in the internet stack: given a resolved KB endpoint (from A2K-KRP), an agent queries that KB on behalf of a user and receives a cited, governed, auditable response.

A2K-KCP defines:

- Four read-only operations: `search`, `ask`, `explain`, `getDocument`
- Common request fields, including on-behalf-of (OBO) authorization context
- The cited-response envelope binding answers, passages, claims, citations, grounding, freshness, access decisions, and audit metadata
- The claim model and claim statuses
- The citation model, including W3C Web Annotation span selectors and source hashes
- Grounding metadata and strict grounding mode
- Conflict reports for cross-KB disagreement, and referrals for out-of-scope redirects
- Regulated controls: OBO assertions, ethical-wall safeguards, signed responses and requests, data lineage, immutable audit, streaming proof footer
- Transport bindings: HTTPS, MCP, A2A

A2K-KCP does **not** define: how to discover or select KBs (A2K-KRP); a new IAM or transport protocol; mutation or write operations; autonomous agent orchestration; or truth guarantees — a strictly grounded answer may still be wrong if the underlying source is wrong, stale, incomplete, or misinterpreted.

**Prerequisite.** The agent is assumed to hold a resolved KB Card before speaking A2K-KCP, just as HTTP assumes DNS has resolved the hostname.

## 2. Transport bindings

A2K-KCP is transport-neutral: the same operations and envelope apply identically over MCP, A2A, and HTTPS.

**HTTPS + JSON:**

```text
POST https://{kb-host}/a2k/{operation}
Content-Type: application/json
Authorization: Bearer {token}
```

Card at `GET https://{kb-host}/.well-known/a2k-card.json` (stub rules in A2K-KBCard-Schema §3.1).

**MCP:**

```text
Resource:  a2k://card
Tools:     a2k.search, a2k.ask, a2k.explain, a2k.getDocument
Optional:  a2k.validateCitation, a2k.reportConflict, a2k.streamAsk, a2k.getAuditRecord
```

**A2A:** a KB exposed as an A2A agent SHOULD declare:

```json
{
  "capabilities": {
    "extensions": [
      {
        "uri": "urn:a2k:enterprise:profile:1.0",
        "description": "A2K enterprise KB governance and cited-response profile",
        "required": false
      }
    ]
  }
}
```

## 3. Common request fields

All A2K-KCP operations share this request structure:

```json
{
  "a2kVersion": "0.6-baseline",
  "operation": "ask",
  "query": "What is our standard indemnification clause?",
  "context": {
    "userLocale": "en-US",
    "jurisdiction": "EMEA",
    "dateContext": "2026-07-07",
    "purpose": "answer_user_question",
    "riskLevel": "medium"
  },
  "onBehalfOf": {
    "subject": "user:jdoe@example.com",
    "subjectIdP": "okta",
    "department": "Sales",
    "roles": [
      "account-executive"
    ],
    "entitlements": [
      "legal.templates.read"
    ],
    "clearanceLevel": "confidential",
    "purpose": "answer_user_question",
    "oboAssertionToken": "<signed-entitlement-assertion>"
  },
  "agent": {
    "agentId": "enterprise-assistant",
    "agentInstanceId": "agent-run-789",
    "serviceIdentity": "svc:enterprise-assistant"
  },
  "requirements": {
    "citationsRequired": true,
    "strictGrounding": false,
    "maxStalenessHours": 720,
    "answerLanguage": "en",
    "allowedClassifications": [
      "public",
      "internal",
      "confidential"
    ],
    "requireApprovedSources": true,
    "reportConflicts": true,
    "regulatedMode": false
  },
  "requestMetadata": {
    "requestId": "req-001",
    "sessionId": "session-456",
    "traceId": "trace-abc"
  }
}
```

### 3.1 OBO authorization

For non-public enterprise KBs, authorization MUST be evaluated against the effective permissions of the user, service, or process on whose behalf the agent is acting.

1. The KB MUST authorize against `onBehalfOf.subject`, never merely the agent's `serviceIdentity`.
2. An agent MUST NOT become a privilege-escalation path.
3. A KB MUST NOT return material that the subject could not retrieve directly, unless an explicit, audited privileged-service policy allows it.
4. Deployments SHOULD use standard token-exchange/OBO mechanisms (e.g. OAuth 2.0 Token Exchange, RFC 8693) or IdP equivalents. A2K mandates the *property* that verified subject identity and entitlements reach the KB; it does not mandate a new identity protocol.
5. Audit logs MUST record both the represented subject and the agent/service identity.
6. **Tier rule.** At security tier **S1+** (A2K-KBCard-Schema §4.4.1), the request MUST carry a valid `oboAssertionToken` and the KB MUST validate it before returning any content. The plain attribute claims (`department`, `roles`, `entitlements`) are advisory context and MUST NOT be the basis of an access decision at S1+ — an unverified attribute-carrying assertion is the confused-deputy pattern of §19.9. At S0, plain claims plus the transport bearer token are acceptable.
7. Ethical-wall-sensitive KBs MUST validate the assertion regardless of tier and apply §14.2.

## 4. Operations

A2K-KCP defines four operations. All are read-only: they MUST NOT mutate enterprise state and MUST NOT execute arbitrary tools.

| Operation | Required by level | Purpose |
|---|---:|---|
| `search` | Level 1+ | Retrieve relevant passages or documents with citations. |
| `ask` | Level 2+ | Return a synthesized answer grounded in cited sources. |
| `explain` | Level 2+ recommended | Explain a prior answer, claim, or citation. |
| `getDocument` | Level 1+ where permitted | Retrieve a source document or fragment by ID. |

### 4.1 `search`

`search` retrieves relevant passages or documents matching a query. It MUST use the cited-response envelope, with `passages` populated and `answer: null`. It does not synthesize an answer.

**Request:**

```json
{
  "a2kVersion": "0.6-baseline",
  "operation": "search",
  "query": "business class flight expense policy",
  "filters": {
    "language": "en",
    "dateAsOf": "2026-07-07",
    "classificationAllowed": [
      "internal"
    ],
    "lifecycleStatusAllowed": [
      "active"
    ]
  },
  "pagination": {
    "limit": 10,
    "cursor": null
  },
  "requirements": {
    "citationsRequired": true,
    "maxStalenessHours": 2160
  }
}
```

**Response excerpt:**

```json
{
  "a2kVersion": "0.6-baseline",
  "ok": true,
  "operation": "search",
  "sourceKbId": "urn:a2k:enterprise:finance-expense-policy",
  "answer": null,
  "passages": [
    {
      "id": "passage-1",
      "text": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
      "documentId": "doc:expense-policy:air-travel:v3",
      "score": 0.91,
      "citationIds": [
        "citation-1"
      ]
    }
  ],
  "claims": [],
  "citations": [
    {
      "id": "citation-1",
      "claimIds": [],
      "documentId": "doc:expense-policy:air-travel:v3",
      "title": "Employee Expense Policy — Air Travel",
      "selector": {
        "type": "TextQuoteSelector",
        "exact": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
        "prefix": "Air travel policy. ",
        "suffix": " Exceptions must be approved by Finance."
      },
      "sourceUrl": "https://kb.example.com/finance/expense-policy#air-travel",
      "sourceHash": "sha256:e3b0c442...",
      "retrievedAt": "2026-07-07T09:12:00Z",
      "sourceLastUpdated": "2026-05-10T00:00:00Z",
      "classification": "internal",
      "dataLineage": []
    }
  ],
  "freshness": {
    "stale": false,
    "validAsOf": "2026-07-07"
  },
  "accessDecision": {
    "decision": "allowed"
  },
  "audit": {
    "requestId": "req-001",
    "logged": true
  },
  "pageInfo": {
    "nextCursor": "eyJvYm8iOiJ1c2VyOmpkb2UiLCJwIjoxMH0",
    "hasMore": true,
    "pageLimit": 10
  },
  "error": null
}
```

A **passage** carries: `id`; `text`; `documentId`; an optional relevance `score`; and `citationIds` linking it to citation objects.

### 4.2 `ask`

`ask` returns a synthesized answer grounded in cited sources: the full cited-response envelope with `answer`, `claims`, `citations`, and `grounding` populated.

**Request:**

```json
{
  "a2kVersion": "0.6-baseline",
  "operation": "ask",
  "query": "Can employees expense business-class flights?",
  "context": {
    "jurisdiction": "US",
    "dateContext": "2026-07-07",
    "purpose": "answer_user_question",
    "riskLevel": "medium"
  },
  "onBehalfOf": {
    "subject": "user:employee@example.com",
    "department": "Sales",
    "entitlements": [
      "finance.policy.read"
    ]
  },
  "requirements": {
    "citationsRequired": true,
    "strictGrounding": false,
    "requireApprovedSources": true,
    "maxStalenessHours": 2160,
    "reportConflicts": true
  }
}
```

**Response:** full cited-response envelope (§5); complete worked example in §20.

### 4.3 `explain`

`explain` explains a prior answer, claim, or citation set. A KB MAY implement it statelessly (requiring the prior answer and citations in the request body) or statefully (accepting an `answerRef`).

**Request:**

```json
{
  "a2kVersion": "0.6-baseline",
  "operation": "explain",
  "answerRef": "resp-123",
  "claimIds": [
    "claim-1"
  ],
  "priorCitations": [],
  "requirements": {
    "citationsRequired": true
  }
}
```

**Response:** cited-response envelope with `answer` carrying the explanation and `claims` scoped to the explained claim(s).

### 4.4 `getDocument`

`getDocument` retrieves a source document or addressable fragment by ID. The **document** returned — and the documents referenced by citations — is the concrete realization of the *Document* defined in A2K-Overview §15: any addressable unit of enterprise knowledge (a text document, web page, wiki page, table or row set, JSON document, PDF, code file, ticket, or fragment of any of these), identified by `documentId` and described by `mimeType`.

Unlike `search`, `ask`, and `explain`, `getDocument` does not use the cited-response envelope, because the document itself is the evidence.

**Request:**

```json
{
  "a2kVersion": "0.6-baseline",
  "operation": "getDocument",
  "documentId": "doc:expense-policy:air-travel:v3",
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
  "a2kVersion": "0.6-baseline",
  "ok": true,
  "operation": "getDocument",
  "sourceKbId": "urn:a2k:enterprise:finance-expense-policy",
  "document": {
    "documentId": "doc:expense-policy:air-travel:v3",
    "title": "Employee Expense Policy — Air Travel",
    "sourceUrl": "https://kb.example.com/finance/expense-policy#air-travel",
    "mimeType": "text/html",
    "content": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
    "hash": "sha256:9f2b...",
    "lastUpdated": "2026-05-10T00:00:00Z",
    "classification": "internal"
  },
  "accessDecision": {
    "decision": "allowed",
    "reason": "User satisfies finance.policy.read scope."
  },
  "error": null
}
```

A KB MAY omit `content` if the caller is not authorized to retrieve the document body. It MUST NOT leak restricted titles, URLs, document IDs, or existence through denial responses. Large bodies are handled with addressable fragments, not pagination.

## 5. Cited-response envelope

The cited-response envelope is the core A2K-KCP response shape for `search`, `ask`, and `explain`.

```json
{
  "a2kVersion": "0.6-baseline",
  "ok": true,
  "operation": "ask",
  "sourceKbId": "urn:a2k:enterprise:example-kb",
  "answer": "Employees may expense business-class flights only when they have VP approval or when the flight duration exceeds 8 hours.",
  "passages": [],
  "claims": [
    {
      "id": "claim-1",
      "text": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
      "type": "policy",
      "status": "SUPPORTED",
      "citationIds": [
        "citation-1"
      ],
      "conflictsWith": []
    }
  ],
  "citations": [
    {
      "id": "citation-1",
      "claimIds": [
        "claim-1"
      ],
      "documentId": "doc:expense-policy:air-travel:v3",
      "title": "Employee Expense Policy — Air Travel",
      "selector": {
        "type": "TextQuoteSelector",
        "exact": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
        "prefix": "Air travel policy. ",
        "suffix": " Exceptions must be approved by Finance."
      },
      "sourceUrl": "https://kb.example.com/finance/expense-policy#air-travel",
      "sourceHash": "sha256:e3b0c442...",
      "retrievedAt": "2026-07-07T09:12:00Z",
      "sourceLastUpdated": "2026-05-10T00:00:00Z",
      "classification": "internal"
    }
  ],
  "grounding": {
    "groundedRatio": 1.0,
    "ungroundedSpans": [],
    "confidence": 0.88,
    "confidenceMethod": "nli-entailment",
    "strictGroundingSatisfied": true
  },
  "freshness": {
    "sourceLastUpdated": "2026-05-10T00:00:00Z",
    "reviewedAt": "2026-05-10T00:00:00Z",
    "nextReviewDue": "2026-08-10T00:00:00Z",
    "retrievedAt": "2026-07-07T09:12:00Z",
    "stale": false,
    "validAsOf": "2026-07-07"
  },
  "accessDecision": {
    "decision": "allowed",
    "reason": "User satisfies finance.policy.read scope.",
    "appliedScopes": [
      "finance.policy.read"
    ],
    "dataClassification": "internal",
    "policyEngine": "enterprise-iam",
    "decisionId": "authz-789",
    "oboValidated": true
  },
  "audit": {
    "requestId": "req-001",
    "sessionId": "session-456",
    "agentId": "enterprise-assistant",
    "userId": "user-123",
    "sourceKbVersion": "2026-05-10.3",
    "kbCardVersion": "1.1.0",
    "policyDecision": "allowed",
    "logged": true,
    "loggedAt": "2026-07-07T09:12:01Z"
  },
  "conflicts": [],
  "referrals": [],
  "usage": {
    "latencyMs": 641,
    "retrievalCount": 4
  },
  "responseSignature": null,
  "error": null,
  "pageInfo": null
}
```

| Field | Required | Description |
|---|---:|---|
| `a2kVersion` | Yes | Contract version. |
| `ok` | Yes | Success boolean. |
| `operation` | Yes | Operation name. |
| `sourceKbId` | Yes | KB identifier from the card. |
| `answer` | For `ask`/`explain` | Synthesized answer. Null for pure `search`. |
| `passages` | For `search` | Retrieved passages with scores and citation IDs. |
| `claims` | Level 2+ for `ask`/`explain` | Machine-readable assertions (§6). |
| `citations` | Level 1+ | Evidence references (§7). |
| `grounding` | Level 2+ | Grounding coverage and unsupported spans (§8). |
| `freshness` | Recommended | Source freshness and validity metadata (§9). |
| `accessDecision` | Recommended; required for non-public KBs | Access decision applied to the request (§10). |
| `audit` | Recommended; required at S1+ and in regulated mode | Audit metadata (§11). |
| `conflicts` | Optional (§12.1) | Self-reported aware-conflicts. |
| `referrals` | Optional | Better-suited KBs for out-of-scope queries (§13). |
| `usage` | Optional | Cost, token, latency, or retrieval metrics. |
| `pageInfo` | For paginated `search` | Cursor pagination (§5.1). Null or absent for `ask`/`explain`. |
| `responseSignature` | S2 SHOULD / Level 4 | Detached signature over response metadata and content (§14.3). |
| `error` | Yes | Structured error if `ok: false`; otherwise null. |

### 5.1 Pagination

Pagination applies only to `search`, which returns collections. `ask` and `explain` return a single synthesized answer and MUST NOT paginate it; `getDocument` uses addressable fragments. Field shapes and rules follow A2K-KRP §5 identically: opaque cursors, identity-bound, access re-evaluated per page, no total counts for non-public results, short-lived cursors, per-page `requestId`.

## 6. Claims

### 6.1 Claim object

```json
{
  "id": "claim-1",
  "text": "ACME's standard indemnification clause caps liability at 12 months' fees.",
  "type": "policy",
  "status": "SUPPORTED",
  "citationIds": [
    "citation-1"
  ],
  "conflictsWith": []
}
```

### 6.2 Claim types (informative)

`type` MAY be one of: `factual`, `definitional`, `procedural`, `interpretive`, `policy`, `permission`, `quantitative`, `temporal`, `negative`, `unknown`. The taxonomy is informative — claim typing is an active research area — and clients MUST NOT gate behavior on `type`.

### 6.3 Claim statuses (normative)

| Status | Meaning |
|---|---|
| `SUPPORTED` | Cited evidence supports the claim for this caller. |
| `REFUTED` | Cited evidence contradicts the claim. |
| `DISPUTED` | Multiple sources disagree materially. MUST reference a conflict where available. |
| `INSUFFICIENT_EVIDENCE` | No relevant evidence, or not enough, is available to this caller. Also the correct status where existence is intentionally concealed by policy. |
| `OUT_OF_SCOPE` | The query is outside the KB's declared scope; see referrals. |
| `ACCESS_DENIED` | Material may exist but this caller may not access it. Emitted only where concealment policy does not apply; otherwise `INSUFFICIENT_EVIDENCE`. Compliance-specific denials carry the nuance in `accessDecision.decision: "restricted-by-compliance"`. |
| `UNAUDITED_MATERIAL` | Relevant material exists but is automated or unreviewed and not approved for the requested risk level. |
| `AMBIGUOUS_QUERY` | The query cannot be answered safely without clarification. |
| `HUMAN_REVIEW_REQUIRED` | Policy requires human review before the answer may be used. |

Staleness is not a claim status: it is expressed in the `freshness` object (§9) and, where a hard staleness requirement fails, in the `STALE_SOURCE` error.

## 7. Citations

A citation is a machine-readable reference to enterprise evidence supporting or refuting a passage or claim.

```json
{
  "id": "citation-1",
  "claimIds": [
    "claim-1"
  ],
  "documentId": "doc:legal:templates:indemnification:v7",
  "title": "Standard Indemnification Clause v7",
  "selector": {
    "type": "TextQuoteSelector",
    "exact": "liability is capped at twelve months of fees",
    "prefix": "Except for excluded claims, ",
    "suffix": " unless otherwise approved by Legal."
  },
  "sourceHash": "sha256:9f2b...",
  "sourceUrl": "https://kb.internal.example.com/legal/indemnification-v7",
  "retrievedAt": "2026-07-07T09:12:00Z",
  "sourceLastUpdated": "2026-06-01T08:00:00Z",
  "classification": "confidential",
  "dataLineage": []
}
```

### 7.1 Citation fields

| Field | Required | Description |
|---|---:|---|
| `id` | Yes | Citation identifier. |
| `claimIds` | Recommended | Claims supported or refuted by the citation. |
| `documentId` | Yes, unless redacted | Stable document or fragment identifier. Subject to leakage rules. |
| `title` | Recommended | Human-readable title. Subject to leakage rules. |
| `selector` | Recommended at Level 1; required at Level 4 where possible | Addressable fragment selector (§7.2). |
| `sourceHash` | Level 4 where possible | Hash of the cited fragment or canonical document (§7.3). |
| `sourceUrl` | Recommended where permitted | Resolvable URL or URI. Subject to leakage rules. |
| `retrievedAt` | Yes | Retrieval timestamp. |
| `sourceLastUpdated` | Recommended | Source freshness timestamp. |
| `classification` | Recommended | Data classification of the cited material. |
| `dataLineage` | Level 4 where applicable | Processing lineage from ingestion to retrieval representation (§7.4). |

All of `title`, `sourceUrl`, `documentId`, and even `selector.exact` are subject to access-leakage rules. A citation MUST NOT expose information a caller is not authorized to see.

### 7.2 Span selectors

Citation spans use **W3C Web Annotation Data Model selectors** by reference:

| Selector | Use |
|---|---|
| `TextQuoteSelector` | Preferred for mutable documents; identifies text by exact quoted content and SHOULD include `prefix`/`suffix` context for disambiguation; resilient to formatting and offset drift. |
| `TextPositionSelector` | Character offsets (`start`/`end`) for stable text. |
| `FragmentSelector` | Page, section, or media fragments per the relevant fragment specification. |
| `RefinedBy` composition | E.g., a `FragmentSelector` for a page refined by a `TextQuoteSelector` within it, for page-plus-quote citations. |

Tabular material is expressed as a `FragmentSelector` with the A2K-registered fragment scheme `a2k-table:{tableId};r{row};c{col}`.

Known gap: PDFs without a text layer, images, audio/video transcripts, knowledge-graph triples, and spreadsheet formulas need richer selectors. KBs holding such content SHOULD document their own selector convention and target Level 1–2 until a future baseline standardizes these.

### 7.3 Source hashes

`sourceHash` SHOULD use `sha256:{digest}` and, where possible, cover the cited fragment. Pair the hash with `retrievedAt`, `documentId`, and document version: for living sources, a later verification may see a mismatch from normal drift, so treat *mismatch plus version change* as drift and *mismatch at the same version* as a stronger tamper signal.

### 7.4 Data lineage (Level 4)

Level 4 regulated citations SHOULD include `dataLineage` where the KB uses ingestion pipelines, chunking, embeddings, vector stores, transformations, or derived indexes:

```json
{
  "dataLineage": [
    {
      "step": "ingestion",
      "sourcePipeline": "federal-register-sync-v2",
      "ingestedAt": "2026-01-12T04:15:00Z"
    },
    {
      "step": "chunking",
      "chunkerVersion": "policy-chunker-v4",
      "processedAt": "2026-01-12T04:15:45Z"
    },
    {
      "step": "vectorization",
      "embeddingModel": "text-embedding-3-large",
      "embeddingModelVersion": "2026-01-01",
      "processedAt": "2026-01-12T04:16:12Z"
    }
  ]
}
```

Data lineage serves audit and model-risk workflows. It is not required for every KB.

## 8. Grounding

```json
{
  "grounding": {
    "groundedRatio": 1.0,
    "ungroundedSpans": [],
    "confidence": 0.9,
    "confidenceMethod": "nli-entailment",
    "strictGroundingSatisfied": true
  }
}
```

**Semantics.** `groundedRatio` is the character-weighted fraction of the `answer` covered by spans attributable to at least one citation; 

`ungroundedSpans` lists the uncovered spans as `TextPositionSelector`s over the answer text. 

Grounding measures citation coverage of what was said;

 it does not prove that relevant evidence wasn't omitted, and it says nothing about truth.

`confidence` is OPTIONAL and MUST be treated as a hint, not a gate. If present it MUST include `confidenceMethod`: `calibrated`, `nli-entailment`, `retrieval-similarity`, `heuristic`, `llm-self-report`, `unknown`. 

Clients SHOULD discount `llm-self-report` and `unknown` heavily, and **`llm-self-report` MUST NOT be the sole basis for any gating decision** (strict grounding, refusal, escalation) by KB, gateway, or client.

### 8.1 Strict grounding mode

`requirements.strictGrounding` is OPTIONAL in general, RECOMMENDED for high-risk workflows, and REQUIRED when enterprise policy designates the operation as regulated or when deterministic compliance logic depends on the answer.

If `strictGrounding` is true, the KB MUST NOT return an answer containing material unsupported assertions. It MUST do one of:

1. Return only cited, extractive, or fully supported content.
2. Return a structured refusal.
3. Return `HUMAN_REVIEW_REQUIRED`.
4. Fail with `GROUNDING_VIOLATION`.

Strict grounding MUST NOT depend solely on a self-reported scalar confidence score. Strict grounding does not prove truth: a strictly grounded answer may still be wrong if the underlying source is wrong, stale, incomplete, or misinterpreted.

## 9. Freshness

```json
{
  "freshness": {
    "sourceLastUpdated": "2026-05-10T00:00:00Z",
    "reviewedAt": "2026-05-10T00:00:00Z",
    "nextReviewDue": "2026-08-10T00:00:00Z",
    "retrievedAt": "2026-07-07T09:12:00Z",
    "stale": false,
    "validAsOf": "2026-07-07",
    "validUntil": null
  }
}
```

If the request set `maxStalenessHours`, the KB MUST set `stale: true` when source material exceeds it — the client asked a yes/no question and the KB must answer it. High-risk clients SHOULD refuse stale sources unless policy allows warning-only use.

## 10. Access decision

A response SHOULD include the access decision applied to the request; non-public KBs MUST include it unless doing so would itself leak sensitive information.

```json
{
  "accessDecision": {
    "decision": "allowed",
    "reason": "User satisfies required scope.",
    "appliedScopes": [
      "finance.policy.read"
    ],
    "dataClassification": "internal",
    "policyEngine": "enterprise-iam",
    "decisionId": "authz-789",
    "oboValidated": true
  }
}
```

`decision` SHOULD be one of: `allowed`, `denied`, `redacted`, `partial`, `restricted-by-compliance`, `human-review-required`, `unknown`. At S1+, `oboValidated` MUST be `true` for any non-denied decision.

If access is denied, the response MUST NOT leak restricted content in `answer`, `passages`, `claims`, `citations`, `referrals`, errors, or caller-visible audit fields.

## 11. Audit metadata

Enterprise deployments SHOULD include audit metadata in responses; it is REQUIRED at tier S1+ and in regulated mode, where it MUST be sufficient for post-hoc reconstruction.

```json
{
  "audit": {
    "requestId": "req-001",
    "sessionId": "session-456",
    "traceId": "trace-abc",
    "agentId": "enterprise-assistant",
    "agentInstanceId": "agent-run-789",
    "userId": "user-123",
    "sourceKbId": "urn:a2k:enterprise:finance-expense-policy",
    "sourceKbVersion": "2026-05-10.3",
    "kbCardVersion": "1.1.0",
    "operation": "ask",
    "policyDecision": "allowed",
    "decisionReason": "User has finance.policy.read",
    "citationIds": [
      "citation-1"
    ],
    "logged": true,
    "loggedAt": "2026-07-07T09:12:01Z"
  }
}
```

Audit metadata MUST allow reconstruction of: which agent made the request; which user, service, or process identity was represented; which KB was queried; which KB version and card version were used; which policy decision was applied; which citations supported the response; when the response was generated; and whether strict grounding, regulated mode, or proof verification was requested.

**Immutable audit targets.** For regulated deployments, audit declarations MAY require write-once logging (`immutableLogRequired: true`, `logTarget: "enterprise-worm-archive"`, compliance-set retention). A2K does not mandate a storage system: WORM storage, immutable object storage, append-only ledgers, SIEM, GRC, or audit platforms all qualify. Immutable logs improve accountability but preserve sensitive queries and user intent; enterprises SHOULD apply access controls, retention policies, minimization, and redaction. In banking deployments these records are candidate books-and-records artifacts (A2K-Overview §11).

## 12. Conflict handling

A2K distinguishes two kinds of conflict.

### 12.1 Aware-conflict (optional, informative)

A KB that happens to know its answer disagrees with another named source MAY self-report a conflict. This is not a duty: most KBs cannot maintain awareness of other KBs' content, and an obligation nobody can discharge breeds checkbox conformance.

```json
{
  "conflicts": [
    {
      "id": "conflict-1",
      "claimId": "claim-1",
      "nature": "scope-conflict",
      "thisPosition": "Business-class flights require VP approval unless flight duration exceeds 8 hours.",
      "otherPosition": "Business-class flights are never reimbursable for Sales without CFO approval.",
      "otherSource": {
        "kbId": "urn:a2k:enterprise:sales-travel-guide",
        "title": "Sales Travel Guide"
      },
      "assessment": "context-dependent",
      "rationale": "Finance is canonical for reimbursement; Sales guide may impose stricter departmental controls."
    }
  ]
}
```

Aware-conflicts are subjective. Where present: a KB MUST NOT assert `this-source-authoritative` solely because it is the responder; clients MUST weight aware-conflicts by the reporting KB's catalog-asserted authority and SHOULD corroborate against the named source rather than trusting the characterization.

### 12.2 Cross-KB conflict report (the primary mechanism)

When an agent or gateway fans out to multiple KBs and detects material disagreement, it SHOULD produce a conflict report:

```json
{
  "a2kVersion": "0.6-baseline",
  "kind": "conflictReport",
  "query": "Can employees expense business-class flights?",
  "producedBy": {
    "role": "knowledge-agent",
    "id": "urn:a2k:agent:enterprise-assistant"
  },
  "producedAt": "2026-07-07T09:12:05Z",
  "onBehalfOf": {
    "subject": "user:employee@example.com"
  },
  "kbsQueried": [
    "urn:a2k:enterprise:finance-expense-policy",
    "urn:a2k:enterprise:sales-travel-guide"
  ],
  "responses": [
    {
      "sourceKbId": "urn:a2k:enterprise:finance-expense-policy",
      "responseRef": "resp-123"
    },
    {
      "sourceKbId": "urn:a2k:enterprise:sales-travel-guide",
      "responseRef": "resp-456"
    }
  ],
  "conflicts": [
    {
      "id": "conflict-1",
      "type": "scope-conflict",
      "severity": "medium",
      "claims": [
        "resp-123:claim-1",
        "resp-456:claim-2"
      ],
      "summary": "Finance and Sales sources impose different approval thresholds."
    }
  ],
  "reconciliation": {
    "status": "context-dependent",
    "recommendedAction": "Use Finance as reimbursement SoR and Sales as stricter departmental overlay.",
    "basis": [
      "scope-match",
      "system-of-record",
      "department-specific-policy"
    ]
  },
  "escalationTelemetry": null,
  "audit": {
    "logged": true,
    "logRef": "audit-987"
  }
}
```

The report MUST preserve the per-KB responses or response references so the result remains re-checkable.

**Detection quality.** Claim alignment across heterogeneous sources is imperfect. Producers SHOULD tune for precision over recall — a false "conflict" erodes trust faster than a missed one — and SHOULD record the detection method in the report.

### 12.3 Conflict types

`type` SHOULD be one of: `value-conflict`, `scope-conflict`, `temporal-conflict`, `interpretation-conflict`, `methodology-conflict`, `authority-collision`, `freshness-conflict`, `access-conditioned-conflict`, `unknown`.

### 12.4 Reconciliation order (informative) and the surfacing rule (normative)

Recommended reconciliation inputs, in order: (1) scope, jurisdiction, product, region, department, and temporal match; (2) system-of-record level from Catalog assertions; (3) governance health (flags); (4) freshness; (5) source generation and review status; (6) corroboration across independent KBs; (7) regulated controls and verifiability where required.

The normative rule is singular: **when a material conflict remains unresolved, agents MUST surface it rather than silently choosing** — silent resolution by model behavior defeats the design.

All conflict surfacing is bound by the access-leakage rules of §18. Detecting a conflict is never permission to disclose a restricted KB, document, title, or claim.

### 12.5 Escalation telemetry (S2 / regulated)

For regulated workflows, unresolved high-risk conflicts SHOULD include escalation telemetry, transforming semantic disagreement into a governance workflow:

```json
{
  "escalationTelemetry": {
    "required": true,
    "route": "grc-case-management",
    "caseType": "policy-conflict",
    "ownerTeams": [
      "Finance Operations",
      "Sales Operations"
    ],
    "severity": "high",
    "sla": "5-business-days",
    "createdTicketRef": "GRC-99231"
  }
}
```

## 13. Referrals

A KB that returns `OUT_OF_SCOPE` MAY include referrals to better-suited KBs:

```json
{
  "referrals": [
    {
      "kbId": "urn:a2k:enterprise:travel-policy",
      "reason": "Travel booking policy is owned by Global Travel, not Finance Operations.",
      "authorityLevel": "scoped-guidance",
      "cardUrl": "https://catalog.example.com/kbs/travel-policy"
    }
  ]
}
```

Referrals are advisory. A client MUST independently verify referred KB metadata via A2K-KRP before use. Referrals MUST NOT leak KB names, scopes, URLs, or existence to unauthorized callers.

## 14. Regulated controls

### 14.1 OBO assertion token

The signed OBO assertion (mandatory and validated at tier S1+, §3.1) SHOULD bind: subject identity; agent identity; service identity; delegated scopes or entitlements; purpose; timestamp; expiration; request or session identifier; issuer; and audience. Implementations SHOULD realize it via OAuth 2.0 Token Exchange (RFC 8693) or an equivalent enterprise IAM mechanism. Ethical-wall sensitive KBs MUST validate it regardless of tier.

### 14.2 Ethical-wall safeguards

Validating OBO assertions at the KB prevents an agent or gateway with broad network access from bypassing user-specific ethical walls, clearance boundaries, or departmental restrictions. For ethical-wall sensitive KBs:

1. `enterprise.access.ethicalWallSensitive` MUST be `true` in the card (forcing tier S2).
2. `auth.oboAssertionRequired` MUST be `true`.
3. Requests without verifiable OBO context MUST be rejected or redacted.
4. Access denials MUST NOT leak restricted matter names, deal names, client names, securities, documents, or teams.

### 14.3 Signed responses

```json
{
  "responseSignature": {
    "alg": "EdDSA",
    "kid": "https://kb.internal.example.com/jwks.json#response-key-4",
    "canonicalization": "JCS-RFC8785",
    "signedFields": [
      "a2kVersion",
      "operation",
      "sourceKbId",
      "answer",
      "claims",
      "citations",
      "grounding",
      "freshness",
      "accessDecision",
      "audit"
    ],
    "jws": "<detached-signature>"
  }
}
```

Verification test vectors ship in the release-gate package (A2K-Overview §13). A2K does not require public cryptographic infrastructure where equivalent enterprise trust infrastructure exists.

### 14.4 Signed requests

Level 4 KBs MAY require signed requests. KBs advertising `signedRequestRequired` MUST verify nonce, timestamp, audience, and detached signature; reject requests outside an allowed clock-skew window; and reject nonce replay (`REPLAY_DETECTED`).

## 15. Verifiability and response integrity

Level 4 KBs support verifiability features that let a client *confirm* grounding rather than merely trust it. This is not required for baseline wiki onboarding.

Verifiable inputs a response can expose: a resolvable `documentId` or `sourceUrl`; a fragment `selector`; a `sourceHash`; `retrievedAt`; source version; a `responseSignature`; a card signature or catalog attestation; and an audit-log reference.

In proportion to risk, a client SHOULD: confirm each `documentId` or `sourceUrl` resolves; confirm the cited span actually supports the claim; recompute and match `sourceHash` where feasible; verify `responseSignature`; verify the card signature or catalog attestation; and verify the audit-log write.

Verifiability confirms grounding and integrity; it does not prove that a claim is true.

## 16. Streaming and deferred verification

A2K-KCP supports streaming answers while preserving verification through a trailing proof footer.

```text
event: text_chunk
data: {
data:   "text": "According to corporate policy, all credit derivative swaps "
data: }

event: text_chunk
data: {
data:   "text": "must clear through the Central Risk Desk before market open."
data: }

event: proof_footer
data: {
data:   "a2kVersion": "0.6-baseline",
data:   "ok": true,
data:   "sourceKbId": "urn:a2k:finance-corp:risk-desk-kb",
data:   "claims": [
data:     {
data:       "id": "claim-1",
data:       "text": "All credit derivative swaps must clear through the Central Risk Desk before market open.",
data:       "type": "procedural",
data:       "status": "SUPPORTED",
data:       "citationIds": [
data:         "citation-1"
data:       ]
data:     }
data:   ],
data:   "citations": [
data:     {
data:       "id": "citation-1",
data:       "claimIds": [
data:         "claim-1"
data:       ],
data:       "documentId": "doc:risk:ops-manual:v4",
data:       "selector": {
data:         "type": "TextQuoteSelector",
data:         "exact": "credit derivative swaps must clear through the Central Risk Desk"
data:       },
data:       "sourceHash": "sha256:e3b0c442..."
data:     }
data:   ],
data:   "grounding": {
data:     "groundedRatio": 1.0,
data:     "ungroundedSpans": []
data:   },
data:   "accessDecision": {
data:     "decision": "allowed"
data:   },
data:   "audit": {
data:     "requestId": "req-001",
data:     "logged": true
data:   },
data:   "responseSignature": {
data:     "alg": "EdDSA",
data:     "kid": "https://risk.internal.example.com/jwks.json#key-4",
data:     "jws": "<signature-computed-over-streamed-text-plus-footer>"
data:   }
data: }
```

Streaming rules:

1. A streamed answer is provisional until the `proof_footer` is received and accepted.
2. Clients MUST NOT take irreversible business actions based on streamed text until proof-footer validation succeeds.
3. If `strictGrounding` was requested and the proof footer does not satisfy it, the client MUST treat the streamed answer as invalid for the requested purpose.
4. Clients MAY display provisional streamed text if the UI clearly indicates verification is pending.
5. A gateway MAY redact, retract, or replace streamed text if the proof footer indicates policy, access, or grounding failure.
6. The proof footer is subject to the same access, citation, audit, and signature rules as non-streaming responses.

## 17. Error model

```json
{
  "ok": false,
  "error": {
    "code": "GROUNDING_VIOLATION",
    "message": "Strict grounding was requested but not satisfied.",
    "retryable": false,
    "details": {
      "groundedRatio": 0.82
    }
  }
}
```

Standard error codes:

```text
INVALID_REQUEST
UNSUPPORTED_VERSION
UNSUPPORTED_OPERATION
AUTHENTICATION_REQUIRED
AUTHORIZATION_FAILED
OBO_ASSERTION_REQUIRED
OBO_ASSERTION_INVALID
ACCESS_DENIED
NOT_FOUND
OUT_OF_SCOPE
AMBIGUOUS_QUERY
INSUFFICIENT_EVIDENCE
STALE_SOURCE
GROUNDING_VIOLATION
STRICT_GROUNDING_UNSUPPORTED
CONFLICT_DETECTED
HUMAN_REVIEW_REQUIRED
PAGINATION_CURSOR_INVALID
RATE_LIMITED
TIMEOUT
UPSTREAM_ERROR
SCHEMA_VALIDATION_FAILED
SIGNATURE_REQUIRED
SIGNATURE_INVALID
REPLAY_DETECTED
REGULATED_FEATURE_UNSUPPORTED
INTERNAL_ERROR
```

Errors MUST follow the access-leakage rules. For restricted sources, `NOT_FOUND` replaces `ACCESS_DENIED` where denial would reveal existence.

## 18. Access-leakage rules

1. **Denials must not leak.** A bare `ACCESS_DENIED` on a named document confirms it exists. For `restricted`, `highly-restricted`, or `regulated` content, "you cannot see it" MUST be indistinguishable from "it does not exist" where enterprise policy requires concealment.
2. **No citation laundering.** A KB MUST NOT answer using evidence the subject is unauthorized to see and present the answer as grounded. If the only support is unauthorized material, the claim is unsupported for this caller — `INSUFFICIENT_EVIDENCE`, or `ACCESS_DENIED` / `HUMAN_REVIEW_REQUIRED` where concealment does not apply — never `SUPPORTED`.
3. **No laundering through summaries.** A gateway or agent MUST NOT summarize restricted evidence into an answer for an unauthorized subject. The same authorization boundary applies uniformly to raw passages, citations, summaries, claims, and derived answers.
4. **Citations, referrals, conflict reports, and caller-visible audit fields** inherit the same rules as the primary answer, and the Catalog filters discovery by the caller's authorization.
5. **Caching honors authorization.** Cache keys MUST include effective OBO context; a response authorized for one subject MUST NOT be served to a subject with lesser access.
6. **Query privacy is bidirectional.** Internal queries can be more sensitive than the answers — a query about severance policy for a specific office may reveal a layoff plan. KBs MUST honor their declared `queryRetention`, and KBs at classification `confidential` and above SHOULD default to `none`, `session`, or `limited`. Audit logs SHOULD record that a query happened without storing more sensitive content than policy permits.

## 19. Security considerations

1. **Prompt injection.** Clients MUST NOT treat retrieved KB content as instructions; KB content is data, not agent policy. Implementations SHOULD keep source content distinguishable from system, developer, and user instructions.
2. **Overbroad authority claims.** Mitigated at registration: SoR claims require approval proof (A2K-KRP §9, A2K-KBCard-Schema §4.2).
3. **Stale knowledge.** Clients SHOULD downgrade or refuse sources past their review or attestation date in high-risk contexts.
4. **Catalog misconfiguration.** A compromised or misconfigured Catalog may rank wrong sources; clients and gateways SHOULD preserve raw ranking signals and avoid opaque trust scores.
5. **Gateway risk.** A gateway may suppress conflicts, select preferred sources, or misapply access policy; the normative Gateway duties (A2K-Overview §8.1) — log all KBs queried, preserve envelopes, never suppress conflicts, citations, access decisions, or freshness failures — are the structural mitigation.
6. **Personal and draft KBs.** Useful but never authoritative for decisions; restrict to low-risk or explicitly scoped contexts.
7. **Citation laundering.** A response may cite an approved source while making a claim the cited span does not support. Clients SHOULD inspect cited spans in proportion to risk; the optional `a2k.validateCitation` tool exists for exactly this.
8. **Audit privacy.** Audit logs may contain sensitive intent or confidential queries; apply enterprise retention, minimization, access-control, and redaction policies.
9. **Token passthrough and confused deputy.** Never pass a client token downstream without validating audience, issuer, purpose, expiry, and delegated subject. Use audience-bound tokens, per-client consent where applicable, and explicit OBO/token-exchange patterns. The mandatory validated assertion at S1+ (§3.1) is the primary structural mitigation.
10. **No laundering through summaries.** See §18.3; it is a security property, not only a leakage rule.

## 20. Privacy considerations

A2K queries may reveal sensitive intent: legal exposure, HR issues, security incidents, customer problems, financial plans, M&A topics, or product strategy. Clients, KBs, catalogs, and gateways SHOULD:

1. minimize query content sent to KBs;
2. avoid sending unnecessary personal data;
3. respect classification and purpose limitation;
4. redact sensitive data where appropriate;
5. avoid exposing private KB Cards to unauthorized users;
6. avoid sending full user context unless required for authorization;
7. store sensitive query logs only where policy permits;
8. provide privacy-preserving discovery for restricted KBs.

## 21. Complete `ask` response example

```json
{
  "a2kVersion": "0.6-baseline",
  "ok": true,
  "operation": "ask",
  "sourceKbId": "urn:a2k:enterprise:finance-expense-policy",
  "answer": "Employees may expense business-class flights only when they have VP approval or when the flight duration exceeds 8 hours.",
  "passages": [],
  "claims": [
    {
      "id": "claim-1",
      "text": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
      "type": "policy",
      "status": "SUPPORTED",
      "citationIds": [
        "citation-1"
      ],
      "conflictsWith": []
    }
  ],
  "citations": [
    {
      "id": "citation-1",
      "claimIds": [
        "claim-1"
      ],
      "documentId": "doc:expense-policy:air-travel:v3",
      "title": "Employee Expense Policy — Air Travel",
      "selector": {
        "type": "TextQuoteSelector",
        "exact": "Business-class air travel requires VP approval unless the flight duration exceeds 8 hours.",
        "prefix": "Air travel policy. ",
        "suffix": " Exceptions must be approved by Finance."
      },
      "sourceUrl": "https://kb.example.com/finance/expense-policy#air-travel",
      "sourceHash": "sha256:e3b0c442...",
      "retrievedAt": "2026-07-07T09:12:00Z",
      "sourceLastUpdated": "2026-05-10T00:00:00Z",
      "classification": "internal"
    }
  ],
  "grounding": {
    "groundedRatio": 1.0,
    "ungroundedSpans": [],
    "confidence": 0.88,
    "confidenceMethod": "nli-entailment",
    "strictGroundingSatisfied": true
  },
  "freshness": {
    "sourceLastUpdated": "2026-05-10T00:00:00Z",
    "reviewedAt": "2026-05-10T00:00:00Z",
    "nextReviewDue": "2026-08-10T00:00:00Z",
    "retrievedAt": "2026-07-07T09:12:00Z",
    "stale": false,
    "validAsOf": "2026-07-07"
  },
  "accessDecision": {
    "decision": "allowed",
    "reason": "User satisfies finance.policy.read scope.",
    "appliedScopes": [
      "finance.policy.read"
    ],
    "dataClassification": "internal",
    "policyEngine": "enterprise-iam",
    "decisionId": "authz-789",
    "oboValidated": true
  },
  "audit": {
    "requestId": "req-001",
    "sessionId": "session-456",
    "agentId": "enterprise-assistant",
    "userId": "user-123",
    "sourceKbVersion": "2026-05-10.3",
    "kbCardVersion": "1.1.0",
    "policyDecision": "allowed",
    "logged": true,
    "loggedAt": "2026-07-07T09:12:01Z"
  },
  "conflicts": [],
  "referrals": [],
  "usage": {
    "latencyMs": 641,
    "retrievalCount": 4
  },
  "responseSignature": null,
  "error": null
}
```

The conformance corpus (A2K-Overview §13) is authoritative over this and all prose examples on any discrepancy.

## 22. Cited-response envelope schema fragment (illustrative)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:a2k:enterprise:schemas:cited-response-envelope:0.6-baseline",
  "type": "object",
  "required": [
    "a2kVersion",
    "ok",
    "operation",
    "sourceKbId",
    "error"
  ],
  "properties": {
    "a2kVersion": {
      "const": "0.6-baseline"
    },
    "ok": {
      "type": "boolean"
    },
    "operation": {
      "enum": [
        "search",
        "ask",
        "explain"
      ]
    },
    "sourceKbId": {
      "type": "string"
    },
    "answer": {
      "type": [
        "string",
        "null"
      ]
    },
    "passages": {
      "type": "array"
    },
    "claims": {
      "type": "array"
    },
    "citations": {
      "type": "array"
    },
    "grounding": {
      "type": "object"
    },
    "freshness": {
      "type": "object"
    },
    "accessDecision": {
      "type": "object"
    },
    "audit": {
      "type": "object"
    },
    "conflicts": {
      "type": "array"
    },
    "referrals": {
      "type": "array"
    },
    "usage": {
      "type": "object"
    },
    "pageInfo": {
      "type": [
        "object",
        "null"
      ],
      "properties": {
        "nextCursor": {
          "type": [
            "string",
            "null"
          ]
        },
        "hasMore": {
          "type": "boolean"
        },
        "pageLimit": {
          "type": "integer"
        }
      }
    },
    "responseSignature": {
      "type": [
        "object",
        "null"
      ]
    },
    "error": {
      "type": [
        "object",
        "null"
      ]
    }
  }
}
```

The complete executable contract is the modular schema package (A2K-Overview §13).
