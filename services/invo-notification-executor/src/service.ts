import { appendFileSync, mkdirSync } from 'fs';
import { createServer, IncomingMessage, ServerResponse } from 'http';
import { dirname, resolve } from 'path';
import { randomUUID } from 'crypto';
import { validateEnv, INVO_TOKEN, INVO_REFRESH_TOKEN, HL_AGENT_KEY, resolveWalletAddress } from './env.js';
import * as invo from './invo-client.js';
import * as hl from './hl-client.js';
import { extractNotificationHints, hintsMatchSignal, InvoSignal, NotificationHints, signalFromFeedPost } from './notification-signal.js';
import { ManagedPosition, NotificationState } from './notification-state.js';
import { TraderTracker } from './trader-tracker.js';
import { liveScopeSkipReason } from './live-scope.js';
import { fetchFeedBackfill } from './feed-backfill.js';
import { canProspectivelyRebaseGap, planUnrecoverableGap } from './gap-reconciliation.js';
import {
  INVO_FEED_SURFACES,
  type InvoFeedSurface,
  parseDiscoverySurfaces,
  parseFeedSurface,
  planSurfaceBaseline,
  surfaceNeedsBaseline,
} from './feed-surfaces.js';
import {
  directSourceTimeMs,
  establishClosedBaseline,
  closedBoundaryProof,
  closedSignalsAfterBoundary,
  EliteDirectWatchState,
  loadEliteDirectTargets,
  planClosedHydrations,
  planDirectHydrations,
  runIsolatedHydrations,
  signalsFromDirectInvestments,
  unownedCloseEvidence,
} from './elite-direct-watch.js';
import {
  closeLifecycleKey,
  signalWasSeen,
  sourceEventKey,
} from './source-event-dedupe.js';
import { runSignalBatchBySource, SourceLifecycleQueue } from './source-lifecycle.js';
import { eliteAdmissionFromState, ELITE_ADMISSION_VERSION } from './elite-admission.js';
import { shouldTerminallyDustReconcile } from './close-rejection.js';
import {
  COST_MODEL_VERSION,
  EXECUTION_EVIDENCE_VERSION,
  fetchAssetBook,
  fundingForPosition,
  markShadowPosition,
} from './shadow-market.js';
import {
  closeAction,
  computePositionEconomics,
  isNonExecutableDust,
  openAction,
  simulateL2Fill,
  type ShadowExecutionPolicy,
} from './shadow-execution.js';

if (INVO_TOKEN) invo.setToken(INVO_TOKEN);
if (INVO_REFRESH_TOKEN) invo.setRefreshToken(INVO_REFRESH_TOKEN);

function n(name: string, fallback: number): number {
  const raw = process.env[name];
  const value = raw == null || raw === '' ? fallback : Number(raw);
  if (!Number.isFinite(value)) throw new Error(`Invalid ${name}: ${raw}`);
  return value;
}

function b(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw == null || raw === '') return fallback;
  return ['1', 'true', 'yes', 'on'].includes(raw.toLowerCase());
}

function loadConfig() {
  const allow = (process.env.NOTIFICATION_TRADER_ALLOW ?? '')
    .split(',').map(v => v.trim().replace(/^@/, '').toLowerCase()).filter(Boolean);
  const feedFilter = parseFeedSurface(
    process.env.NOTIFICATION_TRADER_FEED_FILTER ?? 'following',
    'NOTIFICATION_TRADER_FEED_FILTER',
  );
  const discoverySurfaces = parseDiscoverySurfaces(
    process.env.NOTIFICATION_TRADER_DISCOVERY_SURFACES ?? INVO_FEED_SURFACES.join(','),
  );
  return {
    live: b('NOTIFICATION_TRADER_LIVE', false),
    host: process.env.NOTIFICATION_TRADER_HOST ?? '127.0.0.1',
    port: n('NOTIFICATION_TRADER_PORT', 8787),
    pollMs: Math.max(500, n('NOTIFICATION_TRADER_POLL_MS', 1000)),
    // Research requirement: accept canonical Invo signals up to 25 seconds old.
    maxSignalAgeMs: Math.max(1000, n('NOTIFICATION_TRADER_MAX_SIGNAL_AGE_MS', 25_000)),
    feedFilter,
    discoverySurfaces: [...new Set(discoverySurfaces)],
    feedLimit: Math.max(1, Math.min(100, Math.trunc(n('NOTIFICATION_TRADER_FEED_LIMIT', 100)))),
    feedMaxPages: Math.max(1, Math.min(100, Math.trunc(n('NOTIFICATION_TRADER_FEED_MAX_PAGES', 20)))),
    // Opus 2026-09-15 measured transport lag p99=799ms (p50=586, p90=753).
    // A reproducible 1s ceiling covers that measured tail without the prior 5s looseness.
    shadowMaxBookAgeMs: Math.max(50, n('NOTIFICATION_TRADER_SHADOW_MAX_BOOK_AGE_MS', 1000)),
    shadowMaxSpreadBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_MAX_SPREAD_BPS', 50)),
    shadowTakerFeeBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_TAKER_FEE_BPS', 4.5)),
    shadowFundingOracleMaxDelayMs: Math.max(500, n('NOTIFICATION_TRADER_SHADOW_FUNDING_ORACLE_MAX_DELAY_MS', 10_000)),
    shadowMinNotionalUsd: Math.max(1, n('NOTIFICATION_TRADER_SHADOW_MIN_NOTIONAL_USD', 10)),
    marginPct: Math.max(0.01, n('NOTIFICATION_TRADER_MARGIN_PCT', 1)),
    // These are live-account safety controls only. Shadow research is intentionally uncapped.
    maxNotionalUsd: Math.max(10, n('NOTIFICATION_TRADER_MAX_NOTIONAL_USD', 500)),
    maxSlippagePct: Math.max(0.0001, n('NOTIFICATION_TRADER_MAX_SLIPPAGE_PCT', 0.005)),
    maxChaseBps: Math.max(0, n('NOTIFICATION_TRADER_MAX_CHASE_BPS', 25)),
    copyAllFollowed: b('NOTIFICATION_TRADER_COPY_ALL_FOLLOWED', true),
    maxPositions: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MAX_POSITIONS', 5))),
    bridgeToken: process.env.NOTIFICATION_BRIDGE_TOKEN ?? '',
    packageName: process.env.NOTIFICATION_TRADER_PACKAGE_NAME ?? 'com.involio.app',
    allow: new Set(allow),
    statePath: resolve(process.env.NOTIFICATION_TRADER_STATE_PATH ?? 'data/notification-trader-state.json'),
    auditPath: resolve(process.env.NOTIFICATION_TRADER_AUDIT_PATH ?? 'data/notification-trader-audit.jsonl'),
    trackerPath: resolve(process.env.NOTIFICATION_TRADER_TRACKER_PATH ?? 'data/notification-trader-population.json'),
    candidateStatePath: resolve(process.env.NOTIFICATION_TRADER_CANDIDATE_STATE_PATH ?? '/var/lib/hyperliquid-copy-engine/invo-notification-executor/portfolio-candidates.json'),
    candidateSnapshotsPath: resolve(process.env.INVO_PORTFOLIO_CANDIDATE_SNAPSHOTS_PATH ?? '/var/lib/hyperliquid-copy-engine/invo-notification-executor/portfolio-candidate-snapshots.jsonl'),
    candidateStateMaxAgeMs: Math.max(60_000, n('NOTIFICATION_TRADER_CANDIDATE_MAX_AGE_MS', 20 * 60 * 1000)),
    directWatchStatePath: resolve(process.env.NOTIFICATION_TRADER_DIRECT_WATCH_STATE_PATH ?? '/var/lib/hyperliquid-copy-engine/invo-notification-executor/elite-direct-watch.json'),
    directWatchScanMs: Math.max(2_000, n('NOTIFICATION_TRADER_DIRECT_WATCH_SCAN_MS', 3_000)),
    directWatchMaxHydratesPerScan: Math.max(1, Math.min(20, Math.trunc(n('NOTIFICATION_TRADER_DIRECT_WATCH_MAX_HYDRATES_PER_SCAN', 8)))),
    directWatchFallbackPollMs: Math.max(10_000, n('NOTIFICATION_TRADER_DIRECT_WATCH_FALLBACK_POLL_MS', 18_000)),
    directWatchClosedPollMs: Math.max(30_000, n('NOTIFICATION_TRADER_DIRECT_WATCH_CLOSED_POLL_MS', 60_000)),
    directWatchMaxClosedHydratesPerScan: Math.max(1, Math.min(10, Math.trunc(n('NOTIFICATION_TRADER_DIRECT_WATCH_MAX_CLOSED_HYDRATES_PER_SCAN', 3)))),
    directWatchClosedMaxPages: Math.max(1, Math.min(3, Math.trunc(n('NOTIFICATION_TRADER_DIRECT_WATCH_CLOSED_MAX_PAGES', 2)))),
    minEvidenceEvents: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MIN_EVIDENCE_EVENTS', 20))),
    minObservationDays: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MIN_OBSERVATION_DAYS', 7))),
    staleAfterMs: Math.max(60_000, n('NOTIFICATION_TRADER_STALE_AFTER_MS', 3 * 24 * 60 * 60 * 1000)),
    inactiveAfterMs: Math.max(60_000, n('NOTIFICATION_TRADER_INACTIVE_AFTER_MS', 14 * 24 * 60 * 60 * 1000)),
  };
}

