import { appendFileSync, mkdirSync } from 'fs';
import { createServer, IncomingMessage, ServerResponse } from 'http';
import { dirname, resolve } from 'path';
import { randomUUID } from 'crypto';
import { validateEnv, INVO_TOKEN, INVO_REFRESH_TOKEN, HL_AGENT_KEY, resolveWalletAddress } from './env.js';
import * as invo from './invo-client.js';
import * as hl from './hl-client.js';
import { extractNotificationHints, hintsMatchSignal, InvoSignal, NotificationHints, signalFromFeedPost } from './notification-signal.js';
import { NotificationState } from './notification-state.js';

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
  return {
    live: b('NOTIFICATION_TRADER_LIVE', false),
    host: process.env.NOTIFICATION_TRADER_HOST ?? '127.0.0.1',
    port: n('NOTIFICATION_TRADER_PORT', 8787),
    pollMs: Math.max(500, n('NOTIFICATION_TRADER_POLL_MS', 1000)),
    maxSignalAgeMs: Math.max(1000, n('NOTIFICATION_TRADER_MAX_SIGNAL_AGE_MS', 5_000)),
    maxLeverage: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MAX_LEVERAGE', 20))),
    marginPct: Math.max(0.01, n('NOTIFICATION_TRADER_MARGIN_PCT', 1)),
    maxNotionalUsd: Math.max(10, n('NOTIFICATION_TRADER_MAX_NOTIONAL_USD', 500)),
    maxSlippagePct: Math.max(0.0001, n('NOTIFICATION_TRADER_MAX_SLIPPAGE_PCT', 0.005)),
    maxChaseBps: Math.max(0, n('NOTIFICATION_TRADER_MAX_CHASE_BPS', 25)),
    copyAllFollowed: b('NOTIFICATION_TRADER_COPY_ALL_FOLLOWED', false),
    maxPositions: Math.max(1, Math.trunc(n('NOTIFICATION_TRADER_MAX_POSITIONS', 5))),
    bridgeToken: process.env.NOTIFICATION_BRIDGE_TOKEN ?? '',
    packageName: process.env.NOTIFICATION_TRADER_PACKAGE_NAME ?? 'com.involio.app',
    allow: new Set(allow),
    statePath: resolve(process.env.NOTIFICATION_TRADER_STATE_PATH ?? 'data/notification-trader-state.json'),
    auditPath: resolve(process.env.NOTIFICATION_TRADER_AUDIT_PATH ?? 'data/notification-trader-audit.jsonl'),
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
const inFlight = new Set<string>();
let initialized = false;
let hydrating = false;
let pendingWake: { source: string; hints?: NotificationHints; receivedAtMs: number } | null = null;
let lastSuccessPollMs = 0;
let backoffMs = 0;

function log(event: Record<string, unknown>) {
  const row = { ts: new Date().toISOString(), ...event };
  console.log(JSON.stringify(row));
  mkdirSync(dirname(cfg.auditPath), { recursive: true });
  appendFileSync(cfg.auditPath, `${JSON.stringify(row)}\n`);
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

async function execute(signal: InvoSignal, wakeSource: string, receivedAtMs: number) {
  if (state.hasSeen(signal.key) || inFlight.has(signal.key)) return;
  inFlight.add(signal.key);
  const decisionAtMs = Date.now();

  try {
    if (signal.action !== 'close' && cfg.allow.size && !cfg.allow.has(signal.username)) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'trader_not_allowlisted', signal, wakeSource });
      return;
    }
    if (signal.action !== 'close' && !cfg.allow.size && !cfg.copyAllFollowed) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'no_live_trader_scope', signal, wakeSource });
      return;
    }

    const ageMs = signal.sourceTimeMs == null ? null : decisionAtMs - signal.sourceTimeMs;
    if (signal.action !== 'close' && signal.sourceTimeMs == null) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'missing_source_timestamp', signal, wakeSource });
      return;
    }
    if (signal.action !== 'close' && (signal.entryPrice == null || signal.entryPrice <= 0)) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'missing_source_entry_price', signal, wakeSource });
      return;
    }
    if (signal.action !== 'close' && ageMs != null && ageMs > cfg.maxSignalAgeMs) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'stale_signal', ageMs, signal, wakeSource });
      return;
    }

    if (signal.action === 'increase') {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'increase_not_supported_fail_closed', signal, wakeSource });
      return;
    }

    if (signal.action === 'close') {
      const managed = state.getManaged(signal.coin);
      if (!managed || managed.sourceBaseId !== signal.sourceBaseId) {
        state.markSeen(signal.key);
        log({ type: 'skip', reason: 'close_not_owned_by_service', managed, signal, wakeSource });
        return;
      }

      if (!cfg.live) {
        const mids = await hl.getAllMids();
        const exitMid = Number(mids[signal.coin]);
        const entryMid = Number(managed.entryMid);
        const size = Number(managed.size);
        if (!(exitMid > 0) || !(entryMid > 0) || !(size > 0)) {
          state.clearManaged(signal.coin);
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
        state.clearManaged(signal.coin);
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

      const before = await hl.getPositions(WALLET_ADDRESS);
      const pos = before.find((p: any) => p.coin === signal.coin);
      if (!pos) {
        state.clearManaged(signal.coin);
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
        const assetIndex = meta.universe.findIndex(a => a.name === signal.coin);
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

      state.clearManaged(signal.coin);
      state.markSeen(signal.key);
      log({
        type: 'closed', signal, wakeSource, result, invoResult,
        detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
        decisionLatencyMs: decisionAtMs - receivedAtMs,
        executionLatencyMs: Date.now() - orderAtMs,
      });
      return;
    }

    const existingManaged = state.getManaged(signal.coin);
    if (existingManaged?.sourceBaseId === signal.sourceBaseId) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'source_already_managed', existingManaged, signal, wakeSource });
      return;
    }
    if (existingManaged && existingManaged.sourceBaseId !== signal.sourceBaseId) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'same_coin_source_conflict', existingManaged, signal, wakeSource });
      return;
    }

    const meta = await hl.getMeta();
    const assetIndex = meta.universe.findIndex(a => a.name === signal.coin);
    if (assetIndex < 0) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'unknown_hl_asset', signal, wakeSource });
      return;
    }
    const asset = meta.universe[assetIndex];
    const leverage = Math.min(signal.leverage, cfg.maxLeverage, asset.maxLeverage);
    if (signal.leverage > leverage) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'source_leverage_above_limit', sourceLeverage: signal.leverage, allowedLeverage: leverage, signal, wakeSource });
      return;
    }

    const [equity, mids, positions] = await Promise.all([
      hl.getAccountEquity(WALLET_ADDRESS),
      hl.getAllMids(),
      hl.getPositions(WALLET_ADDRESS),
    ]);
    const mid = Number(mids[signal.coin]);
    if (!(mid > 0)) throw new Error(`No mid for ${signal.coin}`);

    const chase = chaseBps(signal, mid);
    if (chase != null && chase > cfg.maxChaseBps) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'entry_chased_too_far', chaseBps: chase, mid, signal, wakeSource });
      return;
    }

    const marginUsd = equity * (cfg.marginPct / 100);
    const notionalUsd = Math.min(marginUsd * leverage, cfg.maxNotionalUsd);
    if (notionalUsd < 10) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'below_hl_min_notional', notionalUsd, signal, wakeSource });
      return;
    }
    const size = roundSize(notionalUsd / mid, asset.szDecimals);

    const existing = positions.find((p: any) => p.coin === signal.coin);
    if (Object.keys(state.snapshot().managed).length >= cfg.maxPositions) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'max_managed_positions', maxPositions: cfg.maxPositions, signal, wakeSource });
      return;
    }
    if (existing) {
      state.markSeen(signal.key);
      log({ type: 'skip', reason: 'unmanaged_existing_coin_position', existing, signal, wakeSource });
      return;
    }

    if (!cfg.live) {
      state.setManaged({
        coin: signal.coin,
        sourceBaseId: signal.sourceBaseId,
        sourceBaseShortId: signal.sourceBaseShortId,
        sourcePostId: signal.postId,
        username: signal.username,
        side: signal.side,
        openedAtMs: Date.now(),
        paper: true,
        entryMid: mid,
        notionalUsd,
        marginUsd,
        leverage,
        size: Number(size),
      });
      state.markSeen(signal.key);
      log({
        type: 'shadow_opened', signal, wakeSource, equity, entryMid: mid, chaseBps: chase,
        leverage, marginUsd, notionalUsd, size: Number(size),
        detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
        decisionLatencyMs: Date.now() - receivedAtMs,
      });
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
        assetIndex,
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
      entryMid: mid,
      notionalUsd,
      marginUsd,
      leverage,
      size: Number(size),
    });
    state.markSeen(signal.key);
    log({
      type: 'opened', signal, wakeSource, equity, mid, chaseBps: chase, leverage,
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

async function fetchAndProcess(source: string, hints: NotificationHints | undefined, receivedAtMs: number): Promise<number> {
  const data = await invo.getFeed('following', null, 30);
  const posts = data?.items ?? [];

  if (!initialized) {
    const recoverableCloses: InvoSignal[] = [];
    for (const post of posts) {
      const signal = signalFromFeedPost(post);
      if (!signal) continue;
      const managed = signal.action === 'close' ? state.getManaged(signal.coin) : null;
      if (managed?.sourceBaseId === signal.sourceBaseId) recoverableCloses.push(signal);
      else state.markSeen(signal.key);
    }
    initialized = true;
    lastSuccessPollMs = Date.now();
    log({ type: 'baseline_indexed', posts: posts.length, recoverableCloses: recoverableCloses.length, live: cfg.live });
    for (const signal of recoverableCloses) await execute(signal, 'startup_recovery', receivedAtMs);
    return recoverableCloses.length;
  }

  const signals: InvoSignal[] = (posts as any[])
    .map((post: any) => signalFromFeedPost(post))
    .filter((s: InvoSignal | null): s is InvoSignal => Boolean(s))
    .filter((s: InvoSignal) => !state.hasSeen(s.key));

  signals.sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs));
  const matching = hints ? signals.filter(s => hintsMatchSignal(hints, s)) : [];
  const ordered = matching.length ? [...matching, ...signals.filter(s => !matching.includes(s))] : signals;

  for (const signal of ordered) await execute(signal, source, receivedAtMs);
  lastSuccessPollMs = Date.now();
  return ordered.length;
}

