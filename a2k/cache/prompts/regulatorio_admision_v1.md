# Judge module — Regulatory and policy environment (REG) — regulatorio_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `REG` as `domain_primary` or `domain_secondary`.

- Domain: Entorno regulatorio y de políticas / Regulatory and policy environment. Questions in GT v3: 23.
- Checks: REG-1, REG-2, REG-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 30 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE REG — Regulatory and policy environment
Scope: The rules that govern the bank and the public positions of supervisors, regulators and sector bodies on them: governance requirements, supervisory expectations, internal audit and control standards, regulatory positions. The object is the regulatory framework, not a company.
A check may be "not_applicable" only where it says so.

REG-1 The answer names the specific rule or guideline.
  PASS when: Every requirement, principle or position the answer states is attributed to a named document of a named issuer: its title or identifier (e.g. "EBA/GL/2021/05", "Directive 2013/36/EU (CRD)", "BCBS 239", "G20/OECD Principles of Corporate Governance") and, when the raw tool output gives it, the article, section, paragraph or principle number. For a public position (a speech, a consultation response, a press release), the document is that publication, named with its date.
  FAIL when: A requirement is described generically ("European regulation requires…", "supervisors expect…") without naming the instrument, or the document named is not the one the requirement comes from in the raw tool output.
  NOT_APPLICABLE: never; this check always applies.

REG-2 The answer states which version is in force, and since when.
  PASS when: The answer says which version of the instrument it describes (adoption or publication date, date of application, or "as amended by …") and the raw tool output supports it. If the raw tool output shows that the instrument was replaced, the answer describes the version in force or says clearly that it describes the old one.
  FAIL when: No version or date is given for an instrument that has versions; or the answer describes a superseded version as if it were in force.
  NOT_APPLICABLE only when: the question explicitly asks about a specific historical version, and the answer describes exactly that version; or the source is a one-off publication with no versions (a speech, a press release), whose date is then given under REG-1.

REG-3 The issuer is the one the question asks about.
  PASS when: If the question names an issuer (EBA, ECB, OECD, Basel Committee, FCA, a national supervisor), every requirement in the answer comes from that issuer, or the answer says explicitly which other issuer each additional point comes from. If the question names no issuer, every requirement is attributed to its own issuer and they are not mixed.
  FAIL when: The answer replaces the issuer asked for with another one (an ECB guide when the question asks for the EBA, a national transposition when the question asks for the EU text), or mixes requirements of several issuers as if they were one.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- The same topic is often covered by several issuers (EBA, ECB, Basel Committee, OECD). Most wrong answers in this domain cite a real document of the wrong issuer: REG-3 is the check that catches it.
- Principles and requirements must be taken from the raw tool output, not paraphrased from general knowledge of the regulation: that is a C3 failure even when the paraphrase is accurate.

Examples:
- "What responsibilities does the EBA assign to the management body regarding risk culture?" The answer quotes the EBA Guidelines on internal governance (EBA/GL/2021/05), section on risk culture, applicable since 31 Dec 2021, all from the EBA page in the raw output. → REG-1 pass, REG-2 pass, REG-3 pass.
- Same question; the answer describes the ECB Guide on governance and risk culture (2024) as "what the EBA requires". → REG-3 fail (wrong issuer), and C4 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "REG"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the REG examples of the judge ground truth (E3.1), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
