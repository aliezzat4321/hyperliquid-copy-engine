const BASE = 'https://api.invoapp.com';
const APP_HEADERS = { 'x-app-version': '0.0.75', 'x-platform': 'web' } as const;

let token = '';
let refreshToken = '';

export class InvoHttpError extends Error {
  constructor(path: string, public readonly status: number, data: unknown) {
    super(`Invo ${path} ${status}: ${JSON.stringify(data)}`);
    this.name = 'InvoHttpError';
  }
}

export function setToken(value: string) {
  token = value.startsWith('Bearer ') ? value : `Bearer ${value}`;
}

export function setRefreshToken(value: string) {
  refreshToken = value.replace(/^Bearer\s+/i, '');
}

function accessTokenStillFresh(): boolean {
  if (!token) return false;
  try {
    const raw = token.replace(/^Bearer\s+/i, '').split('.')[1];
    const normalized = raw.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
    const payload = JSON.parse(Buffer.from(padded, 'base64').toString('utf8'));
    const expires = Number(payload.expires ?? payload.exp ?? 0);
    return Number.isFinite(expires) && expires - Date.now() / 1000 > 30;
  } catch {
    return false;
  }
}

async function refreshAccessToken(): Promise<boolean> {
  if (!refreshToken) return false;
  const resp = await fetch(`${BASE}/v1_0/auth/refresh_token`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${refreshToken}`, ...APP_HEADERS },
  });
  if (resp.status !== 200) return false;
  const data: any = await resp.json();
  if (!data?.accessToken) return false;
  token = `Bearer ${data.accessToken}`;
  if (data.refreshToken) refreshToken = String(data.refreshToken).replace(/^Bearer\s+/i, '');
  return true;
}

export async function ensureToken(): Promise<void> {
  if (accessTokenStillFresh()) return;
  const refreshed = await refreshAccessToken();
  if (!refreshed && !token) throw new Error('No valid Invo token and refresh failed');
}

function decodeResponse(text: string): unknown {
  try { return JSON.parse(text); } catch {}
  try { return JSON.parse(Buffer.from(text, 'base64').toString('utf8')); } catch {}
  return text;
}

async function post(path: string, body: unknown, retried = false): Promise<any> {
  await ensureToken();
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      Authorization: token,
      'Content-Type': 'application/json',
      ...APP_HEADERS,
    },
    body: JSON.stringify(body),
  });
  const data = decodeResponse(await resp.text());
  if (resp.status === 401 && !retried && await refreshAccessToken()) return post(path, body, true);
  if (resp.status >= 400) throw new InvoHttpError(path, resp.status, data);
  return data;
}

export async function getFeed(filter = 'following', lastPostId: string | null = null, itemLimit = 30) {
  return post('/v1_0/posts/get_feed', {
    filter: { filter, assetTypes: [] },
    params: { lastPostId, itemLimit },
  });
}

export async function checkAccountReady() {
  return post('/dex/account/ready', {});
}

export interface RecordOpenPayload {
  clientTxId: string;
  coin: string;
  assetIndex: number;
  entry: { side: 'long' | 'short'; marginMode: 'isolated' | 'cross'; leverage: number; tpPx: string | null; slPx: string | null };
  submission: { hlOrder: any; nonceMs: number; hlResponse: any };
  summary: { qtyBefore: string; qtyAfter: string; intendedLeverage: number };
  mimicMeta: { portfolioId: string; creatorInvoUserId: string; initialSourcePaperUpdateId: string; sourcePaperTradeBaseId: string };
}

export async function recordOpen(payload: RecordOpenPayload) {
  return post('/dex/position/create', payload);
}

export interface RecordClosePayload {
  clientTxId: string;
  baseShortId: string;
  assetIndex: number;
  submission: { hlOrder: any; nonceMs: number; hlResponse: any };
  summary: { qtyBefore: string; qtyAfter: string };
}

export async function recordClose(payload: RecordClosePayload) {
  return post('/dex/position/close', payload);
}