const cfg = loadConfig();
const shadowPolicy: ShadowExecutionPolicy = {
  maxBookAgeMs: cfg.shadowMaxBookAgeMs,
  maxSpreadBps: cfg.shadowMaxSpreadBps,
  minNotionalUsd: cfg.shadowMinNotionalUsd,
  takerFeeBps: cfg.shadowTakerFeeBps,
  fundingOracleMaxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
};
if (cfg.live && (process.env.REAL_TRADING_ENABLED ?? 'NO').trim().toUpperCase() !== 'YES') {
  throw new Error('NOTIFICATION_TRADER_LIVE=true requires REAL_TRADING_ENABLED=YES');
}
if (cfg.live && cfg.allow.size === 0 && !cfg.copyAllFollowed) {
  throw new Error('Live mode requires NOTIFICATION_TRADER_ALLOW or explicit NOTIFICATION_TRADER_COPY_ALL_FOLLOWED=true');
}
validateEnv(cfg.live);
const WALLET_ADDRESS = resolveWalletAddress();
const state = new NotificationState(cfg.statePath);
if (cfg.inactiveAfterMs <= cfg.staleAfterMs) throw new Error('NOTIFICATION_TRADER_INACTIVE_AFTER_MS must exceed NOTIFICATION_TRADER_STALE_AFTER_MS');
const tracker = new TraderTracker(cfg.trackerPath, {
  minEvents: cfg.minEvidenceEvents,
  minObservationDays: cfg.minObservationDays,
  staleAfterMs: cfg.staleAfterMs,
  inactiveAfterMs: cfg.inactiveAfterMs,
});
const directWatch = new EliteDirectWatchState(cfg.directWatchStatePath);
const inFlight = new Set<string>();
const inFlightSourceEvents = new Set<string>();
const sourceLifecycleQueue = new SourceLifecycleQueue();
let hydrating = false;
let pendingWake: { source: string; hints?: NotificationHints; receivedAtMs: number; feedFilter?: InvoFeedSurface } | null = null;
let lastSuccessPollMs = 0;
let backoffMs = 0;
let discoverySurfaceIndex = 0;
let lastFundingOracleBoundaryMs = -1;
let lastDirectWatchScanMs = 0;
let directWatchBackoffMs = 0;
let directWatchBackoffUntilMs = 0;
let directWatchSelectorIndex = 0;
const directWatchMetrics = {
  selectorRequests: 0, hydrationRequests: 0, hydrationCount: 0,
  closedHydrationCount: 0, closedBaselineCount: 0, closedOverflowRiskCount: 0,
  signalsObserved: 0, signalsHandled: 0, http429s: 0,
  targetErrors: 0, selectorErrors: 0, skippedAfterRateLimit: 0,
  lastScanAtMs: 0, lastSuccessAtMs: 0,
};
const FUNDING_INTERVAL_MS = 60 * 60 * 1000;
const SOURCE_CLOSE_RETRY_BASE_MS = 250;
const SOURCE_CLOSE_RETRY_MAX_MS = 30_000;
const HEALTH_MTM_CONCURRENCY = 4;

function closeRetryDelayMs(attempt: number) {
  return Math.min(SOURCE_CLOSE_RETRY_MAX_MS, SOURCE_CLOSE_RETRY_BASE_MS * (2 ** Math.min(7, Math.max(0, attempt - 1))));
}

async function mapConcurrent<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  async function worker() {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await fn(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

function log(event: Record<string, unknown>) {
  const row = { ts: new Date().toISOString(), ...event };
  console.log(JSON.stringify(row));
  mkdirSync(dirname(cfg.auditPath), { recursive: true });
  appendFileSync(cfg.auditPath, `${JSON.stringify(row)}\n`);
  const signal = event.signal as InvoSignal | undefined;
  if (signal && (event.type === 'skip' || event.type === 'execution_error')) {
    tracker.recordFailure(`invo-user:${signal.ownerId}`, String(event.reason ?? event.type));
  }
}

function detectionLatencyMs(signal: InvoSignal, receivedAtMs: number): number | null {
  return signal.sourceTimeMs == null ? null : receivedAtMs - signal.sourceTimeMs;
}

function closeFreshness(signal: InvoSignal, receivedAtMs: number) {
  const latencyMs = detectionLatencyMs(signal, receivedAtMs);
  return {
    closeFreshnessStatus: latencyMs == null ? 'unknown' : latencyMs > cfg.maxSignalAgeMs ? 'stale' : 'fresh',
    closeLatencyMs: latencyMs,
    closeSourceTimeField: signal.sourceTimeField,
  };
}

function chaseBps(signal: InvoSignal, mid: number): number | null {
  if (!signal.entryPrice || signal.entryPrice <= 0) return null;
  return signal.side === 'long'
    ? ((mid - signal.entryPrice) / signal.entryPrice) * 10_000
    : ((signal.entryPrice - mid) / signal.entryPrice) * 10_000;
}

function roundSize(raw: number, decimals: number): string {
  const factor = 10 ** decimals;
  const rounded = Math.floor(raw * factor) / factor;
  if (!(rounded > 0)) throw new Error(`Rounded size is zero: ${raw} @ ${decimals} decimals`);
  return rounded.toFixed(decimals).replace(/\.?0+$/, '');
}

function positive(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null;
}

function validateSourceLeverage(signal: InvoSignal, asset: any): number | null {
  const leverage = Math.max(1, Math.trunc(signal.leverage));
  const assetMax = Number(asset?.maxLeverage);
  if (Number.isFinite(assetMax) && assetMax > 0 && leverage > assetMax) return null;
  return leverage;
}

function baseShadowSlice(equity: number, mid: number, leverage: number) {
  const marginUsd = equity * (cfg.marginPct / 100);
  const notionalUsd = marginUsd * leverage;
  const size = notionalUsd / mid;
  if (!(marginUsd > 0) || !(notionalUsd > 0) || !(size > 0)) {
    throw new Error(`Invalid shadow sizing equity=${equity} mid=${mid} leverage=${leverage}`);
  }
  return { marginUsd, notionalUsd, size };
}

function shadowReupSize(managed: ManagedPosition, signal: InvoSignal, fallbackSize: number) {
  const sourceIncrementSize = positive(signal.entrySize);
  const priorSourceSize = positive(managed.sourceSize);
  const priorCopySize = positive(managed.size);
  if (sourceIncrementSize && priorSourceSize && priorCopySize) {
    const copyPerSourceUnit = priorCopySize / priorSourceSize;
    return {
      addSize: sourceIncrementSize * copyPerSourceUnit,
      sourceIncrementSize,
      sizingModel: 'relative_source_increment',
      copyPerSourceUnit,
    };
  }
  return {
    addSize: fallbackSize,
    sourceIncrementSize,
    sizingModel: 'fallback_equal_shadow_slice',
    copyPerSourceUnit: null,
  };
}

async function marketSnapshot(signal: InvoSignal) {
  const [meta, mids] = await Promise.all([hl.getMeta(), hl.getAllMids()]);
  const assetIndex = meta.universe.findIndex((a: any) => a.name === signal.coin);
  if (assetIndex < 0) return null;
  const asset = meta.universe[assetIndex];
  const mid = Number(mids[signal.coin]);
  if (!(mid > 0)) throw new Error(`No mid for ${signal.coin}`);
  return { meta, mids, assetIndex, asset, mid };
}

async function shadowOpen(
  signal: InvoSignal,
  wakeSource: string,
  receivedAtMs: number,
  openedFromIncrease: boolean,
  decisionAtMs: number,
) {
  const assetBook = await fetchAssetBook(signal.coin);
  if (!assetBook) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'unknown_hl_asset', signal, wakeSource });
    return;
  }
  const leverage = validateSourceLeverage(signal, assetBook.asset);
  if (leverage == null) {
    state.markSeen(signal.key);
    log({
      type: 'skip', reason: 'source_leverage_unexecutable_on_hl',
      sourceLeverage: signal.leverage, assetMaxLeverage: assetBook.asset.maxLeverage,
      signal, wakeSource,
    });
    return;
  }
  const bestBid = assetBook.book?.bids[0]?.px;
  const bestAsk = assetBook.book?.asks[0]?.px;
  const mid = bestBid && bestAsk ? (bestBid + bestAsk) / 2 : 0;
  if (!(mid > 0)) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'shadow_missing_book', signal, wakeSource, decisionAtMs });
    return;
  }

  const equity = await hl.getAccountEquity(WALLET_ADDRESS);
  const sizing = baseShadowSlice(equity, mid, leverage);
  const result = simulateL2Fill(
    assetBook.book,
    openAction(signal.side),
    sizing.size,
    assetBook.asset.szDecimals,
    shadowPolicy,
  );
  if (!result.ok) {
    state.markSeen(signal.key);
    log({
      type: 'skip', reason: `shadow_${result.reason}`, detail: result.detail ?? null,
      signal, wakeSource, decisionAtMs, bookRequestedAtMs: assetBook.requestedAtMs,
      bookReceivedAtMs: assetBook.receivedAtMs,
    });
    return;
  }

  const fill = result.fill;
  const sourceSize = positive(signal.entrySize) ?? undefined;
  const marginUsd = fill.notionalUsd / leverage;
  state.setManaged({
    coin: signal.coin,
    sourceBaseId: signal.sourceBaseId,
    sourceBaseShortId: signal.sourceBaseShortId,
    sourcePostId: signal.postId,
    username: signal.username,
    ownerId: signal.ownerId,
    portfolioId: signal.portfolioId,
    side: signal.side,
    openedAtMs: fill.receivedAtMs,
    paper: true,
    entryMid: fill.midPx,
    entryPrice: fill.avgPx,
    entryBookMid: fill.midPx,
    entryBookTimeMs: fill.bookTimeMs,
    entryBookReceivedAtMs: fill.receivedAtMs,
    entryBookAgeMs: fill.bookAgeMs,
    entrySpreadBps: fill.spreadBps,
    entrySlippageBps: fill.slippageBps,
    entrySlippageUsd: fill.slippageUsd,
    entryFeeUsd: fill.feeUsd,
    entryNotionalExecutedUsd: fill.notionalUsd,
    notionalUsd: fill.notionalUsd,
    marginUsd,
    leverage,
    size: fill.filledSize,
    sourceSize,
    addCount: 0,
    estimatedOpenCostUsd: fill.feeUsd + fill.slippageUsd,
    unfilledOpenSize: fill.unfilledSize,
    exposureCheckpoints: [{ atMs: fill.receivedAtMs, size: fill.filledSize }],
    fundingOracleCheckpoints: [],
    fundingCarryUsd: 0,
    fundingAccruedThroughMs: fill.receivedAtMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
  });
  state.markSeen(signal.key);
  log({
    type: openedFromIncrease ? 'shadow_opened_from_increase' : 'shadow_opened',
    signal, wakeSource, decisionAtMs, equity, leverage, marginUsd,
    sourceSize, requestedSize: fill.requestedRoundedSize, filledSize: fill.filledSize,
    unfilledSize: fill.unfilledSize, partialFill: fill.partial,
    entryPrice: fill.avgPx, entryBookMid: fill.midPx, entryNotionalUsd: fill.notionalUsd,
    entryFeeUsd: fill.feeUsd, entrySlippageUsd: fill.slippageUsd,
    entrySlippageBps: fill.slippageBps, spreadBps: fill.spreadBps,
    bookTimeMs: fill.bookTimeMs, bookRequestedAtMs: fill.requestedAtMs,
    bookReceivedAtMs: fill.receivedAtMs, bookAgeMs: fill.bookAgeMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
    fundingModel: 'Hyperliquid fundingHistory rate x prospective oraclePx x position size; missing oracle fails closed',
    detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
    decisionLatencyMs: fill.requestedAtMs - receivedAtMs,
    marketDataLatencyMs: fill.receivedAtMs - fill.requestedAtMs,
  });
}

