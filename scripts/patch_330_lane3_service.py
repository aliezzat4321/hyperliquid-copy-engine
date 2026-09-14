from pathlib import Path

path = Path('services/invo-notification-executor/src/service.ts')
text = path.read_text()


def replace_between(source: str, start: str, end: str, replacement: str) -> str:
    i = source.index(start)
    j = source.index(end, i)
    return source[:i] + replacement + source[j:]


import_anchor = "import { fetchFeedBackfill } from './feed-backfill.js';\n"
imports = """import { fetchFeedBackfill } from './feed-backfill.js';
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
  openAction,
  simulateL2Fill,
  type ShadowExecutionPolicy,
} from './shadow-execution.js';
"""
assert text.count(import_anchor) == 1
text = text.replace(import_anchor, imports, 1)

config_anchor = "    feedMaxPages: Math.max(1, Math.min(100, Math.trunc(n('NOTIFICATION_TRADER_FEED_MAX_PAGES', 20)))),\n"
config_add = config_anchor + """    shadowMaxBookAgeMs: Math.max(50, n('NOTIFICATION_TRADER_SHADOW_MAX_BOOK_AGE_MS', 750)),
    shadowMaxSpreadBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_MAX_SPREAD_BPS', 50)),
    shadowTakerFeeBps: Math.max(0, n('NOTIFICATION_TRADER_SHADOW_TAKER_FEE_BPS', 4.5)),
    shadowMinNotionalUsd: Math.max(1, n('NOTIFICATION_TRADER_SHADOW_MIN_NOTIONAL_USD', 10)),
"""
assert text.count(config_anchor) == 1
text = text.replace(config_anchor, config_add, 1)

cfg_anchor = "const cfg = loadConfig();\n"
cfg_add = cfg_anchor + """const shadowPolicy: ShadowExecutionPolicy = {
  maxBookAgeMs: cfg.shadowMaxBookAgeMs,
  maxSpreadBps: cfg.shadowMaxSpreadBps,
  minNotionalUsd: cfg.shadowMinNotionalUsd,
  takerFeeBps: cfg.shadowTakerFeeBps,
};
"""
assert text.count(cfg_anchor) == 1
text = text.replace(cfg_anchor, cfg_add, 1)

shadow_open = r"""async function shadowOpen(
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
    exposureCheckpoints: [{ atMs: fill.receivedAtMs, notionalUsd: fill.notionalUsd }],
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
    fundingModel: 'hyperliquid fundingHistory; no zero-cost omission',
    detectionLatencyMs: detectionLatencyMs(signal, receivedAtMs),
    decisionLatencyMs: fill.requestedAtMs - receivedAtMs,
    marketDataLatencyMs: fill.receivedAtMs - fill.requestedAtMs,
  });
}

"""
text = replace_between(text, 'async function shadowOpen(', 'async function shadowReup(', shadow_open)

shadow_reup = r"""async function shadowReup(
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
    { atMs: fill.receivedAtMs, notionalUsd: newEntryNotional },
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

"""
text = replace_between(text, 'async function shadowReup(', 'async function execute(', shadow_reup)

