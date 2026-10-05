"""The POC3 data-domain taxonomy shared by the admission classifier, the
judge's rubric modules and the expiry policy -- see the cache lifecycle
proposal (Propuesta_cache_CdV, Annex A6 for the domains and their judge
checks, Annex A8 for the starting expiry values decided in D6).

One source of truth on purpose: retrieval (proposal Sheet 4) classifies
incoming questions with this same taxonomy, so a domain renamed here and not
there would silently stop matching its threshold/expiry rows. The domain
definitions and boundaries are read from the admission classifier prompt and
the check IDs from each judge module (prompts/), so they cannot drift apart.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field

from . import prompts

CLASSIFIER_PROMPT = "clasificador_admision_v1"


@dataclass(frozen=True)
class Domain:
    code: str
    name: str
    # The judge module file (prompts/<module>.md) loaded for this domain.
    module: str
    # Starting expiry in days (Annex A8 / D6) -- the live value is read from
    # Aurora's expiry_policy table; this is only the seed and the fallback for
    # offline runs with no database.
    default_validity_days: int
    # In RSK every check always applies: "not applicable" is not an escape
    # hatch there (Annex A6, decision rule; the module says so too).
    strict: bool = False
    # Read from the prompt files at import: the definition the classifier
    # gives (retrieval classifies with the very same text) and the check IDs
    # of the judge module.
    definition: str = field(default="", compare=False)
    checks: tuple[str, ...] = field(default=(), compare=False)

    @property
    def module_text(self) -> str:
        return prompts.block(self.module)


_SEEDS = (
    ("REG", "Regulatory and policy environment", "regulatorio_admision_v1", 30, False),
    ("MKT", "Competitive and market intelligence", "competencia_admision_v1", 7, False),
    ("TAL", "Talent, leadership and compensation", "talento_admision_v1", 30, False),
    ("MAC", "Macroeconomic and prudential statistics", "macro_admision_v1", 30, False),
    ("CRP", "Corporate registry and compliance status", "registro_admision_v1", 30, False),
    ("RSK", "Risk, integrity and adverse information", "riesgo_admision_v1", 3, True),
    ("MED", "Media and reputational intelligence", "medios_admision_v1", 1, False),
)
_CODES = "|".join(code for code, *_ in _SEEDS)


def _classifier_section(start: str, end: str) -> str:
    text = prompts.block(CLASSIFIER_PROMPT)
    i = text.index(start)
    return text[i + len(start) : text.index(end, i)]


def _definitions() -> dict[str, str]:
    """The domain list of the classifier prompt: "REG — Regulatory ... e.g. ..."."""
    section = _classifier_section("Never more than two domains.\n", "   Boundaries between domains")
    parts = re.split(rf"^   ({_CODES}) — ", section, flags=re.MULTILINE)
    found = {parts[i]: " ".join(parts[i + 1].split()) for i in range(1, len(parts), 2)}
    if set(found) != {code for code, *_ in _SEEDS}:
        raise RuntimeError(f"classifier prompt defines domains {sorted(found)}")
    return found


def _check_ids(module: str) -> tuple[str, ...]:
    ids = tuple(re.findall(r"^([A-Z]{3}-\d+) ", prompts.block(module), flags=re.MULTILINE))
    if not ids:
        raise RuntimeError(f"{module}.md defines no checks")
    return ids


_DEFINITIONS = _definitions()

# The order matters: the decision rule and the judge read checks per domain.
DOMAINS: dict[str, Domain] = {
    code: Domain(code, name, module, days, strict, definition=_DEFINITIONS[code], checks=_check_ids(module))
    for code, name, module, days, strict in _SEEDS
}

# "Boundaries between domains" of the classifier prompt, for retrieval.
BOUNDARIES = textwrap.dedent(
    _classifier_section("Boundaries between domains (apply them in this order):\n", "\n\n4. question_type")
).strip()

# Expiry row for data whose time anchor lies entirely in the past (Annex A8).
HISTORICAL = "historical"
HISTORICAL_VALIDITY_DAYS = 365


def taxonomy_for_prompt() -> str:
    """Domains and boundaries exactly as the admission classifier reads them,
    so retrieval gives a question the same domain admission gave it."""
    domains = "\n".join(f"- {d.code} — {d.definition}" for d in DOMAINS.values())
    return f"{domains}\n\nBoundaries between domains (apply them in this order):\n{BOUNDARIES}"