async function shadowReup(
  managed: ManagedPosition,
  signal: InvoSignal,
  wakeSource: string,
  receivedAtMs: number,
  decisionAtMs: number,
) {
  if (managed.side !== signal.side || managed.coin !== signal.coin) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'source_reup_direction_or_coin_mismatch', managed, signal, wakeSource });
    return;
  }
  if (managed.executionEvidenceVersion !== EXECUTION_EVIDENCE_VERSION || !(Number(managed.entryPrice) > 0)) {
    state.markSeen(signal.key);
    log({
      type: 'skip', reason: 'shadow_legacy_position_reup_incomplete',
      economicsCompleteness: 'INCOMPLETE_LEGACY_ENTRY', managed, signal, wakeSource,
    });
    return;
  }

  const assetBook = await fetchAssetBook(signal.coin);
  if (!assetBook) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'unknown_hl_asset', signal, wakeSource });
    return;
  }
  const leverage = validateSourceLeverage(signal, assetBook.asset);
  if (leverage == null) {
    state.markSeen(signal.key);
    log({
      type: 'skip', reason: 'source_leverage_unexecutable_on_hl',
      sourceLeverage: signal.leverage, assetMaxLeverage: assetBook.asset.maxLeverage,
      signal, wakeSource,
    });
    return;
  }
  const bestBid = assetBook.book?.bids[0]?.px;
  const bestAsk = assetBook.book?.asks[0]?.px;
  const mid = bestBid && bestAsk ? (bestBid + bestAsk) / 2 : 0;
  if (!(mid > 0)) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'shadow_missing_book', signal, wakeSource, decisionAtMs });
    return;
  }

  const equity = await hl.getAccountEquity(WALLET_ADDRESS);
  const fallback = baseShadowSlice(equity, mid, leverage);
  const reup = shadowReupSize(managed, signal, fallback.size);
  const result = simulateL2Fill(
    assetBook.book,
    openAction(signal.side),
    reup.addSize,
    assetBook.asset.szDecimals,
    shadowPolicy,
  );
  if (!result.ok) {
    state.markSeen(signal.key);
    log({
      type: 'skip', reason: `shadow_${result.reason}`, detail: result.detail ?? null,
      signal, wakeSource, decisionAtMs,
    });
    return;
  }

  const fill = result.fill;
  const priorSize = positive(managed.size) ?? 0;
  const priorEntry = positive(managed.entryPrice) ?? 0;
  if (!(priorSize > 0) || !(priorEntry > 0)) throw new Error('Invalid shadow re-up basis');
  const newSize = priorSize + fill.filledSize;
  const newEntryPrice = ((priorEntry * priorSize) + (fill.avgPx * fill.filledSize)) / newSize;
  const newSourceSize = reup.sourceIncrementSize
    ? (positive(managed.sourceSize) ?? 0) + reup.sourceIncrementSize
    : managed.sourceSize;
  const newEntryNotional = Number(managed.entryNotionalExecutedUsd ?? priorEntry * priorSize) + fill.notionalUsd;
  const newMargin = Number(managed.marginUsd ?? 0) + fill.notionalUsd / leverage;
  const checkpoints = [
    ...(managed.exposureCheckpoints ?? []),
    { atMs: fill.receivedAtMs, size: newSize },
  ];

  state.setManaged({
    ...managed,
    sourcePostId: signal.postId,
    sourceBaseShortId: signal.sourceBaseShortId || managed.sourceBaseShortId,
    leverage,
    entryMid: newEntryPrice,
    entryPrice: newEntryPrice,
    entryBookMid: fill.midPx,
    entryBookTimeMs: fill.bookTimeMs,
    entryBookReceivedAtMs: fill.receivedAtMs,
    entryBookAgeMs: fill.bookAgeMs,
    entrySpreadBps: fill.spreadBps,
    entrySlippageBps: fill.slippageBps,
    entrySlippageUsd: Number(managed.entrySlippageUsd ?? 0) + fill.slippageUsd,
    entryFeeUsd: Number(managed.entryFeeUsd ?? 0) + fill.feeUsd,
    entryNotionalExecutedUsd: newEntryNotional,
    size: newSize,
    sourceSize: newSourceSize,
    notionalUsd: newEntryNotional,
    marginUsd: newMargin,
    addCount: (managed.addCount ?? 0) + 1,
    unfilledOpenSize: Number(managed.unfilledOpenSize ?? 0) + fill.unfilledSize,
    exposureCheckpoints: checkpoints,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
  });
  state.markSeen(signal.key);
  log({
    type: 'shadow_reupped', username: managed.username ?? signal.username,
    coin: signal.coin, side: signal.side, sourceBaseId: signal.sourceBaseId,
    leverage, priorSize, requestedAddSize: fill.requestedRoundedSize,
    addedSize: fill.filledSize, unfilledAddSize: fill.unfilledSize,
    partialFill: fill.partial, newSize, priorEntryPrice: priorEntry,
    addPrice: fill.avgPx, newEntryPrice, addNotionalUsd: fill.notionalUsd,
    addMarginUsd: fill.notionalUsd / leverage, entryFeeAddedUsd: fill.feeUsd,
    slippageAddedUsd: fill.slippageUsd, sourceIncrementSize: reup.sourceIncrementSize,
    priorSourceSize: managed.sourceSize, newSourceSize, sizingModel: reup.sizingModel,
    copyPerSourceUnit: reup.copyPerSourceUnit, signal, wakeSource, decisionAtMs,
    spreadBps: fill.spreadBps, slippageBps: fill.slippageBps,
    bookTimeMs: fill.bookTimeMs, bookRequestedAtMs: fill.requestedAtMs,
    bookReceivedAtMs: fill.receivedAtMs, bookAgeMs: fill.bookAgeMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
    detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
    decisionLatencyMs: fill.requestedAtMs - receivedAtMs,
    marketDataLatencyMs: fill.receivedAtMs - fill.requestedAtMs,
  });
}

