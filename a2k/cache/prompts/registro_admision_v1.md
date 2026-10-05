# Judge module — Corporate registry and compliance status (CRP) — registro_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `CRP` as `domain_primary` or `domain_secondary`.

- Domain: Registro corporativo y estado de cumplimiento / Corporate registry and compliance status. Questions in GT v3: 37.
- Checks: CRP-1, CRP-2, CRP-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 30 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE CRP — Corporate registry and compliance status
Scope: A specific company or legal entity, and what an official registry or the entity’s own website says about it: tax status, incorporation records, ownership and beneficial owners, structure, certifications, registered offices.
A check may be "not_applicable" only where it says so.

CRP-1 An unambiguous identifier that matches the one asked for.
  PASS when: The answer gives at least one unambiguous identifier of the entity (LEI, registry number, tax ID such as CNPJ or NIF) taken from the raw tool output. If the question gives an identifier, the one in the answer is that one.
  FAIL when: No identifier is given, the identifier does not match the one in the question, or the answer mixes data of two records (two profiles of the same company, or a parent and its subsidiary) without saying which is which.
  NOT_APPLICABLE only when: the question asks about a registry or a procedure, not about an entity (for example, how a debt registry can be consulted).

CRP-2 The status comes with a date.
  PASS when: Every status (registered, active, dissolved, in liquidation, tax status, certification valid) comes with the date of the record or of the extraction, as in the raw tool output.
  FAIL when: A status is given with no date.
  NOT_APPLICABLE only when: the question asks for no status, explicitly or implicitly ("is X registered…" asks for a status).

CRP-3 The source is an official registry or the entity’s own site.
  PASS when: The facts come from an official registry (Companies House, GLEIF, Registro Mercantil, Receita Federal, SEC EDGAR, a national registry) or from the entity’s own website or filings, and the answer names it. A vendor record that reproduces an official registry counts if the raw tool output names that registry.
  FAIL when: The facts come only from press, aggregator sites or the model’s knowledge, or the source is not named.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- The same company can have several profiles in a vendor’s data (an official registry record, a trade-only profile, a separately registered subsidiary). The answer must use the official one or say that there are several: otherwise CRP-1 fails.
- Beneficial owner (a person) and ultimate parent (often another entity) are different things: giving one when the other is asked fails C2.
- An empty registry result is ambiguous (the record may not exist, may not be public in that jurisdiction, or coverage may be partial): the answer must say "not found in [registry]", never "does not exist".

Examples:
- "Is REVOLUT LTD registered in the UK, and what is its corporate history?" The answer gives the Companies House number, status "active" as of the extraction date, incorporation date and key filings, citing Companies House. → CRP-1 pass, CRP-2 pass, CRP-3 pass.
- Same question; the history mixes filings of Revolut Ltd and Revolut Holdings Ltd without saying so. → CRP-1 fail, and C4 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "CRP"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the CRP examples of the judge ground truth (E3.1), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
