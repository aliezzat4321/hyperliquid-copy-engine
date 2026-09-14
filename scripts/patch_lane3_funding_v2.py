#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1])
service = root / 'services/invo-notification-executor/src/service.ts'
env_file = root / 'services/invo-notification-executor/.env.example'
readme = root / 'services/invo-notification-executor/README.md'

text = service.read_text()

def rep(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'expected exactly one service replacement, found {count}: {old[:120]!r}')
    text = text.replace(old, new, 1)

rep(
"""    shadowTakerFeeBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_TAKER_FEE_BPS', 4.5)),
    shadowMinNotionalUsd: Math.max(1, n('NOTIFICATION_TRADER_SHADOW_MIN_NOTIONAL_USD', 10)),
""",
"""    shadowTakerFeeBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_TAKER_FEE_BPS', 4.5)),
    shadowFundingOracleMaxDelayMs: Math.max(500, n('NOTIFICATION_TRADER_SHADOW_FUNDING_ORACLE_MAX_DELAY_MS', 10_000)),
    shadowMinNotionalUsd: Math.max(1, n('NOTIFICATION_TRADER_SHADOW_MIN_NOTIONAL_USD', 10)),
""",
)

rep(
"""  minNotionalUsd: cfg.shadowMinNotionalUsd,
  takerFeeBps: cfg.shadowTakerFeeBps,
};
""",
"""  minNotionalUsd: cfg.shadowMinNotionalUsd,
  takerFeeBps: cfg.shadowTakerFeeBps,
  fundingOracleMaxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
};
""",
)

rep(
"""let backoffMs = 0;
let discoverySurfaceIndex = 0;
""",
"""let backoffMs = 0;
let discoverySurfaceIndex = 0;
let lastFundingOracleBoundaryMs = -1;
const FUNDING_INTERVAL_MS = 60 * 60 * 1000;
""",
)

rep(
"""    exposureCheckpoints: [{ atMs: fill.receivedAtMs, notionalUsd: fill.notionalUsd }],
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
""",
"""    exposureCheckpoints: [{ atMs: fill.receivedAtMs, size: fill.filledSize }],
    fundingOracleCheckpoints: [],
    fundingCarryUsd: 0,
    fundingAccruedThroughMs: fill.receivedAtMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
""",
)

rep(
"""    fundingModel: 'hyperliquid fundingHistory; no zero-cost omission',
""",
"""    fundingModel: 'Hyperliquid fundingHistory rate x prospective oraclePx x position size; missing oracle fails closed',
""",
)

rep(
"""  const checkpoints = [
    ...(managed.exposureCheckpoints ?? []),
    { atMs: fill.receivedAtMs, notionalUsd: newEntryNotional },
  ];
""",
"""  const checkpoints = [
    ...(managed.exposureCheckpoints ?? []),
    { atMs: fill.receivedAtMs, size: newSize },
  ];
""",
)

rep(
"""          || !(Number(managed.entryNotionalExecutedUsd) > 0)
          || !Array.isArray(managed.exposureCheckpoints);
""",
"""          || !(Number(managed.entryNotionalExecutedUsd) > 0)
          || !Array.isArray(managed.exposureCheckpoints)
          || !Array.isArray(managed.fundingOracleCheckpoints);
""",
)

rep(
"""        let fundingUsd: number | null = null;
        let fundingPoints: number | null = null;
        let fundingModel: string | null = null;
        let fundingError: string | null = null;
        if (!legacy) {
          try {
            const funding = await fundingForPosition(managed, fill.receivedAtMs);
            fundingUsd = funding.fundingUsd * fraction;
            fundingPoints = funding.fundingPoints;
            fundingModel = funding.model;
          } catch (err) {
            fundingError = err instanceof Error ? err.message : String(err);
          }
        }
""",
"""        let fundingUsd: number | null = null;
        let fullPositionFundingUsd: number | null = null;
        let fundingPoints: number | null = null;
        let fundingOraclePointsMatched: number | null = null;
        let fundingCarryUsd: number | null = null;
        let fundingModel: string | null = null;
        let fundingError: string | null = null;
        if (!legacy) {
          try {
            const funding = await fundingForPosition(
              managed,
              fill.receivedAtMs,
              cfg.shadowFundingOracleMaxDelayMs,
            );
            fullPositionFundingUsd = funding.fundingUsd;
            fundingUsd = funding.fundingUsd * fraction;
            fundingPoints = funding.fundingPoints;
            fundingOraclePointsMatched = funding.oraclePointsMatched;
            fundingCarryUsd = funding.carryUsd;
            fundingModel = funding.model;
          } catch (err) {
            fundingError = err instanceof Error ? err.message : String(err);
          }
        }
""",
)