async function executeUnlocked(signal: InvoSignal, wakeSource: string, receivedAtMs: number, feedFilter: string) {
  const eventKey = sourceEventKey(signal);
  const lifecycleKey = closeLifecycleKey(signal);
  const inFlightKey = eventKey ?? signal.key;
  if (
    signalWasSeen(signal, key => state.hasSeen(key))
    || inFlight.has(signal.key)
    || inFlightSourceEvents.has(inFlightKey)
  ) return;
  inFlight.add(signal.key);
  inFlightSourceEvents.add(inFlightKey);
  const decisionAtMs = Date.now();

  try {
    if (signal.action !== 'close') state.markObservedOpen(signal.sourceBaseId);
    // Discovery remains broad in the separate portfolio-research collector, but NEW
    // Lane 3 shadow exposure is portfolio-level elite-only. Closes bypass this gate so
    // previously owned broad-research exposure can always unwind after a demotion.
    if (!cfg.live && signal.action !== 'close') {
      const eligibilityCutoffMs = signal.sourceTimeMs ?? receivedAtMs;
      const candidateAdmission = eliteAdmissionFromState(
        cfg.candidateStatePath,
        signal.portfolioId,
        eligibilityCutoffMs,
        cfg.candidateStateMaxAgeMs,
        cfg.candidateSnapshotsPath,
      );
      if (!candidateAdmission.allowed) {
        state.markSeen(signal.key);
        log({
          type: 'skip',
          reason: `shadow_${candidateAdmission.reason}`,
          shadowAdmissionMode: 'ELITE_ONLY',
          eligibilityCutoffMs,
          candidateAdmission,
          signal,
          wakeSource,
        });
        return;
      }
      log({
        type: 'shadow_portfolio_admitted',
        shadowAdmissionMode: 'ELITE_ONLY',
        eligibilityCutoffMs,
        candidateAdmission,
        signal,
        wakeSource,
      });
    }

    const liveScopeReason = cfg.live
      ? liveScopeSkipReason(signal, feedFilter, cfg.allow, cfg.copyAllFollowed)
      : null;
    if (liveScopeReason) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: liveScopeReason, feedFilter, signal, wakeSource });
      return;
    }

    const ageMs = signal.sourceTimeMs == null ? null : decisionAtMs - signal.sourceTimeMs;
    if (signal.action !== 'close' && ageMs != null && ageMs > cfg.maxSignalAgeMs) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'stale_signal_over_25s_window', ageMs, maxSignalAgeMs: cfg.maxSignalAgeMs, signal, wakeSource });
      return;
    }

    if (signal.action === 'close') {
      const managed = state.getManagedBySource(signal.sourceBaseId);
      if (!managed) {
        state.markSeen(signal.key);
        const observedOpen = state.hasObservedOpen(signal.sourceBaseId);
        log({
          ...unownedCloseEvidence(signal, observedOpen),
          managed: null, signal, wakeSource, ...closeFreshness(signal, receivedAtMs),
        });
        return;
      }

      if (!cfg.live) {
        if (managed.sourceCloseNextRetryAtMs && decisionAtMs < managed.sourceCloseNextRetryAtMs) return;
        const size = Number(managed.size);
        const assetBook = await fetchAssetBook(signal.coin);
        if (!assetBook || !(size > 0)) {
          const reason = !assetBook ? 'missing_book' : 'invalid_managed_size';
          const attempt = (managed.sourceCloseRetryAttempts ?? 0) + 1;
          state.setManaged({
            ...managed,
            unresolvedAfterSourceClose: true,
            sourceCloseRetryAttempts: attempt,
            sourceCloseNextRetryAtMs: decisionAtMs + closeRetryDelayMs(attempt),
            sourceCloseLastReason: reason,
            pendingSourceClose: signal,
          });
          log({
            type: 'shadow_close_incomplete', reason,
            economicsCompleteness: 'UNRESOLVED_EXPOSURE', managed, signal, wakeSource,
            retryAttempt: attempt,
            retryInMs: closeRetryDelayMs(attempt),
            ...closeFreshness(signal, receivedAtMs),
          });
          return;
        }
        const result = simulateL2Fill(
          assetBook.book,
          closeAction(managed.side),
          size,
          assetBook.asset.szDecimals,
          shadowPolicy,
        );
        if (!result.ok) {
          const bestBid = assetBook.book?.bids[0]?.px ?? null;
          const bestAsk = assetBook.book?.asks[0]?.px ?? null;
          const midFromBook = bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : null;
          const dustRejected = shouldTerminallyDustReconcile(
            result.reason,
            size,
            assetBook.asset.szDecimals,
            midFromBook,
            shadowPolicy.minNotionalUsd,
          );
          if (dustRejected) {
            state.clearManagedBySource(signal.sourceBaseId);
            state.markSeen(signal.key);
            log({
              type: 'shadow_close_dust_reconciled', reason: result.reason, detail: result.detail ?? null,
              economicsCompleteness: 'INCOMPLETE_DUST_RECONCILIATION', managed, signal, wakeSource,
              dustReconciledSize: size,
              dustEstimatedNotionalUsd: midFromBook == null ? null : size * midFromBook,
              unresolvedSize: 0, decisionAtMs,
              bookRequestedAtMs: assetBook.requestedAtMs, bookReceivedAtMs: assetBook.receivedAtMs,
              ...closeFreshness(signal, receivedAtMs),
            });
            return;
          }
          const retryable = true;
          const attempt = (managed.sourceCloseRetryAttempts ?? 0) + 1;
          state.setManaged({
            ...managed,
            unresolvedAfterSourceClose: true,
            sourceCloseRetryAttempts: attempt,
            sourceCloseNextRetryAtMs: retryable ? decisionAtMs + closeRetryDelayMs(attempt) : undefined,
            sourceCloseLastReason: result.reason,
            pendingSourceClose: signal,
          });
          log({
            type: 'shadow_close_rejected', reason: result.reason, detail: result.detail ?? null,
            economicsCompleteness: 'UNRESOLVED_EXPOSURE', managed, signal, wakeSource,
            decisionAtMs, bookRequestedAtMs: assetBook.requestedAtMs,
            bookReceivedAtMs: assetBook.receivedAtMs,
            retryable, retryAttempt: attempt,
            retryInMs: retryable ? closeRetryDelayMs(attempt) : null,
            ...closeFreshness(signal, receivedAtMs),
          });
          return;
        }

        const fill = result.fill;
        const legacy = managed.executionEvidenceVersion !== EXECUTION_EVIDENCE_VERSION
          || !(Number(managed.entryPrice) > 0)
          || !(Number(managed.entryNotionalExecutedUsd) > 0)
          || !Array.isArray(managed.exposureCheckpoints)
          || !Array.isArray(managed.fundingOracleCheckpoints);
        const fraction = Math.min(1, fill.filledSize / size);
        let fundingUsd: number | null = null;
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

        let economics = null;
        if (!legacy && fundingUsd != null) {
          economics = computePositionEconomics({
            side: managed.side,
            entryAvgPx: Number(managed.entryPrice),
            size: fill.filledSize,
            entryFeeUsd: Number(managed.entryFeeUsd ?? 0) * fraction,
            entryNotionalUsd: Number(managed.entryNotionalExecutedUsd) * fraction,
            exitFill: fill,
            fundingUsd,
          });
        }

        const remainingSize = Math.max(0, size - fill.filledSize);
        const dustReconciled = remainingSize > 1e-12 && isNonExecutableDust(
          remainingSize, assetBook.asset.szDecimals, fill.midPx, shadowPolicy.minNotionalUsd,
        );
        const unresolvedSize = dustReconciled ? 0 : remainingSize;
        if (unresolvedSize > 1e-12) {
          const remainingFraction = unresolvedSize / size;
          state.setManaged({
            ...managed,
            size: unresolvedSize,
            entryFeeUsd: Number(managed.entryFeeUsd ?? 0) * remainingFraction,
            entrySlippageUsd: Number(managed.entrySlippageUsd ?? 0) * remainingFraction,
            entryNotionalExecutedUsd: Number(managed.entryNotionalExecutedUsd ?? 0) * remainingFraction,
            notionalUsd: Number(managed.notionalUsd ?? 0) * remainingFraction,
            marginUsd: Number(managed.marginUsd ?? 0) * remainingFraction,
            unresolvedAfterSourceClose: true,
            sourceCloseRetryAttempts: (managed.sourceCloseRetryAttempts ?? 0) + 1,
            sourceCloseNextRetryAtMs: fill.receivedAtMs + closeRetryDelayMs((managed.sourceCloseRetryAttempts ?? 0) + 1),
            sourceCloseLastReason: 'partial_depth',
            pendingSourceClose: signal,
            exposureCheckpoints: [{ atMs: fill.receivedAtMs, size: unresolvedSize }],
            fundingOracleCheckpoints: [],
            fundingCarryUsd: fullPositionFundingUsd == null
              ? Number(managed.fundingCarryUsd ?? 0)
              : fullPositionFundingUsd * remainingFraction,
            fundingAccruedThroughMs: fill.receivedAtMs,
            fundingIncompleteReason: fullPositionFundingUsd == null
              ? (fundingError ?? managed.fundingIncompleteReason ?? 'funding evidence incomplete at partial close')
              : undefined,
          });
        } else {
          state.clearManagedBySource(signal.sourceBaseId);
        }
        if (unresolvedSize <= 1e-12) state.markSeen(signal.key);
        log({
          type: dustReconciled ? 'shadow_close_dust_reconciled' : unresolvedSize > 1e-12 ? 'shadow_partially_closed' : 'shadow_closed',
          economicsCompleteness: dustReconciled
            ? 'INCOMPLETE_DUST_RECONCILIATION'
            : legacy
            ? 'INCOMPLETE_LEGACY_ENTRY'
            : fundingUsd == null ? 'INCOMPLETE_FUNDING' : 'COMPLETE_EXECUTION_REALISTIC',
          fundingError,
          username: managed.username ?? signal.username,
          coin: signal.coin, side: managed.side, sourceBaseId: managed.sourceBaseId,
          entryPrice: managed.entryPrice ?? null, entryBookMid: managed.entryBookMid ?? null,
          exitPrice: fill.avgPx, exitBookMid: fill.midPx,
          sourceClosingPrice: signal.closingPrice, requestedCloseSize: fill.requestedRoundedSize,
          closedSize: fill.filledSize, unresolvedSize,
          dustReconciledSize: dustReconciled ? remainingSize : 0,
          dustEstimatedNotionalUsd: dustReconciled ? remainingSize * fill.midPx : null,
          partialFill: fill.partial, sourceSize: managed.sourceSize,
          addCount: managed.addCount ?? 0, leverage: managed.leverage,
          grossPnlUsd: economics?.grossPnlUsd ?? null,
          grossReturnBps: economics?.grossReturnBps ?? null,
          entryFeeUsd: economics?.entryFeeUsd ?? null,
          exitFeeUsd: economics?.exitFeeUsd ?? fill.feeUsd,
          fundingUsd: economics?.fundingUsd ?? fundingUsd,
          fundingPoints, fundingOraclePointsMatched, fundingCarryUsd, fundingModel,
          totalExplicitCostUsd: economics?.totalExplicitCostUsd ?? null,
          netPnlUsd: economics?.netPnlUsd ?? null,
          netReturnBps: economics?.netReturnBps ?? null,
          exitSlippageUsd: fill.slippageUsd, exitSlippageBps: fill.slippageBps,
          spreadBps: fill.spreadBps, heldMs: fill.receivedAtMs - managed.openedAtMs,
          bookTimeMs: fill.bookTimeMs, bookRequestedAtMs: fill.requestedAtMs,
          bookReceivedAtMs: fill.receivedAtMs, bookAgeMs: fill.bookAgeMs,
          executionEvidenceVersion: managed.executionEvidenceVersion ?? null,
          costModelVersion: managed.costModelVersion ?? null,
          signal, wakeSource, decisionAtMs,
          detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
          decisionLatencyMs: fill.requestedAtMs - receivedAtMs,
          marketDataLatencyMs: fill.receivedAtMs - fill.requestedAtMs,
          ...closeFreshness(signal, receivedAtMs),
        });
        return;
      }

      const sameCoinManaged = state.getManagedForCoin(signal.coin);
      if (sameCoinManaged.some(p => p.sourceBaseId !== signal.sourceBaseId)) {
        throw new Error(`Live close cannot isolate ${signal.sourceBaseId}; ${signal.coin} has multiple managed source positions`);
      }
      const before = await hl.getPositions(WALLET_ADDRESS);
      const pos = before.find((p: any) => p.coin === signal.coin);
      if (!pos) {
        state.clearManagedBySource(signal.sourceBaseId);
        state.markSeen(signal.key);
        log({ type: 'close_already_flat', signal, wakeSource, ...closeFreshness(signal, receivedAtMs) });
        return;
      }

      const orderAtMs = Date.now();
      const result = await hl.closePosition(signal.coin, WALLET_ADDRESS, cfg.maxSlippagePct);
      const after = await hl.getPositions(WALLET_ADDRESS);
      const remaining = after.find((p: any) => p.coin === signal.coin);
      if (remaining && Math.abs(Number(remaining.szi)) > 0) {
        throw new Error(`Close verification failed; remaining ${signal.coin} size=${remaining.szi}`);
      }

      let invoResult: unknown = null;
      if (managed.localBaseShortId) {
        const meta = await hl.getMeta();
        const assetIndex = meta.universe.findIndex((a: any) => a.name === signal.coin);
        if (assetIndex >= 0) {
          try {
            invoResult = await invo.recordClose({
              clientTxId: randomUUID(),
              baseShortId: managed.localBaseShortId,
              assetIndex,
              submission: { hlOrder: result, nonceMs: orderAtMs, hlResponse: result },
              summary: { qtyBefore: String(pos.szi), qtyAfter: '0' },
            });
          } catch (err) {
            invoResult = { error: err instanceof Error ? err.message : String(err) };
          }
        }
      }

      state.clearManagedBySource(signal.sourceBaseId);
      state.markSeen(signal.key);
      log({
        type: 'closed', signal, wakeSource, result, invoResult,
        detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
        decisionLatencyMs: decisionAtMs - receivedAtMs,
        executionLatencyMs: Date.now() - orderAtMs,
        ...closeFreshness(signal, receivedAtMs),
      });
      return;
    }

    const existingManaged = state.getManagedBySource(signal.sourceBaseId);
    if (signal.action === 'increase' && existingManaged) {
      if (!cfg.live) {
        await shadowReup(existingManaged, signal, wakeSource, receivedAtMs, decisionAtMs);
        return;
      }

      const snap = await marketSnapshot(signal);
      if (!snap) {
        state.markSeen(signal.key);
        log({ type: 'skip', reason: 'unknown_hl_asset', signal, wakeSource });
        return;
      }
      const leverage = validateSourceLeverage(signal, snap.asset);
      if (leverage == null) {
        state.markSeen(signal.key);
        log({ type: 'skip', reason: 'source_leverage_unexecutable_on_hl', sourceLeverage: signal.leverage, assetMaxLeverage: snap.asset.maxLeverage, signal, wakeSource });
        return;
      }
      const chase = chaseBps(signal, snap.mid);
      if (chase != null && chase > cfg.maxChaseBps) {
        state.markSeen(signal.key);
        log({ type: 'skip', reason: 'entry_chased_too_far_live_only', chaseBps: chase, mid: snap.mid, signal, wakeSource });
        return;
      }
      const before = await hl.getPositions(WALLET_ADDRESS);
      const beforePos = before.find((p: any) => p.coin === signal.coin);
      if (!beforePos || Math.sign(Number(beforePos.szi)) !== (signal.side === 'long' ? 1 : -1)) {
        throw new Error(`Live re-up position mismatch for ${signal.coin}`);
      }
      const equity = await hl.getAccountEquity(WALLET_ADDRESS);
      const fallbackNotional = Math.min(equity * (cfg.marginPct / 100) * leverage, cfg.maxNotionalUsd);
      const fallbackSize = fallbackNotional / snap.mid;
      const reup = shadowReupSize(existingManaged, signal, fallbackSize);
      const size = roundSize(reup.addSize, snap.asset.szDecimals);
      await hl.setLeverage(signal.coin, leverage);
      const orderAtMs = Date.now();
      const result = await hl.placeMarketOrder(signal.coin, signal.side === 'long', size, cfg.maxSlippagePct);
      const after = await hl.getPositions(WALLET_ADDRESS);
      const afterPos = after.find((p: any) => p.coin === signal.coin);
      const beforeQty = Number(beforePos.szi);
      const afterQty = Number(afterPos?.szi ?? 0);
      const deltaQty = afterQty - beforeQty;
      const expectedSign = signal.side === 'long' ? 1 : -1;
      if (!Number.isFinite(deltaQty) || deltaQty === 0 || Math.sign(deltaQty) !== expectedSign || Math.sign(afterQty) !== expectedSign) {
        throw new Error(`Re-up verification failed; before=${beforeQty} after=${afterQty}`);
      }
      const addSize = Math.abs(deltaQty);
      const priorSize = positive(existingManaged.size) ?? Math.abs(beforeQty);
      const priorEntry = positive(existingManaged.entryMid) ?? snap.mid;
      const newSize = priorSize + addSize;
      const newEntryMid = ((priorEntry * priorSize) + (snap.mid * addSize)) / newSize;
      state.setManaged({
        ...existingManaged,
        sourcePostId: signal.postId,
        sourceBaseShortId: signal.sourceBaseShortId || existingManaged.sourceBaseShortId,
        leverage,
        entryMid: newEntryMid,
        size: newSize,
        sourceSize: reup.sourceIncrementSize ? (positive(existingManaged.sourceSize) ?? 0) + reup.sourceIncrementSize : existingManaged.sourceSize,
        notionalUsd: (existingManaged.notionalUsd ?? priorEntry * priorSize) + snap.mid * addSize,
        marginUsd: (existingManaged.marginUsd ?? 0) + (snap.mid * addSize) / leverage,
        addCount: (existingManaged.addCount ?? 0) + 1,
      });
      state.markSeen(signal.key);
      log({
        type: 'reupped', signal, wakeSource, result, leverage, size, sizingModel: reup.sizingModel,
        detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
        decisionLatencyMs: orderAtMs - receivedAtMs,
        executionLatencyMs: Date.now() - orderAtMs,
      });
      return;
    }

    if (existingManaged) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'source_already_managed', existingManaged, signal, wakeSource });
      return;
    }

    if (!cfg.live) {
      await shadowOpen(signal, wakeSource, receivedAtMs, signal.action === 'increase', decisionAtMs);
      return;
    }

    // A single Hyperliquid account nets same-coin exposure. Keep this physical live constraint;
    // wide independent same-coin experimentation is handled by the shadow ledger above.
    const sameCoinManaged = state.getManagedForCoin(signal.coin);
    if (sameCoinManaged.length) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'same_coin_source_conflict_live_only', sameCoinManaged, signal, wakeSource });
      return;
    }

    const snap = await marketSnapshot(signal);
    if (!snap) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'unknown_hl_asset', signal, wakeSource });
      return;
    }
    const leverage = validateSourceLeverage(signal, snap.asset);
    if (leverage == null) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'source_leverage_unexecutable_on_hl', sourceLeverage: signal.leverage, assetMaxLeverage: snap.asset.maxLeverage, signal, wakeSource });
      return;
    }

    const chase = chaseBps(signal, snap.mid);
    if (chase != null && chase > cfg.maxChaseBps) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'entry_chased_too_far_live_only', chaseBps: chase, mid: snap.mid, signal, wakeSource });
      return;
    }

    const [equity, positions] = await Promise.all([
      hl.getAccountEquity(WALLET_ADDRESS),
      hl.getPositions(WALLET_ADDRESS),
    ]);
    const marginUsd = equity * (cfg.marginPct / 100);
    const notionalUsd = Math.min(marginUsd * leverage, cfg.maxNotionalUsd);
    if (notionalUsd < 10) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'below_hl_min_notional', notionalUsd, signal, wakeSource });
      return;
    }
    const size = roundSize(notionalUsd / snap.mid, snap.asset.szDecimals);

    if (state.managedCount() >= cfg.maxPositions) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'max_managed_positions_live_only', maxPositions: cfg.maxPositions, signal, wakeSource });
      return;
    }
    const existing = positions.find((p: any) => p.coin === signal.coin);
    if (existing) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'unmanaged_existing_coin_position', existing, signal, wakeSource });
      return;
    }

    await hl.setLeverage(signal.coin, leverage);
    const before = await hl.getPositions(WALLET_ADDRESS);
    const beforePos = before.find((p: any) => p.coin === signal.coin);
    const qtyBefore = beforePos ? String(beforePos.szi) : '0';
    const orderAtMs = Date.now();
    const result = await hl.placeMarketOrder(signal.coin, signal.side === 'long', size, cfg.maxSlippagePct);
    const after = await hl.getPositions(WALLET_ADDRESS);
    const afterPos = after.find((p: any) => p.coin === signal.coin);
    const qtyAfter = afterPos ? String(afterPos.szi) : '0';
    const beforeQty = Number(qtyBefore);
    const afterQty = Number(qtyAfter);
    const deltaQty = afterQty - beforeQty;
    const expectedSign = signal.side === 'long' ? 1 : -1;
    if (!Number.isFinite(afterQty) || !Number.isFinite(deltaQty) || deltaQty === 0) {
      throw new Error(`Order verification failed; position unchanged at ${qtyAfter}`);
    }
    if (Math.sign(deltaQty) !== expectedSign || Math.sign(afterQty) !== expectedSign) {
      throw new Error(`Order verification failed; expected ${signal.side}, before=${qtyBefore}, after=${qtyAfter}`);
    }

    let invoResult: any = null;
    try {
      invoResult = await invo.recordOpen({
        clientTxId: randomUUID(),
        coin: signal.coin,
        assetIndex: snap.assetIndex,
        entry: { side: signal.side, marginMode: 'isolated', leverage, tpPx: null, slPx: null },
        submission: { hlOrder: result, nonceMs: orderAtMs, hlResponse: result },
        summary: { qtyBefore, qtyAfter, intendedLeverage: leverage },
        mimicMeta: {
          portfolioId: signal.portfolioId,
          creatorInvoUserId: signal.ownerId,
          initialSourcePaperUpdateId: signal.postId,
          sourcePaperTradeBaseId: signal.sourceBaseId,
        },
      });
    } catch (err) {
      invoResult = { error: String(err) };
    }

    state.setManaged({
      coin: signal.coin,
      sourceBaseId: signal.sourceBaseId,
      sourceBaseShortId: signal.sourceBaseShortId,
      sourcePostId: signal.postId,
      username: signal.username,
      ownerId: signal.ownerId,
      portfolioId: signal.portfolioId,
      side: signal.side,
      openedAtMs: Date.now(),
      localBaseShortId: invoResult?.baseShortId ?? invoResult?.investment?.baseShortId,
      paper: false,
      entryMid: snap.mid,
      notionalUsd,
      marginUsd,
      leverage,
      size: Math.abs(deltaQty),
      sourceSize: positive(signal.entrySize) ?? undefined,
      addCount: 0,
    });
    state.markSeen(signal.key);
    log({
      type: signal.action === 'increase' ? 'opened_from_increase' : 'opened',
      signal, wakeSource, equity, mid: snap.mid, chaseBps: chase, leverage,
      marginUsd, notionalUsd, size, qtyBefore, qtyAfter, result, invoResult,
      detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
      decisionLatencyMs: orderAtMs - receivedAtMs,
      executionLatencyMs: Date.now() - orderAtMs,
    });
  } catch (err) {
    log({
      type: 'execution_error', signal, wakeSource,
      error: err instanceof Error ? err.message : String(err),
      ...(signal.action === 'close' ? closeFreshness(signal, receivedAtMs) : {}),
    });
  } finally {
    if (state.hasSeen(signal.key)) {
      if (eventKey != null) state.markSeen(eventKey);
      if (lifecycleKey != null) state.markSeen(lifecycleKey);
    }
    inFlight.delete(signal.key);
    inFlightSourceEvents.delete(inFlightKey);
  }
}

