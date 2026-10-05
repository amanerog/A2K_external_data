# Judge module — Competitive and market intelligence (MKT) — competencia_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `MKT` as `domain_primary` or `domain_secondary`.

- Domain: Inteligencia competitiva y de mercado / Competitive and market intelligence. Questions in GT v3: 19.
- Checks: MKT-1, MKT-2, MKT-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 7 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE MKT — Competitive and market intelligence
Scope: Specific competitors and their market: their products, prices, recent launches, financial results, customer perception, alliances, and consultancies’ readings of where the market is going, read from public and self-published sources.
A check may be "not_applicable" only where it says so.

MKT-1 Exactly the competitor named.
  PASS when: Everything in the answer is about the competitor the question names, at the level it names it: the brand vs. the legal entity (N26 vs. N26 Bank AG), the group vs. a local subsidiary or country offering (Nubank vs. Nu México), and the country if the question gives one.
  FAIL when: Part of the answer is about another company of similar name, a different entity of the same group, another country’s offering, or a different competitor that appeared in the same search result.
  NOT_APPLICABLE: never; this check always applies.

MKT-2 Each product, price, launch or result comes with a date or period.
  PASS when: Every product, feature, price or fee, launch, alliance or financial figure in the answer has a date or period from the raw tool output: the launch or announcement date, the "as of" date of the price page, the reporting quarter or year.
  FAIL when: Any product, price or figure is given with no date or period, or with a date the raw tool output does not support.
  NOT_APPLICABLE only when: the question asks for no product, price, launch, alliance or figure (for example, only a consultancy’s qualitative outlook).

MKT-3 Sources identified.
  PASS when: Every claim says where it comes from, by type and name: the company’s own website or app store page, a results report or regulatory filing, a named press outlet, a named consultancy report with its edition (e.g. "McKinsey Global Banking Annual Review 2025"), or a review platform with the sample it is based on.
  FAIL when: Claims are given with no source, or the source named is not in the raw tool output, or a consultancy reading is given without naming the report.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- Prices, features, reviews and open positions change continuously: any current instance of the right kind covers its element (core rule). MKT-2 still requires its date.
- Customer perception (ratings, reviews) must say which platform and how many reviews it is based on, under MKT-3; a single review is not "customer perception".

Examples:
- "What new account products does N26 offer?" The answer lists three products from the N26 website, each with its launch month, citing the N26 newsroom. → MKT-1 pass, MKT-2 pass, MKT-3 pass.
- Same question; one of the products comes from a Revolut article returned in the same search. → MKT-1 fail, and C3/C4 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "MKT"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the MKT examples of the judge ground truth (E3.1), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