rep(
"""            unresolvedAfterSourceClose: true,
            exposureCheckpoints: [
              ...(managed.exposureCheckpoints ?? []),
              { atMs: fill.receivedAtMs, notionalUsd: Number(managed.entryNotionalExecutedUsd ?? 0) * remainingFraction },
            ],
""",
"""            unresolvedAfterSourceClose: true,
            exposureCheckpoints: [{ atMs: fill.receivedAtMs, size: remainingSize }],
            fundingOracleCheckpoints: [],
            fundingCarryUsd: fullPositionFundingUsd == null
              ? Number(managed.fundingCarryUsd ?? 0)
              : fullPositionFundingUsd * remainingFraction,
            fundingAccruedThroughMs: fill.receivedAtMs,
            fundingIncompleteReason: fullPositionFundingUsd == null
              ? (fundingError ?? managed.fundingIncompleteReason ?? 'funding evidence incomplete at partial close')
              : undefined,
""",
)

rep(
"""          fundingUsd: economics?.fundingUsd ?? fundingUsd,
          fundingPoints, fundingModel,
""",
"""          fundingUsd: economics?.fundingUsd ?? fundingUsd,
          fundingPoints, fundingOraclePointsMatched, fundingCarryUsd, fundingModel,
""",
)

capture_fn = r'''
async function captureFundingOracleCheckpoints(nowMs = Date.now()) {
  if (cfg.live) return;
  const fundingTimeMs = Math.floor(nowMs / FUNDING_INTERVAL_MS) * FUNDING_INTERVAL_MS;
  if (fundingTimeMs === lastFundingOracleBoundaryMs) return;

  const snapshot = state.snapshot();
  const targets = Object.values(snapshot.managed).filter(position => (
    position.paper
    && position.executionEvidenceVersion === EXECUTION_EVIDENCE_VERSION
    && position.openedAtMs < fundingTimeMs
    && !position.fundingIncompleteReason
    && !(position.fundingOracleCheckpoints ?? []).some(point => point.fundingTimeMs === fundingTimeMs)
  ));
  const delayAtStartMs = nowMs - fundingTimeMs;
  if (delayAtStartMs > cfg.shadowFundingOracleMaxDelayMs) {
    lastFundingOracleBoundaryMs = fundingTimeMs;
    for (const position of targets) {
      const latest = state.getManagedBySource(position.sourceBaseId);
      if (!latest || latest.fundingIncompleteReason) continue;
      state.setManaged({
        ...latest,
        fundingIncompleteReason: `missed oracle checkpoint for funding interval ${fundingTimeMs}`,
      });
    }
    if (targets.length) {
      log({
        type: 'funding_oracle_capture_missed',
        fundingTimeMs,
        delayMs: delayAtStartMs,
        maxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
        affectedSourceBaseIds: targets.map(position => position.sourceBaseId),
      });
    }
    return;
  }
  if (!targets.length) {
    lastFundingOracleBoundaryMs = fundingTimeMs;
    return;
  }

  const requestedAtMs = Date.now();
  const oraclePrices = await hl.getOraclePrices();
  const observedAtMs = Date.now();
  const delayMs = observedAtMs - fundingTimeMs;
  lastFundingOracleBoundaryMs = fundingTimeMs;

  for (const position of targets) {
    const latest = state.getManagedBySource(position.sourceBaseId);
    if (!latest || latest.fundingIncompleteReason || latest.openedAtMs >= fundingTimeMs) continue;
    if ((latest.fundingOracleCheckpoints ?? []).some(point => point.fundingTimeMs === fundingTimeMs)) continue;
    const oraclePx = Number(oraclePrices[latest.coin]);
    if (!(oraclePx > 0) || delayMs > cfg.shadowFundingOracleMaxDelayMs) {
      state.setManaged({
        ...latest,
        fundingIncompleteReason: !(oraclePx > 0)
          ? `missing oraclePx for ${latest.coin} at funding interval ${fundingTimeMs}`
          : `oracle checkpoint too late for funding interval ${fundingTimeMs}: ${delayMs}ms`,
      });
      continue;
    }
    state.setManaged({
      ...latest,
      fundingOracleCheckpoints: [
        ...(latest.fundingOracleCheckpoints ?? []),
        { fundingTimeMs, observedAtMs, oraclePx },
      ],
    });
  }
  log({
    type: 'funding_oracle_checkpoint',
    fundingTimeMs,
    requestedAtMs,
    observedAtMs,
    delayMs,
    maxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
    targetCount: targets.length,
  });
}

'''