async function execute(signal: InvoSignal, wakeSource: string, receivedAtMs: number, feedFilter: string) {
  return sourceLifecycleQueue.run(signal.sourceBaseId, () => executeUnlocked(signal, wakeSource, receivedAtMs, feedFilter));
}


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

async function fetchAndProcess(source: string, hints: NotificationHints | undefined, receivedAtMs: number, feedFilter = cfg.feedFilter): Promise<number> {
  if (!cfg.live) {
    const pendingCloses = Object.values(state.snapshot().managed)
      .map(position => position.pendingSourceClose)
      .filter((signal): signal is InvoSignal => Boolean(signal));
    for (const signal of pendingCloses) {
      await execute(signal, `${source}:source_close_reconciliation`, receivedAtMs, feedFilter);
    }
  }
  const saved = state.getFeedCursor(feedFilter);
  const backfill = await fetchFeedBackfill(
    lastPostId => invo.getFeed(feedFilter, lastPostId, cfg.feedLimit),
    saved?.postId ?? null,
    cfg.feedMaxPages,
  );
  const posts = backfill.posts;
  if (saved && !backfill.cursorReached) {
    const gapPlan = planUnrecoverableGap(
      posts,
      post => signalFromFeedPost(post, receivedAtMs),
      sourceBaseId => Boolean(state.getManagedBySource(sourceBaseId)),
      key => state.hasSeen(key),
    );
    log({
      type: 'unrecoverable_feed_gap',
      feedFilter,
      savedCursor: saved,
      newestPostId: backfill.newestPostId,
      pagesFetched: backfill.pagesFetched,
      maxPages: cfg.feedMaxPages,
      exhausted: backfill.exhausted,
      managedCount: state.managedCount(),
      unresolvedManaged: state.snapshot().managed,
      cursorAdvanceAllowed: gapPlan.cursorAdvanceAllowed,
      recoverableOwnedCloses: gapPlan.ownedCloses.length,
    });
    // A missing historical cursor must never make a close already visible on a fetched
    // page disappear. Reconcile only closes for exposure this service still owns; leave
    // all other gap posts unseen and never advance the cursor across the missing range.
    for (const signal of gapPlan.ownedCloses) {
      await execute(signal, `${source}:gap_recovery`, receivedAtMs, feedFilter);
    }
    const remainingManagedCount = state.managedCount();
    const prospectiveRebaseAllowed = canProspectivelyRebaseGap(
      cfg.live,
      remainingManagedCount,
      backfill.newestPostId,
    );
    log({
      type: 'unrecoverable_feed_gap_reconciliation',
      feedFilter,
      savedCursor: saved,
      ownedCloseKeys: gapPlan.ownedCloses.map(signal => signal.key),
      reconciledOwnedCloses: gapPlan.ownedCloses.filter(signal => state.hasSeen(signal.key)).length,
      remainingManagedCount,
      cursorAdvanceAllowed: prospectiveRebaseAllowed,
    });
    if (prospectiveRebaseAllowed && backfill.newestPostId) {
      const rebasedCursor = {
        postId: backfill.newestPostId,
        observedAtMs: Date.now(),
        source: 'shadow_zero_managed_gap_rebase',
      };
      state.setFeedCursor(feedFilter, rebasedCursor);
      log({
        type: 'unrecoverable_feed_gap_rebased',
        feedFilter,
        previousCursor: saved,
        newCursor: rebasedCursor,
        fetchedPostsSkipped: posts.length,
        missingRangeUnknown: true,
        reason: 'shadow_zero_managed_prospective_rebase',
        productionTrading: false,
      });
      lastSuccessPollMs = Date.now();
      return gapPlan.ownedCloses.length;
    }
    lastSuccessPollMs = Date.now();
    return gapPlan.ownedCloses.length;
  }
  const tracked = (posts as any[]).map((post: any) => {
    const signal = signalFromFeedPost(post);
    tracker.observe(post, feedFilter, signal, receivedAtMs, false);
    return { signal };
  });
  if (tracked.length) tracker.flush();

  if (surfaceNeedsBaseline(state.hasFeedBaseline(feedFilter))) {
    const baseline = planSurfaceBaseline(
      tracked.map(row => row.signal),
      sourceBaseId => Boolean(state.getManagedBySource(sourceBaseId)),
    );
    for (const signal of baseline.skipped) state.markSeen(signal.key);
    lastSuccessPollMs = Date.now();
    log({ type: 'surface_baseline_indexed', posts: posts.length, skippedOpenAddsAndUnownedCloses: baseline.skipped.length, recoverableCloses: baseline.recoverableCloses.length, live: cfg.live, feedFilter, feedLimit: cfg.feedLimit, traderFunnel: tracker.report().funnel });
    for (const signal of baseline.recoverableCloses) {
      await execute(signal, 'surface_baseline_owned_close_recovery', receivedAtMs, feedFilter);
    }
    const startupHandled = baseline.recoverableCloses.every(signal => state.hasSeen(signal.key));
    if (startupHandled) {
      const baselineAtMs = Date.now();
      if (backfill.newestPostId) {
        state.setFeedCursor(feedFilter, { postId: backfill.newestPostId, observedAtMs: baselineAtMs, source: 'startup_baseline' });
      } else {
        state.markFeedBaselined(feedFilter, baselineAtMs);
      }
    }
    return baseline.recoverableCloses.length;
  }

  const signals: InvoSignal[] = tracked
    .map(({ signal }) => signal)
    .filter((s: InvoSignal | null): s is InvoSignal => Boolean(s))
    .filter((s: InvoSignal) => !state.hasSeen(s.key));

  signals.sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs));
  const matching = hints ? signals.filter(s => hintsMatchSignal(hints, s)) : [];
  const ordered = matching.length ? [...matching, ...signals.filter(s => !matching.includes(s))] : signals;

  // In shadow, sources are independent and can be hydrated in parallel. Live remains sequential.
  if (cfg.live) {
    for (const signal of ordered) await execute(signal, source, receivedAtMs, feedFilter);
  } else {
    await runSignalBatchBySource(ordered, signal => execute(signal, source, receivedAtMs, feedFilter));
  }
  const allHandled = ordered.every(signal => state.hasSeen(signal.key));
  if (allHandled && backfill.newestPostId) state.setFeedCursor(feedFilter, { postId: backfill.newestPostId, observedAtMs: Date.now(), source });
  lastSuccessPollMs = Date.now();
  return ordered.length;
}

