# Judge module — Macroeconomic and prudential statistics (MAC) — macro_admision_v1

Domain module of the admission judge (`juez_admision_v1`), proposal Annex A6. The prompt
assembler inserts the "Module text" below in place of `{{DOMAIN_MODULES}}` when the
admission classifier returns `MAC` as `domain_primary` or `domain_secondary`.

- Domain: Estadísticas macroeconómicas y prudenciales / Macroeconomic and prudential statistics. Questions in GT v3: 9.
- Checks: MAC-1, MAC-2, MAC-3. Each check says when it may be not applicable.
- Related values decided in the working session of 30 Sep 2026: similarity threshold 0.90
  (D3), expiry 30 days (D6), judge precision ≥ 95% on its ground truth (D2).

---

## Module text (inserted into the judge prompt as-is)

```
MODULE MAC — Macroeconomic and prudential statistics
Scope: Aggregate figures for a country, the euro area or a sector: capital and liquidity ratios of EU banks, GDP, inflation, official interest rates, credit balances, other national statistics. No specific company is the object.
A check may be "not_applicable" only where it says so.

MAC-1 Every figure has a unit, a period and a source.
  PASS when: Every figure comes with its unit (%, % year on year, EUR million, index points), the period it refers to and its source (Eurostat, ECB, BACEN SGS, EBA Risk Dashboard), all as in the raw tool output. If the raw tool output marks a figure as provisional, a flash estimate or revised, the answer says so.
  FAIL when: Any figure lacks its unit, period or source, or a provisional figure is presented as final.
  NOT_APPLICABLE: never; this check always applies.

MAC-2 The named series or code is the one used.
  PASS when: If the question names a series, dataset, index or code (SGS 20539, prc_hicp_aind, SX7E), the figures in the answer come from exactly that series, as identified in the raw tool output.
  FAIL when: The answer uses a related but different series (another currency variant of the index, another dataset, a different aggregation) without saying so.
  NOT_APPLICABLE only when: the question names no series, dataset, index or code.

MAC-3 An explicit period is mandatory, not optional.
  PASS when: The answer states the actual period of each figure: "latest available" or "most recent" is resolved to the month, quarter or year it is, and a trend states its start and end periods.
  FAIL when: A figure is given as "the latest", "current" or "recent" without saying which period it is, or a trend is given without its start and end.
  NOT_APPLICABLE: never; this check always applies.

Domain notes:
- The geography is part of the subject (C4): euro area vs. EU vs. Spain are different series.
- Questions here are usually "metrics_aggregates": C2 fails when the answer describes the trend in words but gives no figure.

Examples:
- "What are the ECB MRR and DFR, and the latest euro-area HICP?" The answer gives both rates with the decision date and HICP "2.2% y/y, August 2026 (flash estimate), Eurostat". → MAC-1 pass, MAC-2 not_applicable, MAC-3 pass.
- Same question; HICP given as "around 2% recently". → MAC-1 fail, MAC-3 fail.
```

---

## Usage notes (not part of the module text)

- The judge returns one entry per check in `modules`, with `domain = "MAC"`. The
  decision rule in code (Annex A6) requires every check of this module to be `pass` or `not_applicable`, on top of the common core C1–C4.
- The check IDs are the ones in Annex A6 of the proposal; keep them stable across versions,
  because the judge GT and the serving log refer to them.
- Test: the MAC examples of the judge ground truth (E3.1, plus the examples added in E3.2), with the classifier, the
  core and this module together, as part of E4.7.
- Changing it: owned by the domain owner (rubric), passed through the judge GT by the
  technical team (Annex A10). A new version is released with the judge and, from phase 4,
  re-judges the active entries of this domain.

### Changelog

- **v1** — first version, from the checks of Annex A6 and what the POC2.1 and POC3 results
  showed for this domain.
