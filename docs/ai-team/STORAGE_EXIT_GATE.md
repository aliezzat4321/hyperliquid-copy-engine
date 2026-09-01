# Issue #120 storage exit gate

This is the manager/reviewer handoff for the one-time Hyperliquid storage closure. It
does not authorize a destructive action. REAL trading remains disabled. Exact-SHA
independent Claude Opus review is required before either apply step.

## Reviewed sequence

1. Stop every material writer listed in `config/storage_policy.json` using manager-owned
   deployment controls. Confirm PostgreSQL 14/`hlcopy` on port 5433 is healthy and has no
   other client sessions. Do not inspect or mutate any unrelated platform.
2. Generate a fresh PostgreSQL plan with `scripts/postgres_storage_compact_v1.py`. Record
   the plan SHA-256 and obtain exact-SHA independent review. Apply that exact manifest
   using `--apply --manifest ... --expected-sha256 ...`. Preserve the audit output.
3. Re-run the storage-retention audit against complete current funnel evidence. Record
   the manifest SHA-256 and obtain exact-SHA independent review. Apply only that exact
   manifest with `scripts/storage_retention_apply.py`; the default target is 75% and the
   script rejects targets at or above 80%.
4. Run `scripts/storage_controller.py` to create the baseline observation. Wire the
   manager-owned hourly job to provide the preceding successful observation and to stop
   **all** names in `controlled_writers` whenever the decision is
   `STOP_ALL_MATERIAL_WRITERS`. Resume only after a later `ALLOW` decision. Deployment or
   service-definition changes are owner-sensitive and are deliberately not in this PR.
5. Restart capture/research/profitability loops through manager controls. Collect
   successive controller observations across at least the configured 24-hour history
   window.

## Closure evidence (all required)

- PostgreSQL apply audit succeeds; both compacted relation sizes are materially lower;
  exact provenance checks are zero; `fills_after >= fills_before`.
- Retention apply audit reaches below 80% (preferably at or below 75%) using only reviewed
  candidates. Recent and robust tape remains full fidelity. Older useful evidence is not
  deleted; compression/downsampling requires its own reviewed implementation evidence.
- Every policy dataset has a non-empty owner, writer, retention class, byte budget,
  growth budget and pressure control. No material writer is absent from the registry.
- Successive observations report bounded bytes/hour, acceptable time-to-full, no budget
  breach and stable headroom while all normal loops run.
- Independent review provenance records the reviewed commit SHA and both reviewed
  manifest hashes. No unrelated-platform access occurred and real trading stayed off.

If any invariant, evidence source, history sample, manifest identity, writer mapping or
forecast is missing, stale or inconsistent, the gate remains open and writers remain
stopped. Storage returns to maintenance-only only after every item above is demonstrated.