marker = 'async function fetchAndProcess(source: string, hints: NotificationHints | undefined, receivedAtMs: number, feedFilter = cfg.feedFilter): Promise<number> {'
if text.count(marker) != 1:
    raise SystemExit('fetchAndProcess marker missing or duplicated')
text = text.replace(marker, capture_fn + marker, 1)

rep(
"""    const surface = cfg.discoverySurfaces[discoverySurfaceIndex % cfg.discoverySurfaces.length];
    discoverySurfaceIndex += 1;
    await wake(`api_poll:${surface}`, undefined, Date.now(), surface);
""",
"""    const surface = cfg.discoverySurfaces[discoverySurfaceIndex % cfg.discoverySurfaces.length];
    discoverySurfaceIndex += 1;
    if (!cfg.live) {
      try {
        await captureFundingOracleCheckpoints(Date.now());
      } catch (err) {
        log({ type: 'funding_oracle_capture_error', error: err instanceof Error ? err.message : String(err) });
      }
    }
    await wake(`api_poll:${surface}`, undefined, Date.now(), surface);
""",
)

rep(
"""  await wake('startup_baseline', undefined, Date.now());
  startServer();
""",
"""  if (!cfg.live) {
    try {
      await captureFundingOracleCheckpoints(Date.now());
    } catch (err) {
      log({ type: 'funding_oracle_capture_error', phase: 'startup', error: err instanceof Error ? err.message : String(err) });
    }
  }
  await wake('startup_baseline', undefined, Date.now());
  startServer();
""",
)

rep(
"""    shadowExecutionPolicy: shadowPolicy,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
""",
"""    shadowExecutionPolicy: shadowPolicy,
    fundingOracleMaxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
""",
)

service.write_text(text)

env_text = env_file.read_text()
anchor = 'NOTIFICATION_TRADER_SHADOW_TAKER_FEE_BPS=4.5\n'
if anchor not in env_text:
    raise SystemExit('env funding anchor missing')
env_text = env_text.replace(
    anchor,
    anchor + 'NOTIFICATION_TRADER_SHADOW_FUNDING_ORACLE_MAX_DELAY_MS=10000\n',
    1,
)
env_file.write_text(env_text)

readme_text = readme.read_text()
old = 'funding: Hyperliquid `fundingHistory`, exposure-checkpoint notional; unavailable funding => incomplete economics;'
new = 'funding: Hyperliquid `fundingHistory` rate × prospective `oraclePx` × position size at each hourly funding interval; missing/stale oracle checkpoints => incomplete economics;'
if old in readme_text:
    readme_text = readme_text.replace(old, new, 1)
else:
    # Keep the patch safe if wording changed: append the exact v2 contract near the dry-mode section.
    marker = 'This intentionally reports **gross** copied P&L.'
    if marker not in readme_text:
        raise SystemExit('README funding marker missing')
    readme_text = readme_text.replace(
        marker,
        'Funding evidence is prospective: each hourly funding interval must have a fresh Hyperliquid `oraclePx` checkpoint; cost is position size × oracle price × funding rate. Missing checkpoints make economics incomplete rather than substituting entry/mark prices.\n\n' + marker,
        1,
    )
readme.write_text(readme_text)