async function wake(source: string, hints?: NotificationHints, receivedAtMs = Date.now()) {
  pendingWake = { source, hints, receivedAtMs };
  if (hydrating) return;
  hydrating = true;
  try {
    while (pendingWake) {
      const current = pendingWake;
      pendingWake = null;
      try {
        let found = await fetchAndProcess(current.source, current.hints, current.receivedAtMs);
        if (current.source === 'push_notification' && current.hints && found === 0) {
          for (const delayMs of [120, 280, 600]) {
            await new Promise(r => setTimeout(r, delayMs));
            found = await fetchAndProcess('push_hydration_retry', current.hints, current.receivedAtMs);
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
      return json(res, 200, { ok: true, initialized, live: cfg.live, lastSuccessPollMs, managed: state.snapshot().managed });
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
    await wake('api_poll', undefined, Date.now());
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
    type: 'service_started', live: cfg.live, pollMs: cfg.pollMs,
    maxSignalAgeMs: cfg.maxSignalAgeMs, maxLeverage: cfg.maxLeverage,
    marginPct: cfg.marginPct, maxNotionalUsd: cfg.maxNotionalUsd,
    maxChaseBps: cfg.maxChaseBps, copyAllFollowed: cfg.copyAllFollowed,
    maxPositions: cfg.maxPositions, allow: [...cfg.allow],
  });
  await pollLoop();
}

main().catch(err => {
  console.error(err instanceof Error ? err.stack ?? err.message : String(err));
  process.exit(1);
});
