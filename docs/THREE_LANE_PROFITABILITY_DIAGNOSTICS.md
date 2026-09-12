# Three-lane profitability diagnostics

Issue #289 adds one offline, fail-closed summary over the existing lane-specific
measurement artifacts. It does not replace the Lane 3 position ledger, change a
promotion policy, or authorize execution.

Run:

```bash
PYTHONPATH=src python3 scripts/three_lane_profitability_diagnostics.py \
  --lane1-report /path/to/prospective-champions/report.json \
  --lane2-measurement /path/to/lane2_measurements/latest.json \
  --lane2-shadow /path/to/verified-shadow-sync.json \
  --lane3-report /path/to/lane3-report.json \
  --json-output /path/to/three-lane-diagnostics.json \
  --markdown-output /path/to/three-lane-diagnostics.md
```

Missing inputs and costs are represented as `null` and block net economics. They
are never coerced to zero. Lane 2 resolver yield is reported independently and can
never imply profitability. An ENOSPC handoff state emits a storage-restored rerun
trigger. The artifact always states `profitability_verdict=DIAGNOSTIC_ONLY`, keeps
real trading disabled, and labels the decision-time causal dimensions exploratory.

The concise Markdown output contains the per-lane economics and completeness
tables. The JSON artifact additionally contains cohorts, scenario bands, identity
funnel evidence, causal diagnostics, evidence gaps, and candidate hypotheses that
must be frozen under #197 before prospective testing.
