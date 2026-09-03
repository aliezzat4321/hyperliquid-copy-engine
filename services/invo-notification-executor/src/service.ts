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
  const feedFilter = (process.env.NOTIFICATION_TRADER_FEED_FILTER ?? 'following').trim().toLowerCase();
  if (!['following', 'all', 'trending'].includes(feedFilter)) {
    throw new Error(`Invalid NOTIFICATION_TRADER_FEED_FILTER: ${feedFilter}`);
  }
  const discoverySurfaces = (process.env.NOTIFICATION_TRADER_DISCOVERY_SURFACES ?? 'following,all,trending')
    .split(',').map(v => v.trim().toLowerCase()).filter(Boolean);
  if (!discoverySurfaces.length || discoverySurfaces.some(v => !['following', 'all', 'trending'].includes(v))) {
    throw new Error(`Invalid NOTIFICATION_TRADER_DISCOVERY_SURFACES: ${discoverySurfaces.join(',')}`);
  }
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
    minEvidenceEvents: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MIN_EVIDENCE_EVENTS', 20))),
    minObservationDays: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MIN_OBSERVATION_DAYS', 7))),
    staleAfterMs: Math.max(60_000, n('NOTIFICATION_TRADER_STALE_AFTER_MS', 3 * 24 * 60 * 60 * 1000)),
    inactiveAfterMs: Math.max(60_000, n('NOTIFICATION_TRADER_INACTIVE_AFTER_MS', 14 * 24 * 60 * 60 * 1000)),
  };
}

const cfg = loadConfig();
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
const inFlight = new Set<string>();
let initialized = false;
let hydrating = false;
let pendingWake: { source: string; hints?: NotificationHints; receivedAtMs: number; feedFilter?: string } | null = null;
let lastSuccessPollMs = 0;
let backoffMs = 0;
let discoverySurfaceIndex = 0;

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

async function shadowOpen(signal: InvoSignal, wakeSource: string, receivedAtMs: number, openedFromIncrease: boolean) {
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

  const equity = await hl.getAccountEquity(WALLET_ADDRESS);
  const sizing = baseShadowSlice(equity, snap.mid, leverage);
  const chase = chaseBps(signal, snap.mid);
  const sourceSize = positive(signal.entrySize) ?? undefined;
  state.setManaged({
    coin: signal.coin,
    sourceBaseId: signal.sourceBaseId,
    sourceBaseShortId: signal.sourceBaseShortId,
    sourcePostId: signal.postId,
    username: signal.username,
    side: signal.side,
    openedAtMs: Date.now(),
    paper: true,
    entryMid: snap.mid,
    notionalUsd: sizing.notionalUsd,
    marginUsd: sizing.marginUsd,
    leverage,
    size: sizing.size,
    sourceSize,
    addCount: 0,
  });
  state.markSeen(signal.key);
  log({
    type: openedFromIncrease ? 'shadow_opened_from_increase' : 'shadow_opened',
    signal,
    wakeSource,
    equity,
    entryMid: snap.mid,
    chaseBps: chase,
    leverage,
    marginUsd: sizing.marginUsd,
    notionalUsd: sizing.notionalUsd,
    size: sizing.size,
    sourceSize,
    detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
    decisionLatencyMs: Date.now() - receivedAtMs,
  });
}