async function wake(source: string, hints?: NotificationHints, receivedAtMs = Date.now(), feedFilter = cfg.feedFilter) {
  pendingWake = { source, hints, receivedAtMs, feedFilter };
  if (hydrating) return;
  hydrating = true;
  try {
    while (pendingWake) {
      const current = pendingWake;
      pendingWake = null;
      try {
        let found = await fetchAndProcess(current.source, current.hints, current.receivedAtMs, current.feedFilter);
        if (current.source === 'push_notification' && current.hints && found === 0) {
          for (const delayMs of [120, 280, 600]) {
            await new Promise(r => setTimeout(r, delayMs));
            found = await fetchAndProcess('push_hydration_retry', current.hints, current.receivedAtMs, current.feedFilter);
            if (found > 0) break;
          }
        }
        backoffMs = 0;
      } catch (err: any) {
        const status = err?.status;
        if (status === 429) backoffMs = Math.min(Math.max(backoffMs * 2, 2000), 30_000);
        log({ type: 'hydrate_error', source: current.source, status, backoffMs, error: err instanceof Error ? err.message : String(err) });
      }
    }
  } finally {
    hydrating = false;
  }
}

function directInvestmentRows(payload: any): any[] {
  return Array.isArray(payload?.investmentsTicker) ? payload.investmentsTicker : [];
}

function ownedDirectPortfolioIds(): Set<string> {
  return new Set(
    Object.values(state.snapshot().managed)
      .map(position => String(position.portfolioId ?? '').trim())
      .filter(Boolean),
  );
}

async function hydrateDirectTarget(
  target: ReturnType<EliteDirectWatchState['targets']>[number],
  selectorUpdatedAtMs: number | null,
  reason: string,
  observedAtMs: number,
) {
  directWatchMetrics.hydrationRequests += 1;
  const openPayload = await invo.getPortfolioInvestments(target.portfolioId, true, 1, 100);
  const openRows = directInvestmentRows(openPayload);
  const signals = signalsFromDirectInvestments(
    openRows, [], target, target.processedThroughMs, observedAtMs,
  );
  directWatchMetrics.hydrationCount += 1;
  directWatchMetrics.signalsObserved += signals.length;
  for (const signal of signals) {
    await execute(signal, `elite_direct:${reason}`, observedAtMs, target.sourceFilter);
  }
  const allHandled = signals.every(signal => signalWasSeen(signal, key => state.hasSeen(key)));
  const highWaterMs = signals.reduce(
    (highWater, signal) => Math.max(highWater, signal.sourceTimeMs ?? highWater),
    target.processedThroughMs,
  );
  if (allHandled) {
    directWatch.commitHydration(
      target.portfolioId,
      highWaterMs,
      selectorUpdatedAtMs == null ? undefined : selectorUpdatedAtMs,
    );
    directWatchMetrics.signalsHandled += signals.length;
    directWatchMetrics.lastSuccessAtMs = Date.now();
  }
  log({
    type: 'elite_direct_hydration',
    source: 'portfolio_specific_read_only',
    reason,
    portfolioId: target.portfolioId,
    username: target.username,
    selectorUpdatedAtMs,
    previousProcessedThroughMs: target.processedThroughMs,
    highWaterMs,
    signalCount: signals.length,
    allHandled,
    live: false,
  });
}

