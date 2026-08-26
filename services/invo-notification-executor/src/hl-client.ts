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

export async function getClearinghouseState(wallet: string) {
  return info({ type: 'clearinghouseState', user: wallet });
}

export async function getAccountEquity(wallet: string): Promise<number> {
  const data = await getClearinghouseState(wallet);
  const raw = data?.marginSummary?.accountValue ?? data?.crossMarginSummary?.accountValue ?? '0';
  const equity = Number(raw);
  if (!Number.isFinite(equity) || equity <= 0) throw new Error(`Invalid account equity: ${raw}`);
  return equity;
}

export async function getPositions(wallet: string): Promise<any[]> {
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
  const positions = await getPositions(wallet);
  const pos = positions.find((p: any) => p.coin === coin);
  if (!pos) throw new Error(`No open position for ${coin}`);
  const signedSize = Number(pos.szi);
  return placeMarketOrder(coin, signedSize < 0, Math.abs(signedSize).toString(), slippagePct);
}

export { INVO_BUILDER };