async function shadowReup(managed: ManagedPosition, signal: InvoSignal, wakeSource: string, receivedAtMs: number) {
  if (managed.side !== signal.side || managed.coin !== signal.coin) {
    state.markSeen(signal.key);
    log({ type: 'skip', reason: 'source_reup_direction_or_coin_mismatch', managed, signal, wakeSource });
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

  const equity = await hl.getAccountEquity(WALLET_ADDRESS);
  const fallback = baseShadowSlice(equity, snap.mid, leverage);
  const reup = shadowReupSize(managed, signal, fallback.size);
  const priorSize = positive(managed.size) ?? 0;
  const priorEntryMid = positive(managed.entryMid) ?? snap.mid;
  if (!(priorSize > 0) || !(reup.addSize > 0)) throw new Error('Invalid shadow re-up size');
  const newSize = priorSize + reup.addSize;
  const newEntryMid = ((priorEntryMid * priorSize) + (snap.mid * reup.addSize)) / newSize;
  const addNotionalUsd = snap.mid * reup.addSize;
  const addMarginUsd = addNotionalUsd / leverage;
  const newSourceSize = reup.sourceIncrementSize
    ? (positive(managed.sourceSize) ?? 0) + reup.sourceIncrementSize
    : managed.sourceSize;

  state.setManaged({
    ...managed,
    sourcePostId: signal.postId,
    sourceBaseShortId: signal.sourceBaseShortId || managed.sourceBaseShortId,
    leverage,
    entryMid: newEntryMid,
    size: newSize,
    sourceSize: newSourceSize,
    notionalUsd: (managed.notionalUsd ?? priorEntryMid * priorSize) + addNotionalUsd,
    marginUsd: (managed.marginUsd ?? 0) + addMarginUsd,
    addCount: (managed.addCount ?? 0) + 1,
  });
  state.markSeen(signal.key);
  log({
    type: 'shadow_reupped',
    username: managed.username ?? signal.username,
    coin: signal.coin,
    side: signal.side,
    sourceBaseId: signal.sourceBaseId,
    leverage,
    priorSize,
    addedSize: reup.addSize,
    newSize,
    priorEntryMid,
    addMid: snap.mid,
    newEntryMid,
    addNotionalUsd,
    addMarginUsd,
    sourceIncrementSize: reup.sourceIncrementSize,
    priorSourceSize: managed.sourceSize,
    newSourceSize,
    sizingModel: reup.sizingModel,
    copyPerSourceUnit: reup.copyPerSourceUnit,
    chaseBps: chaseBps(signal, snap.mid),
    signal,
    wakeSource,
    detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
    decisionLatencyMs: Date.now() - receivedAtMs,
  });
}

async function execute(signal: InvoSignal, wakeSource: string, receivedAtMs: number, feedFilter: string) {
  if (state.hasSeen(signal.key) || inFlight.has(signal.key)) return;
  inFlight.add(signal.key);
  const decisionAtMs = Date.now();

  try {
    // Wide shadow research deliberately includes every trader in the selected Invo feed.
    // Trader scope restrictions remain live-only.
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
        log({ type: 'skip', reason: 'close_not_owned_by_service', managed: null, signal, wakeSource });
        return;
      }

      if (!cfg.live) {
        const mids = await hl.getAllMids();
        const exitMid = Number(mids[signal.coin]);
        const entryMid = Number(managed.entryMid);
        const size = Number(managed.size);
        if (!(exitMid > 0) || !(entryMid > 0) || !(size > 0)) {
          state.clearManagedBySource(signal.sourceBaseId);
          state.markSeen(signal.key);
          log({ type: 'shadow_close_unpriced', reason: 'missing_shadow_economics', managed, signal, exitMid, wakeSource });
          return;
        }
        const direction = managed.side === 'long' ? 1 : -1;
        const grossPnlUsd = (exitMid - entryMid) * size * direction;
        const grossReturnBps = ((exitMid - entryMid) / entryMid) * direction * 10_000;
        const returnOnMarginPct = managed.marginUsd && managed.marginUsd > 0
          ? (grossPnlUsd / managed.marginUsd) * 100
          : null;
        const heldMs = Date.now() - managed.openedAtMs;
        state.clearManagedBySource(signal.sourceBaseId);
        state.markSeen(signal.key);
        log({
          type: 'shadow_closed',
          username: managed.username ?? signal.username,
          coin: signal.coin,
          side: managed.side,
          sourceBaseId: managed.sourceBaseId,
          entryMid,
          exitMid,
          sourceClosingPrice: signal.closingPrice,
          size,
          sourceSize: managed.sourceSize,
          addCount: managed.addCount ?? 0,
          notionalUsd: managed.notionalUsd,
          marginUsd: managed.marginUsd,
          leverage: managed.leverage,
          grossPnlUsd,
          grossReturnBps,
          returnOnMarginPct,
          heldMs,
          signal,
          wakeSource,
          detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
          decisionLatencyMs: Date.now() - receivedAtMs,
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
        log({ type: 'close_already_flat', signal, wakeSource });
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
      });
      return;
    }

    const existingManaged = state.getManagedBySource(signal.sourceBaseId);
    if (signal.action === 'increase' && existingManaged) {
      if (!cfg.live) {
        await shadowReup(existingManaged, signal, wakeSource, receivedAtMs);
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
      await shadowOpen(signal, wakeSource, receivedAtMs, signal.action === 'increase');
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
    log({ type: 'execution_error', signal, wakeSource, error: err instanceof Error ? err.message : String(err) });
  } finally {
    inFlight.delete(signal.key);
  }
}

async function fetchAndProcess(source: string, hints: NotificationHints | undefined, receivedAtMs: number, feedFilter = cfg.feedFilter): Promise<number> {
  const data = await invo.getFeed(feedFilter, null, cfg.feedLimit);
  const posts = data?.items ?? [];
  const tracked = (posts as any[]).map((post: any) => {
    const signal = signalFromFeedPost(post);
    tracker.observe(post, feedFilter, signal, receivedAtMs, false);
    return { signal };
  });
  if (tracked.length) tracker.flush();

  if (!initialized) {
    const recoverableCloses: InvoSignal[] = [];
    for (const { signal } of tracked) {
      if (!signal) continue;
      const managed = signal.action === 'close' ? state.getManagedBySource(signal.sourceBaseId) : null;
      if (managed) recoverableCloses.push(signal);
      else state.markSeen(signal.key);
    }
    initialized = true;
    lastSuccessPollMs = Date.now();
    log({ type: 'baseline_indexed', posts: posts.length, recoverableCloses: recoverableCloses.length, live: cfg.live, feedFilter, feedLimit: cfg.feedLimit, traderFunnel: tracker.report().funnel });
    for (const signal of recoverableCloses) await execute(signal, 'startup_recovery', receivedAtMs, feedFilter);
    return recoverableCloses.length;
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
    await Promise.all(ordered.map(signal => execute(signal, source, receivedAtMs, feedFilter)));
  }
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
      return json(res, 200, {
        ok: true,
        initialized,
        live: cfg.live,
        researchWide: !cfg.live,
        feedFilter: cfg.feedFilter,
        maxSignalAgeMs: cfg.maxSignalAgeMs,
        lastSuccessPollMs,
        managedCount: state.managedCount(),
        managed: state.snapshot().managed,
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
    await wake(`api_poll:${surface}`, undefined, Date.now(), surface);
  }
}

async function main() {
  await invo.ensureToken();
  if (cfg.live) {
    await hl.connect(HL_AGENT_KEY, WALLET_ADDRESS);
    await invo.checkAccountReady();
  }
  await wake('startup_baseline', undefined, Date.now());
  startServer();
  log({
    type: 'service_started',
    live: cfg.live,
    researchWide: !cfg.live,
    pollMs: cfg.pollMs,
    maxSignalAgeMs: cfg.maxSignalAgeMs,
    feedFilter: cfg.feedFilter,
    discoverySurfaces: cfg.discoverySurfaces,
    feedLimit: cfg.feedLimit,
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
