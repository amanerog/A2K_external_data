# Judge module — Risk, integrity and adverse information (RSK) — riesgo_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `RSK` as `domain_primary` or `domain_secondary`.

- Domain: Riesgo, integridad y detección de información adversa / Risk, integrity and adverse information. Questions in GT v3: 9.
- Checks: RSK-1, RSK-2, RSK-3, RSK-4. Every check always applies (no tolerance).
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 3 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE RSK — Risk, integrity and adverse information
Scope: Whether something adverse is associated with a named person or company: sanctions and watch-list designations, court or insolvency proceedings, tax debts, appearances in offshore leaks, adverse press, unverified allegations.
No tolerance: every check of this module always applies. "not_applicable" is never allowed.

RSK-1 Identity disambiguation.
  PASS when: The answer states how the subject was identified (date of birth, nationality, identity document, registry number, address, role) and ties every adverse finding to that identity. If the raw tool output contains namesakes and the question gives nothing to tell them apart, the answer says so explicitly and does not attribute their findings to the subject.
  FAIL when: Any finding is attributed to the subject on a name match alone, or findings of different people or entities with similar names are merged (the Ali Ansari / Ali al-Ansari case).

RSK-2 The lists or sources checked are listed.
  PASS when: The answer names the lists, registries or sources it checked (OFAC SDN, EU consolidated list, UN, UK OFSI, national lists, court records, offshore-leaks databases, named press), and, when the raw tool output gives it, the date of the check. Every source named was actually queried in the raw tool output.
  FAIL when: No sources are named, or the answer names sources that the raw tool output never queried.

RSK-3 Verified facts are separated from allegations.
  PASS when: The wording makes clear which findings are verified (a designation, a court ruling, a registry record) and which are allegations (press reports, leaks, unproven accusations), with their source. If there are no allegations, nothing unverified is presented as a fact.
  FAIL when: An allegation is presented as a fact, a verified fact is downplayed as an allegation, or both are mixed with no distinction.

RSK-4 A "no findings" answer is only fit if it says where it looked.
  PASS when: Every negative statement ("no designations", "no proceedings found") says which sources returned nothing, for which identity, and the raw tool output shows those searches and their empty results. A source with partial coverage of that jurisdiction is reported as "not found in [source]", not as a clean result. If the answer contains only positive findings, this check passes.
  FAIL when: A negative is stated without scope ("no sanctions found", "clean"), or is based on a search that the raw tool output does not show, or an empty result is presented as proof of absence.

Domain notes:
- This is the most expensive domain to get wrong: a wrongly cleared person or a wrong designation is served to other users for the whole validity (3 days, D6).
- "Current" sanctions: a designation that was lifted must be reported as past, with its dates; presenting it as current fails C3.
- A substantive negative is a valid answer here, but only with RSK-1, RSK-2 and RSK-4 satisfied.

Examples:
- "Does Ali Ansari have current sanctions?" The answer identifies the person by date of birth and nationality from the OFAC record, lists OFAC SDN and the EU consolidated list as checked on 2026-09-30, reports the OFAC designation as verified and a press allegation separately. → RSK-1 pass, RSK-2 pass, RSK-3 pass, RSK-4 pass.
- Same question; "No sanctions were found", after one search that returned two people of similar name. → RSK-1 fail, RSK-2 fail, RSK-4 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "RSK"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` (in RSK, `not_applicable` counts as `fail`), on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the RSK examples of the judge ground truth (E3.1, plus the examples added in E3.2), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