async function hydrateClosedHistory(
  target: ReturnType<EliteDirectWatchState['targets']>[number],
  reason: 'closed_history_baseline' | 'periodic_closed_poll',
  observedAtMs: number,
) {
  if (!target.closedHistoryInitialized) {
    const baseline = await establishClosedBaseline(async page => {
      directWatchMetrics.hydrationRequests += 1;
      return directInvestmentRows(await invo.getPortfolioInvestments(target.portfolioId, false, page, 100));
    }, cfg.directWatchClosedMaxPages, 100);
    directWatchMetrics.closedHydrationCount += 1;
    if (baseline.overflow) directWatchMetrics.closedOverflowRiskCount += 1;
    if (baseline.boundaryReached) {
      directWatch.commitClosedHydration(target.portfolioId, baseline.boundaryRows, observedAtMs);
      directWatchMetrics.closedBaselineCount += 1;
    }
    log({
      type: baseline.overflow ? 'elite_direct_closed_baseline_overflow' : 'elite_direct_closed_history_baseline',
      portfolioId: target.portfolioId, indexedRows: baseline.boundaryRows.length,
      boundaryTimestampMs: baseline.boundaryTimestampMs, boundaryIds: baseline.boundaryIds.length,
      pagesFetched: baseline.pagesFetched, boundaryReached: baseline.boundaryReached,
      boundaryReason: baseline.boundaryReason, overflow: baseline.overflow,
      watermarkCommitted: baseline.boundaryReached, replayedSignals: 0, live: false,
    });
    return;
  }
  const rows: any[] = [];
  let boundaryReached = false;
  let boundaryReason: ReturnType<typeof closedBoundaryProof>['reason'] = null;
  const storedBoundaryIds = new Set(target.closedBoundaryIds);
  const encounteredBoundaryIds = new Set<string>();
  let pagesFetched = 0;
  for (let page = 1; page <= cfg.directWatchClosedMaxPages; page += 1) {
    directWatchMetrics.hydrationRequests += 1;
    const payload = await invo.getPortfolioInvestments(target.portfolioId, false, page, 100);
    pagesFetched += 1;
    const pageRows = directInvestmentRows(payload);
    rows.push(...pageRows);
    const proof = target.closedHistoryInitialized
      ? closedBoundaryProof(
          pageRows, target.closedProcessedThroughMs, storedBoundaryIds,
          encounteredBoundaryIds, 100,
        )
      : { reached: pageRows.length < 100, reason: pageRows.length < 100 ? 'endpoint_exhausted' as const : null };
    if (proof.reached) {
      boundaryReached = true;
      boundaryReason = proof.reason;
      break;
    }
  }
  directWatchMetrics.closedHydrationCount += 1;
  if (!boundaryReached) directWatchMetrics.closedOverflowRiskCount += 1;

  const signals = closedSignalsAfterBoundary(
    rows, target, target.closedProcessedThroughMs, target.closedBoundaryIds, observedAtMs,
  );
  directWatchMetrics.signalsObserved += signals.length;
  for (const signal of signals) await execute(signal, `elite_direct:${reason}`, observedAtMs, target.sourceFilter);
  const allHandled = signals.every(signal => signalWasSeen(signal, key => state.hasSeen(key)));
  const watermarkCommitted = allHandled && boundaryReached;
  if (watermarkCommitted) {
    directWatch.commitClosedHydration(target.portfolioId, rows, observedAtMs);
    directWatchMetrics.signalsHandled += signals.length;
  }
  log({
    type: 'elite_direct_closed_hydration', portfolioId: target.portfolioId, reason,
    previousClosedProcessedThroughMs: target.closedProcessedThroughMs,
    signalCount: signals.length, pagesFetched, boundaryReached, boundaryReason,
    allHandled, watermarkCommitted, live: false,
  });
}

function applyDirectWatchRateLimit(nowMs: number) {
  directWatchMetrics.http429s += 1;
  directWatchBackoffMs = Math.min(Math.max(directWatchBackoffMs * 2, 2_000), 30_000);
  directWatchBackoffUntilMs = nowMs + directWatchBackoffMs;
}

function logTargetFailures(phase: string, failed: Array<{ item: any; error: unknown }>) {
  for (const { item, error } of failed) {
    directWatchMetrics.targetErrors += 1;
    log({ type: 'elite_direct_target_error', phase, portfolioId: item.target.portfolioId,
      status: (error as any)?.status, error: error instanceof Error ? error.message : String(error), live: false });
  }
}

async function scanEliteDirectWatch(nowMs = Date.now()) {
  if (cfg.live || nowMs < directWatchBackoffUntilMs) return;
  try {
    const candidate = loadEliteDirectTargets(cfg.candidateStatePath, nowMs, cfg.candidateStateMaxAgeMs);
    directWatch.syncTargets(candidate.targets, ownedDirectPortfolioIds(), nowMs);
    const targets = directWatch.targets();
    const targetsByFilter = new Map<string, typeof targets>();
    for (const target of targets) {
      const rows = targetsByFilter.get(target.sourceFilter) ?? [];
      rows.push(target);
      targetsByFilter.set(target.sourceFilter, rows);
    }
    const selectorChanges = new Map<string, number>();
    const selectorGroups = [...targetsByFilter.entries()];
    const rotated = selectorGroups.length === 0 ? [] : selectorGroups.map(
      (_, offset) => selectorGroups[(directWatchSelectorIndex + offset) % selectorGroups.length],
    );
    let selectorRateLimited = false;
    for (const [sourceFilter, filterTargets] of rotated) {
      directWatchMetrics.selectorRequests += 1;
      directWatchSelectorIndex = (directWatchSelectorIndex + 1) % selectorGroups.length;
      try {
        const payload = await invo.discoverPortfolios(sourceFilter, 1, 100);
        const rows = Array.isArray(payload?.items) ? payload.items : [];
        const byId = new Map(rows.map((row: any) => [String(row?.id ?? ''), row]));
        for (const target of filterTargets) {
          const row: any = byId.get(target.portfolioId);
          const selectorUpdatedAtMs = directSourceTimeMs(row?.updatedAt);
          if (selectorUpdatedAtMs != null) {
            const decision = directWatch.observeSelector(target.portfolioId, selectorUpdatedAtMs);
            if (decision.hydrate) selectorChanges.set(target.portfolioId, selectorUpdatedAtMs);
          }
        }
      } catch (error: any) {
        directWatchMetrics.selectorErrors += 1;
        log({ type: 'elite_direct_selector_error', sourceFilter, status: error?.status,
          error: error instanceof Error ? error.message : String(error), live: false });
        if (error?.status === 429) { applyDirectWatchRateLimit(Date.now()); selectorRateLimited = true; break; }
      }
    }

    // Selector timestamps are hints only. Periodic direct plans are constructed
    // even when one or more selector requests fail.
    const hydrationPlan = planDirectHydrations(
      targets, selectorChanges, nowMs, cfg.directWatchFallbackPollMs,
      cfg.directWatchMaxHydratesPerScan,
    );
    const closedPlan = planClosedHydrations(
      directWatch.targets(), nowMs, cfg.directWatchClosedPollMs,
      cfg.directWatchMaxClosedHydratesPerScan,
    );
    if (selectorRateLimited) {
      directWatchMetrics.skippedAfterRateLimit += hydrationPlan.length + closedPlan.length;
      log({ type: 'elite_direct_rate_limit_skip', phase: 'selector', skippedOpen: hydrationPlan.length,
        skippedClosed: closedPlan.length, backoffMs: directWatchBackoffMs, live: false });
      return;
    }

    const openRun = await runIsolatedHydrations(
      hydrationPlan,
      item => directWatch.noteFallbackPoll(item.target.portfolioId, nowMs),
      item => hydrateDirectTarget(item.target, item.selectorUpdatedAtMs, item.reason, nowMs),
    );
    logTargetFailures('open', openRun.failed);
    directWatchMetrics.skippedAfterRateLimit += openRun.skippedAfterRateLimit.length;
    if (openRun.rateLimited) {
      applyDirectWatchRateLimit(Date.now());
      directWatchMetrics.skippedAfterRateLimit += closedPlan.length;
      log({ type: 'elite_direct_rate_limit_skip', phase: 'open', skippedOpen: openRun.skippedAfterRateLimit.length,
        skippedClosed: closedPlan.length, backoffMs: directWatchBackoffMs, live: false });
      return;
    }

    const closedRun = await runIsolatedHydrations(
      closedPlan,
      item => directWatch.noteClosedPoll(item.target.portfolioId, nowMs),
      item => hydrateClosedHistory(item.target, item.reason, nowMs),
    );
    logTargetFailures('closed', closedRun.failed);
    directWatchMetrics.skippedAfterRateLimit += closedRun.skippedAfterRateLimit.length;
    if (closedRun.rateLimited) {
      applyDirectWatchRateLimit(Date.now());
      log({ type: 'elite_direct_rate_limit_skip', phase: 'closed', skippedOpen: 0,
        skippedClosed: closedRun.skippedAfterRateLimit.length, backoffMs: directWatchBackoffMs, live: false });
      return;
    }
    directWatchBackoffMs = 0;
    directWatchBackoffUntilMs = 0;
    directWatchMetrics.lastSuccessAtMs = Date.now();
  } catch (err: any) {
    // State/filesystem faults remain scan-level failures; HTTP target faults are
    // isolated above and never reach this guard.
    log({ type: 'elite_direct_watch_error', source: 'portfolio_specific_read_only', status: err?.status,
      backoffMs: directWatchBackoffMs, error: err instanceof Error ? err.message : String(err), live: false });
  } finally {
    lastDirectWatchScanMs = nowMs;
    directWatchMetrics.lastScanAtMs = nowMs;
  }
}

function readJson(req: IncomingMessage): Promise<any> {
  return new Promise((resolveBody, reject) => {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 64 * 1024) reject(new Error('payload_too_large'));
    });
    req.on('end', () => {
      try { resolveBody(body ? JSON.parse(body) : {}); }
      catch { reject(new Error('invalid_json')); }
    });
    req.on('error', reject);
  });
}

function json(res: ServerResponse, status: number, body: any) {
  res.statusCode = status;
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify(body));
}

