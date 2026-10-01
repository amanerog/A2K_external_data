"""The POC3 data-domain taxonomy shared by the admission classifier, the
judge's rubric modules and the expiry policy -- see the cache lifecycle
proposal (Propuesta_cache_CdV, Annex A6 for the domains and their judge
checks, Annex A8 for the starting expiry values decided in D6).

One source of truth on purpose: retrieval (proposal Sheet 4) classifies
incoming questions with this same taxonomy, so a domain renamed here and not
there would silently stop matching its threshold/expiry rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Domain:
    code: str
    name: str
    description: str
    # Domain-specific judge checks (Annex A6), as (check_id, what it requires).
    # Each is worded as a property of the ANSWER: worded loosely ("sources
    # identified"), the judge flipped between reading it against the answer
    # and against the tool output on the same input (POCv3 id 57, 2026-10-01).
    checks: tuple[tuple[str, str], ...]
    # Starting expiry in days (Annex A8 / D6) -- the live value is read from
    # Aurora's expiry_policy table; this is only the seed and the fallback for
    # offline runs with no database.
    default_validity_days: int
    # In RSK every check always applies: "not applicable" is not an escape
    # hatch there (Annex A6, decision rule).
    strict: bool = False


DOMAINS: dict[str, Domain] = {
    d.code: d
    for d in (
        Domain(
            code="REG",
            name="Regulatory and policy environment",
            description="Regulations, supervisory guidelines, standards and policy documents (EBA, ECB, BIS, OECD, national regulators) and what they require.",
            checks=(
                ("REG-1", "The answer names the specific rule or guideline (issuer, identifier, article)."),
                ("REG-2", "If the question is about what a specific rule or guideline requires, the answer states which version is in force and since when. Not applicable to conceptual questions about a framework or a principle (what an internal control system is for, what SREP aims at)."),
                ("REG-3", "The issuer in the answer is the one the question asks about (e.g. EBA vs. ECB vs. OECD)."),
            ),
            default_validity_days=30,
        ),
        Domain(
            code="MKT",
            name="Competitive and market intelligence",
            description="Competitors' products, prices, fees, launches, strategy and market position.",
            checks=(
                ("MKT-1", "The answer is about exactly the competitor named (e.g. the N26 brand vs. the legal entity N26 Bank AG)."),
                ("MKT-2", "In the answer, each product or price comes with a date or period."),
                ("MKT-3", "The answer itself says where each product or price comes from (the company's own site, a named press outlet, a consultancy) -- sources that only appear in the tool output do not count."),
            ),
            default_validity_days=7,
        ),
        Domain(
            code="TAL",
            name="Talent, leadership and compensation",
            description="Boards, executive committees, senior appointments, people's roles and compensation.",
            checks=(
                ("TAL-1", "In the answer, each person comes with their role."),
                ("TAL-2", "The answer gives a reference date for the composition it describes."),
                ("TAL-3", "The answer is about the right company (group vs. subsidiary)."),
            ),
            default_validity_days=30,
        ),
        Domain(
            code="MAC",
            name="Macroeconomic and prudential statistics",
            description="Macroeconomic series, interest and FX rates, prudential ratios and other statistical aggregates.",
            checks=(
                ("MAC-1", "Every figure in the answer has a unit, a period and a source stated in the answer."),
                ("MAC-2", "If the question names a series or code (e.g. SGS 20539), the answer uses that one."),
                ("MAC-3", "The answer states an explicit period -- mandatory, not optional."),
            ),
            default_validity_days=30,
        ),
        Domain(
            code="CRP",
            name="Corporate registry and compliance status",
            description="Company registration data, identifiers (LEI, registry number, tax ID), legal status, ownership structure, filings and compliance status.",
            checks=(
                ("CRP-1", "The answer gives an unambiguous identifier (LEI, registry number, tax ID) that matches the one asked for."),
                ("CRP-2", "In the answer, the status comes with a date."),
                ("CRP-3", "The answer names its source, and it is one of: an official registry, the entity's own site, or a company-data provider that republishes registry data (e.g. GLEIF, North Data, einforma, Axesor, InfoNIF, cnpj.biz). General web pages, Wikipedia or the data vendor's own knowledge base with no underlying source do not count."),
            ),
            default_validity_days=30,
        ),
        Domain(
            code="RSK",
            name="Risk, integrity and adverse information",
            description="Sanctions, PEP status, watchlists, litigation, adverse media and integrity red flags about a person or entity.",
            checks=(
                ("RSK-1", "The answer disambiguates the identity (date of birth, nationality, ID) -- e.g. Ali Ansari vs. Ali al-Ansari."),
                ("RSK-2", "The answer lists the lists or sources checked."),
                ("RSK-3", "The answer separates verified facts from allegations."),
                ("RSK-4", "If the answer reports no findings, it says where it looked."),
            ),
            default_validity_days=3,
            strict=True,
        ),
        Domain(
            code="MED",
            name="Media and reputational intelligence",
            description="Media coverage, narratives, sentiment, trending topics and reputation over a time window.",
            checks=(
                ("MED-1", "The answer states an explicit time window, resolved to real dates ('last 30 days' -> from/to)."),
                ("MED-2", "The answer gives the volume or number of sources behind each topic."),
                ("MED-3", "The answer covers the scope asked for (the bank, the sector, the peers)."),
            ),
            default_validity_days=1,
        ),
    )
}

# Expiry row for data whose time anchor lies entirely in the past (Annex A8).
HISTORICAL = "historical"
HISTORICAL_VALIDITY_DAYS = 365


def taxonomy_for_prompt() -> str:
    return "\n".join(f"- {d.code}: {d.name} -- {d.description}" for d in DOMAINS.values())
