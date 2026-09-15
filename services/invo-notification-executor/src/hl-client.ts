import { Hyperliquid } from 'hyperliquid';

const INVO_BUILDER = { address: '0x557edb253b1d7ed5f15b248a5a3fd919fa5d3c81', fee: 35 };

function toSdkCoin(coin: string): string {
  return coin.includes('-') ? coin : `${coin}-PERP`;
}

let sdk: Hyperliquid | null = null;

export async function connect(agentKey: string, walletAddress: string): Promise<void> {
  sdk = new Hyperliquid({ privateKey: agentKey, walletAddress, enableWs: false });
  await sdk.connect();
}

function getSdk(): Hyperliquid {
  if (!sdk) throw new Error('Hyperliquid SDK is not connected');
  return sdk;
}

async function info(body: unknown): Promise<any> {
  const resp = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`Hyperliquid info HTTP ${resp.status}: ${await resp.text()}`);
  return resp.json();
}

export async function getMeta(): Promise<{ universe: { name: string; szDecimals: number; maxLeverage: number }[] }> {
  return info({ type: 'meta' });
}

export async function getAllMids(): Promise<Record<string, string>> {
  return info({ type: 'allMids' });
}

export interface HyperliquidAssetContext {
  oraclePx?: string | number;
  markPx?: string | number;
  midPx?: string | number | null;
  funding?: string | number;
}

export async function getOraclePrices(): Promise<Record<string, number>> {
  const payload = await info({ type: 'metaAndAssetCtxs' });
  if (!Array.isArray(payload) || payload.length < 2) {
    throw new Error('Invalid Hyperliquid metaAndAssetCtxs payload');
  }
  const meta = payload[0];
  const contexts = payload[1];
  if (!Array.isArray(meta?.universe) || !Array.isArray(contexts)) {
    throw new Error('Invalid Hyperliquid metaAndAssetCtxs shape');
  }
  const prices: Record<string, number> = {};
  for (let i = 0; i < meta.universe.length; i += 1) {
    const coin = String(meta.universe[i]?.name ?? '');
    const oraclePx = Number((contexts[i] as HyperliquidAssetContext | undefined)?.oraclePx);
    if (coin && Number.isFinite(oraclePx) && oraclePx > 0) prices[coin] = oraclePx;
  }
  return prices;
}

export interface HyperliquidL2Level {
  px: string;
  sz: string;
  n?: number;
}

export interface HyperliquidL2Book {
  coin: string;
  time: number;
  levels: [HyperliquidL2Level[], HyperliquidL2Level[]];
}

export async function getL2Book(coin: string): Promise<HyperliquidL2Book> {
  return info({ type: 'l2Book', coin });
}

export interface HyperliquidFundingPoint {
  coin?: string;
  fundingRate?: string | number;
  premium?: string | number;
  time?: number;
}

export interface FundingHistoryQuery {
  rows: HyperliquidFundingPoint[];
  diagnostics: {
    queryStartTimeMs: number;
    queryEndTimeMs: number;
    returnedTimeMs: number[];
    returnedRows: HyperliquidFundingPoint[];
  };
}