function startServer() {
  const server = createServer(async (req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      const population = tracker.report();
      const snapshot = state.snapshot();
      const directStatus = directWatch.status();
      const healthNowMs = Date.now();
      const oldestOpenPollAgeMs = directStatus.oldestOpenPollAtMs == null
        ? null : Math.max(0, healthNowMs - directStatus.oldestOpenPollAtMs);
      const oldestClosedPollAgeMs = directStatus.oldestClosedPollAtMs == null
        ? null : Math.max(0, healthNowMs - directStatus.oldestClosedPollAtMs);
      const paperPositions = Object.values(snapshot.managed).filter(position => position.paper);
      const unresolvedPaperPositions = paperPositions.filter(position => position.unresolvedAfterSourceClose);
      const markablePaperPositions = paperPositions.filter(position => !position.unresolvedAfterSourceClose);
      const shadowMarks = cfg.live ? [] : await mapConcurrent(markablePaperPositions, HEALTH_MTM_CONCURRENCY, async position => {
        try {
          return await markShadowPosition(position, shadowPolicy);
        } catch (err) {
          return {
            sourceBaseId: position.sourceBaseId,
            coin: position.coin,
            side: position.side,
            size: position.size ?? null,
            status: 'BOOK_REJECTED',
            reason: err instanceof Error ? err.message : String(err),
            markedAtMs: Date.now(),
          };
        }
      });
      return json(res, 200, {
        ok: true,
        initialized: cfg.discoverySurfaces.every(surface => state.hasFeedBaseline(surface)),
        initializedSurfaces: cfg.discoverySurfaces.filter(surface => state.hasFeedBaseline(surface)),
        live: cfg.live,
        researchWide: !cfg.live,
        shadowAdmissionMode: cfg.live ? 'LIVE_SCOPE' : 'ELITE_ONLY',
        eliteAdmissionVersion: ELITE_ADMISSION_VERSION,
        candidateStatePath: cfg.candidateStatePath,
        candidateStateMaxAgeMs: cfg.candidateStateMaxAgeMs,
        feedFilter: cfg.feedFilter,
        feedMaxPages: cfg.feedMaxPages,
        feedCursors: state.snapshot().feedCursors,
        directWatch: {
          enabled: !cfg.live,
          ...directStatus,
          ...directWatchMetrics,
          scanMs: cfg.directWatchScanMs,
          maxHydratesPerScan: cfg.directWatchMaxHydratesPerScan,
          fallbackPollMs: cfg.directWatchFallbackPollMs,
          closedPollMs: cfg.directWatchClosedPollMs,
          maxClosedHydratesPerScan: cfg.directWatchMaxClosedHydratesPerScan,
          closedMaxPages: cfg.directWatchClosedMaxPages,
          nominalOpenSweepMs: Math.ceil(directStatus.targetCount / cfg.directWatchMaxHydratesPerScan) * cfg.directWatchScanMs,
          nominalClosedSweepMs: Math.ceil(directStatus.targetCount / cfg.directWatchMaxClosedHydratesPerScan) * cfg.directWatchScanMs,
          nominalMaxRequestsPerScan: cfg.directWatchMaxHydratesPerScan
            + cfg.directWatchMaxClosedHydratesPerScan * cfg.directWatchClosedMaxPages
            + new Set(directWatch.targets().map(target => target.sourceFilter)).size,
          nominalMaxRequestsPerSecond: (cfg.directWatchMaxHydratesPerScan
            + cfg.directWatchMaxClosedHydratesPerScan * cfg.directWatchClosedMaxPages
            + new Set(directWatch.targets().map(target => target.sourceFilter)).size) / (cfg.directWatchScanMs / 1000),
          oldestOpenPollAgeMs,
          oldestOpenPollOverdueMs: oldestOpenPollAgeMs == null ? null : Math.max(0, oldestOpenPollAgeMs - cfg.directWatchFallbackPollMs),
          oldestClosedPollAgeMs,
          oldestClosedPollOverdueMs: oldestClosedPollAgeMs == null ? null : Math.max(0, oldestClosedPollAgeMs - cfg.directWatchClosedPollMs),
          openFreshnessGuarantee: false,
          openFreshnessLimitReason: '18s nominal sweep leaves 7s for feed, selector, transport, execution, and 429 backoff; use observed poll ages',
          backoffMs: directWatchBackoffMs,
          backoffUntilMs: directWatchBackoffUntilMs,
        },
        maxSignalAgeMs: cfg.maxSignalAgeMs,
        lastSuccessPollMs,
        managedCount: state.managedCount(),
        managed: snapshot.managed,
        shadowMarks,
        shadowOpenExposureCount: paperPositions.length,
        shadowMtmEligibleCount: markablePaperPositions.length,
        unresolvedSourceCloseExposureCount: unresolvedPaperPositions.length,
        unresolvedSourceCloseExposures: unresolvedPaperPositions.map(position => ({
          sourceBaseId: position.sourceBaseId,
          coin: position.coin,
          size: position.size ?? null,
          retryAttempts: position.sourceCloseRetryAttempts ?? 0,
          nextRetryAtMs: position.sourceCloseNextRetryAtMs ?? null,
          lastReason: position.sourceCloseLastReason ?? null,
        })),
        healthMtmConcurrency: HEALTH_MTM_CONCURRENCY,
        closedOnlyProfitabilityForbidden: true,
        shadowExecutionPolicy: shadowPolicy,
        executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
        costModelVersion: COST_MODEL_VERSION,
        traderFunnel: population.funnel,
        evidencePolicy: population.policy,
        assessmentQueue: population.assessmentQueue,
      });
    }

    if (req.method === 'GET' && req.url === '/traders') {
      return json(res, 200, tracker.report());
    }

    if (req.method === 'POST' && req.url === '/invo-notification') {
      if (cfg.bridgeToken && req.headers['x-bridge-token'] !== cfg.bridgeToken) {
        return json(res, 401, { ok: false, error: 'unauthorized' });
      }
      try {
        const receivedAtMs = Date.now();
        const payload = await readJson(req);
        const packageName = payload?.packageName ?? payload?.package ?? payload?.appPackage;
        if (packageName && packageName !== cfg.packageName) {
          return json(res, 202, { ok: true, ignored: 'not_invo_package' });
        }
        const hints = extractNotificationHints(payload);
        void wake('push_notification', hints, receivedAtMs);
        return json(res, 202, { ok: true, hints });
      } catch (err) {
        return json(res, 400, { ok: false, error: err instanceof Error ? err.message : String(err) });
      }
    }

    return json(res, 404, { ok: false, error: 'not_found' });
  });
  if (!['127.0.0.1', 'localhost', '::1'].includes(cfg.host) && !cfg.bridgeToken) {
    throw new Error('NOTIFICATION_BRIDGE_TOKEN is required when notification ingress is not loopback-only');
  }
  server.listen(cfg.port, cfg.host, () => {
    log({ type: 'notification_ingress_started', host: cfg.host, port: cfg.port, live: cfg.live });
  });
}

async function pollLoop() {
  while (true) {
    const wait = backoffMs || cfg.pollMs;
    await new Promise(r => setTimeout(r, wait));
    const surface = cfg.discoverySurfaces[discoverySurfaceIndex % cfg.discoverySurfaces.length];
    discoverySurfaceIndex += 1;
    if (!cfg.live) {
      try {
        await captureFundingOracleCheckpoints(Date.now());
      } catch (err) {
        log({ type: 'funding_oracle_capture_error', error: err instanceof Error ? err.message : String(err) });
      }
    }
    await wake(`api_poll:${surface}`, undefined, Date.now(), surface);
    const directNowMs = Date.now();
    if (!cfg.live && directNowMs - lastDirectWatchScanMs >= cfg.directWatchScanMs) {
      await scanEliteDirectWatch(directNowMs);
    }
  }
}

async function main() {
  await invo.ensureToken();
  if (cfg.live) {
    await hl.connect(HL_AGENT_KEY, WALLET_ADDRESS);
    await invo.checkAccountReady();
  }
  if (!cfg.live) {
    try {
      await captureFundingOracleCheckpoints(Date.now());
    } catch (err) {
      log({ type: 'funding_oracle_capture_error', phase: 'startup', error: err instanceof Error ? err.message : String(err) });
    }
  }
  // Establish every configured surface boundary before ingress and the rotating poller
  // start. Sequential requests keep startup bounded/429-safe and minimize the window in
  // which an event could arrive before a newly enabled surface has its own cursor.
  for (const surface of cfg.discoverySurfaces) {
    await wake(`startup_surface:${surface}`, undefined, Date.now(), surface);
  }
  if (!cfg.live) await scanEliteDirectWatch(Date.now());
  startServer();
  log({
    type: 'service_started',
    live: cfg.live,
    researchWide: !cfg.live,
    shadowAdmissionMode: cfg.live ? 'LIVE_SCOPE' : 'ELITE_ONLY',
    eliteAdmissionVersion: ELITE_ADMISSION_VERSION,
    candidateStatePath: cfg.candidateStatePath,
    candidateStateMaxAgeMs: cfg.candidateStateMaxAgeMs,
    pollMs: cfg.pollMs,
    maxSignalAgeMs: cfg.maxSignalAgeMs,
    feedFilter: cfg.feedFilter,
    discoverySurfaces: cfg.discoverySurfaces,
    feedLimit: cfg.feedLimit,
    feedMaxPages: cfg.feedMaxPages,
    directWatch: {
      enabled: !cfg.live,
      version: directWatch.status().version,
      scanMs: cfg.directWatchScanMs,
      maxHydratesPerScan: cfg.directWatchMaxHydratesPerScan,
      fallbackPollMs: cfg.directWatchFallbackPollMs,
      closedPollMs: cfg.directWatchClosedPollMs,
      maxClosedHydratesPerScan: cfg.directWatchMaxClosedHydratesPerScan,
      closedMaxPages: cfg.directWatchClosedMaxPages,
      statePath: cfg.directWatchStatePath,
      source: 'portfolio_specific_read_only',
    },
    shadowExecutionPolicy: shadowPolicy,
    fundingOracleMaxDelayMs: cfg.shadowFundingOracleMaxDelayMs,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
    closedOnlyProfitabilityForbidden: true,
    evidencePolicy: tracker.report().policy,
    leverageMode: 'source_exact_up_to_hl_asset_max',
    reups: true,
    shadowChaseGate: false,
    shadowPositionCap: false,
    shadowNotionalCap: false,
    marginPct: cfg.marginPct,
    copyAllFollowed: cfg.copyAllFollowed,
    liveMaxNotionalUsd: cfg.maxNotionalUsd,
    liveMaxChaseBps: cfg.maxChaseBps,
    liveMaxPositions: cfg.maxPositions,
    allow: [...cfg.allow],
  });
  await pollLoop();
}

main().catch(err => {
  console.error(err instanceof Error ? err.stack ?? err.message : String(err));
  process.exit(1);
});
