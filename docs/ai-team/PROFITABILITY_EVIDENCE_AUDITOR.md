# Profitability Evidence Auditor v1

The reusable auditor in `hlcopy.profitability.evidence_auditor` enforces the data
integrity section of `PROFITABILITY_STANDARD.md`. It validates evidence and arithmetic;
it does not estimate missing economics, rank strategies, or replace the numeric
promotion policy.

Call `audit_evidence(bundle)` with a normalized JSON-compatible bundle. The required
top-level fields are:

- `report_version`, `policy_version`, and `audited_at`;
- `provenance.source`, exact `provenance.data_sha256`, and an exact Git commit SHA in
  `provenance.code_commit`;
- `evaluation_window.start` / `end` and `max_data_age_seconds`;
- an explicit boolean `selection.prospective` and, for prospective evidence, a
  `frozen_at` strictly before the window;
- an independently supplied `population.input_count`, `funding_applicable`, `positions`,
  and `economics_totals.final_net`.

Each position has a stable `position_id`, an explicit `closed`, `open`, `unresolved`,
or `quarantined` status, and signal-to-position lifecycle timestamps. Closed positions
must include gross PnL and labelled `measured` or `assumption` amounts for fees, spread,
depth, slippage, and impact. Complete funding coverage is required when applicable.
Every non-closed position requires unresolved MTM. The declared final net must equal:

`gross - fees - spread - depth - slippage - impact - funding + unresolved MTM`

The returned `profitability-evidence-audit-v1` report contains deterministic `PASS` or
`FAIL`, structured blockers, population and trading-day counts, reconciled economics,
versions, provenance, and a canonical evidence SHA-256. Diagnostics remain available
on failure and are bounded to 100 blockers. Missing economics produces `null` final net
and `UNKNOWN_MISSING_EVIDENCE`; it is never converted to zero.

`PASS` means the supplied evidence reconciles. It does **not** automatically mean the
result can be called validated or promotion-eligible. Those booleans remain false unless
all integrity checks pass, the evidence is explicitly prospective/frozen, final net is
positive, and every material execution-cost component is measured rather than assumed.
When any fee/spread/depth/slippage/impact component uses `basis: assumption`, the report
is labelled `economics_basis: SCENARIO_ASSUMPTIONS` and remains diagnostic only.
Overlapping positions are valid; timestamp monotonicity is enforced within each
position lifecycle rather than by falsely requiring one position to close before the
next can open.

Lane 3 JSONL is integrated through `lane3_bundle()` or the CLI:

```text
python -m hlcopy.profitability.evidence_audit_cli \
  --format lane3-jsonl --input audit.jsonl --manifest manifest.json --output audit-report.json
```

The manifest supplies version/provenance/window metadata, the independent population
baseline, and any per-position cost or MTM evidence under `position_economics`.
`lane3_bundle()` preserves a supplied population baseline instead of replacing it with
the number of rows it happened to reconstruct, so unexplained population loss remains
detectable. Existing gross-only Lane 3 ledgers remain diagnosable but fail closed on the
missing material evidence. A failing audit exits 2. Passing this integrity audit does
not authorize real trading and does not by itself satisfy the separate promotion-policy
thresholds.
