# Judge module — Media and reputational intelligence (MED) — medios_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `MED` as `domain_primary` or `domain_secondary`.

- Domain: Inteligencia mediática y reputacional / Media and reputational intelligence. Questions in GT v3: 20.
- Checks: MED-1, MED-2, MED-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 1 day (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE MED — Media and reputational intelligence
Scope: Media coverage and narrative trends about the bank, the sector or its peers: volume, sentiment, emerging topics, geographic distribution. Neutral and positive coverage included, not only adverse findings.
A check may be "not_applicable" only where it says so.

MED-1 An explicit time window, resolved to real dates.
  PASS when: The answer states the dates its coverage spans ("from 2026-09-02 to 2026-10-02"), consistent with the question’s window (classification.time_anchor) and with the dates of the articles in the raw tool output. If the question gives no window, the answer states the one it actually covered.
  FAIL when: No window is stated, a relative window ("last 30 days") is not resolved to dates, or the articles used fall outside the window stated.
  NOT_APPLICABLE: never; this check always applies.

MED-2 The volume or number of sources behind each topic.
  PASS when: Each topic, narrative or sentiment reading comes with how much coverage supports it (number of articles or outlets) as found in the raw tool output. If the search returned a bounded sample, the answer says so and gives the counts as counts within that sample.
  FAIL when: Topics or rankings are given with no volume, counts are not supported by the raw tool output, or a bounded sample is presented as total coverage.
  NOT_APPLICABLE: never; this check always applies.

MED-3 The scope asked for.
  PASS when: The answer covers the scope the question asks for: the bank, the sector, or the named peers, each of them when the question compares several. A sentiment or share-of-voice comparison gives the reading for every entity compared.
  FAIL when: The answer covers a different scope (the sector when the bank was asked for, or only some of the named peers), or compares entities with readings for only one of them.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- Most vendors return dated articles, not aggregate statistics. A sentiment split, a share of voice or a ranking by volume is only grounded if the raw tool output contains it or the counts it is computed from; otherwise C3 fails.
- Validity is 1 day (D6): the module is strict about MED-1 because an undated media answer cannot be expired correctly.

Examples:
- "Which topics about Santander received the most coverage in the last 30 days?" The answer gives the window as 2026-09-02 to 2026-10-02 and five topics, each with its article count "within a sample of 50 articles returned by the search". → MED-1 pass, MED-2 pass, MED-3 pass.
- Same question; the answer lists topics of the European banking sector with no counts. → MED-2 fail, MED-3 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "MED"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the MED examples of the judge ground truth (E3.1), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