export async function getFundingHistory(
  coin: string,
  startTime: number,
  endTime: number,
): Promise<FundingHistoryQuery> {
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime) || startTime > endTime) {
    throw new Error(`Invalid funding history boundary for ${coin}: ${startTime}..${endTime}`);
  }
  const rows: HyperliquidFundingPoint[] = [];
  const returnedRows: HyperliquidFundingPoint[] = [];
  const seen = new Set<number>();
  let cursor = startTime;
  const maxPages = 20;
  for (let page = 0; page < maxPages && cursor <= endTime; page += 1) {
    const batch = await info({ type: 'fundingHistory', coin, startTime: cursor, endTime });
    if (!Array.isArray(batch)) throw new Error(`Invalid funding history for ${coin}`);
    returnedRows.push(...batch.map(row => ({
      coin: row?.coin,
      fundingRate: row?.fundingRate,
      premium: row?.premium,
      time: row?.time,
    })));
    let maxTime = -1;
    for (const row of batch) {
      const time = Number(row?.time);
      if (!Number.isFinite(time)) continue;
      maxTime = Math.max(maxTime, time);
      // Enforce our inclusive accounting interval even if an upstream response includes
      // adjacent rows. Raw returned boundaries remain available in diagnostics below.
      if (time >= startTime && time <= endTime && !seen.has(time)) {
        seen.add(time);
        rows.push(row);
      }
    }
    if (batch.length < 500 || maxTime >= endTime) break;
    if (!(maxTime >= cursor)) throw new Error(`Funding history pagination made no progress for ${coin}`);
    if (page === maxPages - 1) {
      throw new Error(`Funding history pagination limit reached for ${coin}; economics incomplete`);
    }
    cursor = maxTime + 1;
  }
  rows.sort((a, b) => Number(a.time ?? 0) - Number(b.time ?? 0));
  return {
    rows,
    diagnostics: {
      queryStartTimeMs: startTime,
      queryEndTimeMs: endTime,
      returnedTimeMs: returnedRows.map(row => Number(row.time)).filter(Number.isFinite),
      returnedRows,
    },
  };
}

export async function getClearinghouseState(wallet: string) {
  if (!wallet) throw new Error('Hyperliquid wallet address is required for account state');
  return info({ type: 'clearinghouseState', user: wallet });
}

export async function getAccountEquity(wallet: string): Promise<number> {
  if (!wallet) {
    const paperEquity = Number(process.env.NOTIFICATION_TRADER_DRY_EQUITY_USD ?? '1000');
    if (!Number.isFinite(paperEquity) || paperEquity <= 0) {
      throw new Error(`Invalid NOTIFICATION_TRADER_DRY_EQUITY_USD: ${process.env.NOTIFICATION_TRADER_DRY_EQUITY_USD}`);
    }
    return paperEquity;
  }
  const data = await getClearinghouseState(wallet);
  const raw = data?.marginSummary?.accountValue ?? data?.crossMarginSummary?.accountValue ?? '0';
  const equity = Number(raw);
  if (!Number.isFinite(equity) || equity <= 0) throw new Error(`Invalid account equity: ${raw}`);
  return equity;
}

export async function getPositions(wallet: string): Promise<any[]> {
  if (!wallet) return [];
  const data = await getClearinghouseState(wallet);
  return (data?.assetPositions ?? [])
    .filter((p: any) => Number(p?.position?.szi) !== 0)
    .map((p: any) => p.position);
}

export async function setLeverage(coin: string, leverage: number) {
  return getSdk().exchange.updateLeverage(toSdkCoin(coin), 'isolated', leverage);
}

export async function placeMarketOrder(coin: string, isBuy: boolean, size: string, slippagePct: number) {
  const mids = await getAllMids();
  const mid = Number(mids[coin]);
  if (!(mid > 0)) throw new Error(`No mid price for ${coin}`);
  const rawPx = isBuy ? mid * (1 + slippagePct) : mid * (1 - slippagePct);
  const limitPx = Number(rawPx.toPrecision(5));
  return getSdk().exchange.placeOrder({
    coin: toSdkCoin(coin),
    is_buy: isBuy,
    sz: Number(size),
    limit_px: limitPx,
    order_type: { limit: { tif: 'Ioc' } },
    reduce_only: false,
    grouping: 'na',
    builder: INVO_BUILDER,
  });
}

export async function closePosition(coin: string, wallet: string, slippagePct: number) {
  if (!wallet) throw new Error('Hyperliquid wallet address is required for live close');
  const positions = await getPositions(wallet);
  const pos = positions.find((p: any) => p.coin === coin);
  if (!pos) throw new Error(`No open position for ${coin}`);
  const signedSize = Number(pos.szi);
  return placeMarketOrder(coin, signedSize < 0, Math.abs(signedSize).toString(), slippagePct);
}

export { INVO_BUILDER };
