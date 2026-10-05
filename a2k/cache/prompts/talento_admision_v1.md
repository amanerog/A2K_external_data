# Judge module — Talent, leadership and compensation (TAL) — talento_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `TAL` as `domain_primary` or `domain_secondary`.

- Domain: Talento, dirección y compensación / Talent, leadership and compensation. Questions in GT v3: 3.
- Checks: TAL-1, TAL-2, TAL-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 30 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE TAL — Talent, leadership and compensation
Scope: People in senior positions: management structures and executive committees of comparable companies, recent changes in senior management, published career paths. The object is people and roles, not ownership.
A check may be "not_applicable" only where it says so.

TAL-1 Each person comes with their role.
  PASS when: Every person named in the answer comes with their exact role or title at the company asked about (CEO, CFO, member of the Executive Committee, non-executive director), as the raw tool output gives it.
  FAIL when: Any person is listed without a role, or with a role the raw tool output does not support.
  NOT_APPLICABLE: never; this check always applies.

TAL-2 The composition has a reference date.
  PASS when: A board, committee or management team is given "as of" a date (the date of the page, the annual report or the announcement). Every change in senior management comes with its announcement or effective date.
  FAIL when: A composition or a change is given with no date.
  NOT_APPLICABLE only when: the question asks for one person’s career path; the dates of each position are then part of the requested elements.

TAL-3 The right company.
  PASS when: Every person and role belongs to the exact company asked about: the group vs. a subsidiary (Siemens AG vs. Siemens Healthineers AG), the current company vs. a former employer. For one person’s career path, every position belongs to that person and not to a namesake.
  FAIL when: People of another entity of the group, former members presented as current, or positions of a different person with the same name.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- Thin domain: only 3 questions in GT v3. Its module cannot be measured until E3.2 adds examples (needed before E4.7, because shadow runs on all seven domains).
- Compensation figures, when asked for, also need their period and source (as in MAC-1), and are covered by C3 grounding.

Examples:
- "Who sits on the Executive Committee of Siemens AG according to its website?" The answer lists the members with their titles, "as of" the date shown on the Siemens page in the raw output. → TAL-1 pass, TAL-2 pass, TAL-3 pass.
- Same question; the list includes the CEO of Siemens Healthineers as a member. → TAL-3 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "TAL"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the TAL examples of the judge ground truth (E3.1, plus the examples added in E3.2), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
