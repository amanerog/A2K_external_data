"""KB Card model (A2K-KBCard-Schema 4.md, section 3 and children)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Principal(BaseModel):
    type: Literal["group", "individual"]
    id: str
    identityProvider: str | None = None
    name: str
    contact: str | None = None


class Ownership(BaseModel):
    ownerTeam: str
    businessOwner: Principal | None = None
    technicalOwner: Principal | None = None
    maintainers: list[str] = Field(default_factory=list)
    supportChannel: str | None = None
    escalationPolicy: str | None = None


class AuthorityProof(BaseModel):
    method: str
    approvedBy: str | None = None
    approvalReference: str | None = None
    approvedAt: str | None = None


class Authority(BaseModel):
    systemOfRecord: Literal["canonical", "scoped-canonical", "vendor", "draft", "personal", "unverified"]
    sorScope: list[str] = Field(default_factory=list)
    authorityScope: str
    relationship: str
    publisher: str
    proof: AuthorityProof | None = None


class Attestation(BaseModel):
    lastAttested: str | None = None
    nextAttestationDue: str | None = None
    attestedBy: str | None = None
    actionOnExpiry: str | None = None


class Lifecycle(BaseModel):
    status: Literal["active", "draft", "deprecated", "retired"]
    createdAt: str
    lastUpdated: str
    reviewedAt: str | None = None
    reviewBy: str | None = None
    attestation: Attestation | None = None
    deprecationDate: str | None = None
    supersededBy: str | None = None
    replacementKbId: str | None = None
    changeLogUrl: str | None = None


class Access(BaseModel):
    visibility: str
    dataClassification: Literal[
        "public", "internal", "confidential", "restricted", "highly-restricted", "regulated"
    ]
    audiences: list[str] = Field(default_factory=list)
    requiredScopes: list[str] = Field(default_factory=list)
    allowedDepartments: list[str] = Field(default_factory=list)
    restrictedDepartments: list[str] = Field(default_factory=list)
    crossDepartmentUse: bool = True
    externalUseAllowed: bool = False
    requiresUserContext: bool = False
    rowLevelSecurity: bool = False
    ethicalWallSensitive: bool = False

    @property
    def derived_security_tier(self) -> Literal["S0", "S1", "S2"]:
        """A2K-KBCard-Schema section 4.4.1."""
        if self.ethicalWallSensitive or self.dataClassification in {
            "restricted",
            "highly-restricted",
            "regulated",
        }:
            return "S2"
        if self.dataClassification == "confidential":
            return "S1"
        return "S0"


class Enterprise(BaseModel):
    ownership: Ownership
    authority: Authority
    lifecycle: Lifecycle
    access: Access


class TemporalRange(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class Coverage(BaseModel):
    scope: str
    departments: list[str] = Field(default_factory=list)
    geography: list[str] = Field(default_factory=list)
    businessUnits: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    temporalRange: TemporalRange | None = None
    completeness: str | None = None


class KnowledgeFreshness(BaseModel):
    updateCadence: str
    lastUpdated: str
    reviewedAt: str | None = None
    nextReviewDue: str | None = None
    stalenessPolicy: str | None = None


class KnowledgeProfile(BaseModel):
    domains: list[str]
    topics: list[str] = Field(default_factory=list)
    coverage: Coverage
    generation: str
    freshness: KnowledgeFreshness
    languages: list[str] = Field(default_factory=list)
    sourceSystems: list[str] = Field(default_factory=list)


class OperationDeclaration(BaseModel):
    name: Literal["search", "ask", "explain", "getDocument"]
    requiredLevel: int
    inputModes: list[str]
    outputModes: list[str]


class OAuth2Scheme(BaseModel):
    scopes: list[str] = Field(default_factory=list)


class Auth(BaseModel):
    schemes: list[str]
    oauth2: OAuth2Scheme | None = None
    delegatedUserRequired: bool = False
    serviceAccountAllowed: bool = True
    oboAssertionRequired: bool = False
    signedRequestRequired: bool = False


class Policies(BaseModel):
    allowedUse: list[str] = Field(default_factory=list)
    disallowedUse: list[str] = Field(default_factory=list)
    citationPolicy: str = "required"
    redistribution: str | None = None
    queryRetention: str | None = None
    piiHandling: str | None = None
    humanReviewRequiredFor: list[str] = Field(default_factory=list)
    sensitiveDecisionUse: str | None = None


class AuditDeclaration(BaseModel):
    loggingRequired: bool = True
    immutableLogRequired: bool = False
    logTarget: str | None = None
    logRetention: str | None = None
    includeUserId: bool = True
    includeAgentId: bool = True
    includeCitationIds: bool = True
    includeProofFooter: bool = False


class RegulatedExtension(BaseModel):
    supported: bool
    features: list[str] = Field(default_factory=list)
    regulatedDomains: list[str] = Field(default_factory=list)
    minimumAuditRetention: str | None = None
    immutableAuditRequired: bool = False


class ConformanceDeclaration(BaseModel):
    level: int
    profile: str
    features: list[str] = Field(default_factory=list)


class CardSignature(BaseModel):
    alg: str
    kid: str
    canonicalization: str
    jws: str


class KBCard(BaseModel):
    a2kVersion: str = "0.6-baseline"
    profile: Literal["enterprise"] = "enterprise"
    id: str
    name: str
    description: str
    url: str
    transport: Literal["https-json", "mcp", "a2a"]
    enterprise: Enterprise
    knowledgeProfile: KnowledgeProfile
    # Local extension, not part of the A2K-KBCard-Schema spec (which has no
    # additionalProperties:false, so this doesn't break conformance): a numeric
    # tie-breaker for vendor routing among multiple topical matches, lower =
    # preferred. Added 2026-08-18 at the routing agent's request; not yet
    # consulted by any routing logic -- see a2k.listVendors in mcp_server/server.py.
    priority: int | None = None
    # Local extension, same conformance rationale as `priority` above. Tells the
    # calling agent how to build a2k.ask/a2k.search's `query` for *this* vendor:
    # "entity" -- the vendor's search is a name/text matcher with no semantic
    #   fallback (confirmed live for Sayari's search_entities -- a query padded
    #   with extra words routinely returns zero matches, see adapters/sayari_mcp.py
    #   and mcp_server/server.py's a2k.ask docstring) -- the caller must extract
    #   just the entity name before querying.
    # "natural_language_query" -- the vendor tolerates (or expects) a fuller
    #   question, e.g. because it falls back to a semantic search tool when a
    #   strict name match finds nothing (confirmed for Cala -- entity_search ->
    #   knowledge_query -> knowledge_search, see adapters/cala_mcp.py).
    # Deliberately `str | None`, not a Literal/enum: a caller reading this is
    # expected to treat any missing/unrecognized value as "natural_language_query"
    # (the safer default -- send the question unmodified rather than guess at
    # entity extraction) rather than have card loading itself fail on a new value
    # a future vendor might need. Not yet consulted by any routing logic in this
    # codebase itself -- see a2k.listVendors in mcp_server/server.py.
    queryType: str | None = None
    operations: list[OperationDeclaration]
    auth: Auth
    policies: Policies | None = None
    audit: AuditDeclaration | None = None
    regulatedExtension: RegulatedExtension | None = None
    conformance: ConformanceDeclaration | None = None
    signature: CardSignature | None = None
