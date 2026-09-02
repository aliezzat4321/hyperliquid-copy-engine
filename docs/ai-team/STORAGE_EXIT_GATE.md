# Issue #120 storage exit gate

This is the manager/reviewer handoff for the one-time Hyperliquid storage closure. It
does not authorize a destructive action. REAL trading remains disabled. Exact-SHA
independent Claude Opus review is required before either apply step.

## Reviewed sequence

1. Stop every material writer listed in `config/storage_policy.json` using manager-owned
   deployment controls. Confirm PostgreSQL 14/`hlcopy` on port 5433 is healthy and has no
   other client sessions.
2. Generate a fresh PostgreSQL plan with `scripts/postgres_storage_compact_v1.py`. Record
   its SHA-256 and obtain exact-SHA independent review. Apply that exact manifest using
   `--apply --manifest ... --expected-sha256 ...`; preserve the audit output.
3. Re-run the retention audit against complete current funnel evidence. Obtain exact-SHA
   review of the resulting manifest, then apply only that manifest with
   `scripts/storage_retention_apply.py`. Its target defaults to 79% and must remain below
   80%. Before deletion it verifies the reviewed pool can reach the target.
4. Run `scripts/storage_controller.py --allow-baseline-without-previous` exactly once for
   a baseline. The manager-owned hourly job must
   pass the preceding successful observation and stop every name in `controlled_writers`
   on `STOP_ALL_MATERIAL_WRITERS`. Resume only after a later `ALLOW`. Deployment and
   service changes are owner-sensitive and deliberately excluded from this checkout.
   The manager must retire the incumbent guard's independent capture-resume path.
5. Restart capture/research/profitability loops through manager controls and collect
   successive controller observations across at least the configured 24-hour window.

## Required manager-owned workflow correction

The protected `.github/workflows/hyperliquid-emergency-storage-reclaim.yml` still passes
`--target-used-pct 92` to both retention apply invocations and passes a 6 GiB audit cap.
Before running it, the manager must change both apply invocations to
`--target-used-pct 79` and set `--max-delete-candidate-gib` to the smallest reviewed value
whose byte equivalent covers the audited deletion-candidate pool and the bytes required
to reach 79%, without exceeding the scripts' 24 GiB hard ceiling. The resulting workflow
commit and manifest require exact-SHA independent Claude Opus review. The autonomous
builder cannot edit this protected path.

## Closure evidence

- PostgreSQL apply succeeds; both target relations materially shrink; provenance checks
  remain zero; and `fills_after >= fills_before`.
- Retention apply reaches below 80% (preferably at or below 75%) using only reviewed
  candidates. Recent and robust tape remains full fidelity. Useful older evidence remains
  protected as `COMPRESS_CANDIDATE` until a separately reviewed compression implementation
  exists; it must not be deleted to satisfy this gate.
- Every policy dataset has a non-empty owner, writer, retention class, byte budget, growth
  budget and pressure control, and no material writer is absent.
- Successive observations show bounded bytes/hour, acceptable time-to-full, no budget
  breach and stable headroom while all normal loops run.
- Independent review provenance records the reviewed commit SHA and both manifest hashes.
  Real trading stays off.

If any invariant, evidence source, history sample, manifest identity, writer mapping or
forecast is missing, stale or inconsistent, the gate remains open and writers remain
stopped. Storage returns to maintenance-only only after every item is demonstrated.