close_start = "      if (!cfg.live) {\n"
close_end = "      const sameCoinManaged = state.getManagedForCoin(signal.coin);\n"
close_i = text.index(close_start, text.index("if (signal.action === 'close')"))
close_j = text.index(close_end, close_i)
close_block = r"""      if (!cfg.live) {
        const size = Number(managed.size);
        const assetBook = await fetchAssetBook(signal.coin);
        if (!assetBook || !(size > 0)) {
          state.setManaged({ ...managed, unresolvedAfterSourceClose: true });
          state.markSeen(signal.key);
          log({
            type: 'shadow_close_incomplete', reason: !assetBook ? 'missing_book' : 'invalid_managed_size',
            economicsCompleteness: 'INCOMPLETE', managed, signal, wakeSource,
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
          state.setManaged({ ...managed, unresolvedAfterSourceClose: true });
          state.markSeen(signal.key);
          log({
            type: 'shadow_close_rejected', reason: result.reason, detail: result.detail ?? null,
            economicsCompleteness: 'UNRESOLVED_EXPOSURE', managed, signal, wakeSource,
            decisionAtMs, bookRequestedAtMs: assetBook.requestedAtMs,
            bookReceivedAtMs: assetBook.receivedAtMs,
            ...closeFreshness(signal, receivedAtMs),
          });
          return;
        }

        const fill = result.fill;
        const legacy = managed.executionEvidenceVersion !== EXECUTION_EVIDENCE_VERSION
          || !(Number(managed.entryPrice) > 0)
          || !(Number(managed.entryNotionalExecutedUsd) > 0)
          || !Array.isArray(managed.exposureCheckpoints);
        const fraction = Math.min(1, fill.filledSize / size);
        let fundingUsd: number | null = null;
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
        if (remainingSize > 1e-12) {
          const remainingFraction = remainingSize / size;
          state.setManaged({
            ...managed,
            size: remainingSize,
            entryFeeUsd: Number(managed.entryFeeUsd ?? 0) * remainingFraction,
            entrySlippageUsd: Number(managed.entrySlippageUsd ?? 0) * remainingFraction,
            entryNotionalExecutedUsd: Number(managed.entryNotionalExecutedUsd ?? 0) * remainingFraction,
            notionalUsd: Number(managed.notionalUsd ?? 0) * remainingFraction,
            marginUsd: Number(managed.marginUsd ?? 0) * remainingFraction,
            unresolvedAfterSourceClose: true,
            exposureCheckpoints: [
              ...(managed.exposureCheckpoints ?? []),
              { atMs: fill.receivedAtMs, notionalUsd: Number(managed.entryNotionalExecutedUsd ?? 0) * remainingFraction },
            ],
          });
        } else {
          state.clearManagedBySource(signal.sourceBaseId);
        }
        state.markSeen(signal.key);
        log({
          type: remainingSize > 1e-12 ? 'shadow_partially_closed' : 'shadow_closed',
          economicsCompleteness: legacy
            ? 'INCOMPLETE_LEGACY_ENTRY'
            : fundingUsd == null ? 'INCOMPLETE_FUNDING' : 'COMPLETE_EXECUTION_REALISTIC',
          fundingError,
          username: managed.username ?? signal.username,
          coin: signal.coin, side: managed.side, sourceBaseId: managed.sourceBaseId,
          entryPrice: managed.entryPrice ?? null, entryBookMid: managed.entryBookMid ?? null,
          exitPrice: fill.avgPx, exitBookMid: fill.midPx,
          sourceClosingPrice: signal.closingPrice, requestedCloseSize: fill.requestedRoundedSize,
          closedSize: fill.filledSize, unresolvedSize: remainingSize,
          partialFill: fill.partial, sourceSize: managed.sourceSize,
          addCount: managed.addCount ?? 0, leverage: managed.leverage,
          grossPnlUsd: economics?.grossPnlUsd ?? null,
          grossReturnBps: economics?.grossReturnBps ?? null,
          entryFeeUsd: economics?.entryFeeUsd ?? null,
          exitFeeUsd: economics?.exitFeeUsd ?? fill.feeUsd,
          fundingUsd: economics?.fundingUsd ?? fundingUsd,
          fundingPoints, fundingModel,
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

"""
text = text[:close_i] + close_block + text[close_j:]

text = text.replace(
    "        await shadowReup(existingManaged, signal, wakeSource, receivedAtMs);\n",
    "        await shadowReup(existingManaged, signal, wakeSource, receivedAtMs, decisionAtMs);\n",
    1,
)
text = text.replace(
    "      await shadowOpen(signal, wakeSource, receivedAtMs, signal.action === 'increase');\n",
    "      await shadowOpen(signal, wakeSource, receivedAtMs, signal.action === 'increase', decisionAtMs);\n",
    1,
)

health_anchor = """    if (req.method === 'GET' && req.url === '/health') {
      const population = tracker.report();
      return json(res, 200, {
"""
health_replacement = """    if (req.method === 'GET' && req.url === '/health') {
      const population = tracker.report();
      const snapshot = state.snapshot();
      const paperPositions = Object.values(snapshot.managed).filter(position => position.paper);
      const shadowMarks = cfg.live ? [] : await Promise.all(paperPositions.map(async position => {
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
      }));
      return json(res, 200, {
"""
assert text.count(health_anchor) == 1
text = text.replace(health_anchor, health_replacement, 1)
text = text.replace("        managed: state.snapshot().managed,\n", "        managed: snapshot.managed,\n        shadowMarks,\n        shadowOpenExposureCount: paperPositions.length,\n        closedOnlyProfitabilityForbidden: true,\n        shadowExecutionPolicy: shadowPolicy,\n        executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,\n        costModelVersion: COST_MODEL_VERSION,\n", 1)

startup_anchor = "    feedMaxPages: cfg.feedMaxPages,\n"
startup_add = startup_anchor + """    shadowExecutionPolicy: shadowPolicy,
    executionEvidenceVersion: EXECUTION_EVIDENCE_VERSION,
    costModelVersion: COST_MODEL_VERSION,
    closedOnlyProfitabilityForbidden: true,
"""
# The first feedMaxPages is in service_started because health uses a literal field in response.
idx = text.rfind(startup_anchor)
assert idx >= 0
text = text[:idx] + startup_add + text[idx + len(startup_anchor):]

path.write_text(text)
print('PATCH_330_SERVICE=OK')
