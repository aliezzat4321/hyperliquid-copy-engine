import {
  INVO_PRIMARY_REQUEST_CLASS, parseRetryAfterMs, type InvoRequestClass,
} from './invo-request-budget.js';

const BASE = 'https://api.invoapp.com';
const APP_HEADERS = { 'x-app-version': '0.0.75', 'x-platform': 'web' } as const;
export const INVO_HTTP_REQUEST_TIMEOUT_MS = Math.max(250, Number.parseInt(
  process.env.INVO_HTTP_REQUEST_TIMEOUT_MS ?? '2000', 10,
) || 2000);

let token = '';
let refreshToken = '';

export class InvoHttpError extends Error {
  constructor(
    path: string, public readonly status: number, data: unknown,
    /** Server-stated retry delay, when the rejection carried one. Drives adaptive cooldown. */
    public readonly retryAfterMs: number | null = null,
  ) {
    super(`Invo ${path} ${status}: ${JSON.stringify(data)}`);
    this.name = 'InvoHttpError';
  }
}

/**
 * Token refreshes are a hard precondition of every other request, so they are never gated
 * behind the coordinated request budget. They are still reported so the budget's view of
 * the account-wide footprint stays truthful.
 *
 * The class is threaded explicitly from the calling request rather than read from shared
 * module state: the feed poller and the direct watcher run concurrently, so a latched
 * "current class" would attribute a refresh to whichever loop happened to write it last.
 * That only ever mis-attributed a metric, never a gate, but an untrue footprint metric is
 * exactly what this subsystem exists to make trustworthy.
 */
export interface InvoAuthRequestObserver {
  /** One refresh is leaving the process, charged to the class that needed the token. */
  charge(requestClass: InvoRequestClass): void;
  /**
   * The refresh itself was rate limited. A refresh is ungated, never retried here, and its
   * failure surfaces to the caller as a generic auth error, so without this hook the one
   * request that proves the account is limited would be the one request the budget never
   * hears about — leaving it to keep pacing at a rate the account has already refused.
   */
  rateLimited(requestClass: InvoRequestClass, retryAfterMs: number | null): void;
}

let authRequestObserver: InvoAuthRequestObserver | null = null;
export function setAuthRequestObserver(observer: InvoAuthRequestObserver | null) {
  authRequestObserver = observer;
}

/** Observation must never block or fail authentication, which everything else depends on. */
function observeAuth(notify: (observer: InvoAuthRequestObserver) => void) {
  const observer = authRequestObserver;
  if (!observer) return;
  try { notify(observer); } catch { /* an observer fault cannot be allowed to break auth */ }
}

export function setToken(value: string) {
  token = value.startsWith('Bearer ') ? value : `Bearer ${value}`;
}

export function setRefreshToken(value: string) {
  refreshToken = value.replace(/^Bearer\s+/i, '');
}

function accessTokenStillFresh(minValidityMs = 30_000): boolean {
  if (!token) return false;
  try {
    const raw = token.replace(/^Bearer\s+/i, '').split('.')[1];
    const normalized = raw.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
    const payload = JSON.parse(Buffer.from(padded, 'base64').toString('utf8'));
    const expires = Number(payload.expires ?? payload.exp ?? 0);
    return Number.isFinite(expires) && expires * 1000 - Date.now() > minValidityMs;
  } catch {
    return false;
  }
}

async function refreshAccessToken(requestClass: InvoRequestClass): Promise<boolean> {
  if (!refreshToken) return false;
  observeAuth(observer => observer.charge(requestClass));
  const resp = await fetch(`${BASE}/v1_0/auth/refresh_token`, {
    method: 'GET',
    headers: { Authorization: `Bearer ${refreshToken}`, ...APP_HEADERS },
    signal: AbortSignal.timeout(INVO_HTTP_REQUEST_TIMEOUT_MS),
  });
  if (resp.status === 429) {
    observeAuth(observer => observer.rateLimited(
      requestClass, parseRetryAfterMs(resp.headers.get('retry-after'), Date.now()),
    ));
  }
  if (resp.status !== 200) return false;
  const data: any = await resp.json();
  if (!data?.accessToken) return false;
  token = `Bearer ${data.accessToken}`;
  if (data.refreshToken) refreshToken = String(data.refreshToken).replace(/^Bearer\s+/i, '');
  return true;
}

export async function ensureToken(
  requestClass: InvoRequestClass = INVO_PRIMARY_REQUEST_CLASS,
): Promise<void> {
  if (accessTokenStillFresh()) return;
  const refreshed = await refreshAccessToken(requestClass);
  if (!refreshed && !token) throw new Error('No valid Invo token and refresh failed');
}

/** Direct-watch scans require one token proven valid for their whole hard horizon. */
export async function ensureTokenFreshFor(
  minValidityMs: number, requestClass: InvoRequestClass = INVO_PRIMARY_REQUEST_CLASS,
): Promise<void> {
  if (accessTokenStillFresh(minValidityMs)) return;
  const refreshed = await refreshAccessToken(requestClass);
  if (!refreshed || !accessTokenStillFresh(minValidityMs)) {
    throw new Error(`Invo token is not provably fresh for ${minValidityMs}ms direct-watch horizon`);
  }
}

function decodeResponse(text: string): unknown {
  try { return JSON.parse(text); } catch {}
  try { return JSON.parse(Buffer.from(text, 'base64').toString('utf8')); } catch {}
  return text;
}

async function post(
  path: string, body: unknown, retried = false, beforeRequest?: () => Promise<void>, allowAuthRetry = true,
  requestClass: InvoRequestClass = INVO_PRIMARY_REQUEST_CLASS,
): Promise<any> {
  await ensureToken(requestClass);
  await beforeRequest?.();
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      Authorization: token,
      'Content-Type': 'application/json',
      ...APP_HEADERS,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(INVO_HTTP_REQUEST_TIMEOUT_MS),
  });
  const data = decodeResponse(await resp.text());
  if (resp.status === 401 && allowAuthRetry && !retried && await refreshAccessToken(requestClass)) {
    return post(path, body, true, beforeRequest, allowAuthRetry, requestClass);
  }
  if (resp.status >= 400) {
    throw new InvoHttpError(path, resp.status, data,
      parseRetryAfterMs(resp.headers.get('retry-after'), Date.now()));
  }
  return data;
}

/**
 * Reverse-engineered Invo portfolio discovery endpoint used by the Top/Trending portfolio surfaces.
 * `portfolioId` is the canonical candidate identity; owner/user identity is metadata only.
 */
export async function discoverPortfolios(filter: string, page = 1, size = 50, userId?: string) {
  const body: any = { filter, params: { page, size } };
  if (userId) body.userId = userId;
  return post('/v1_0/trending/get_portfolios_pl', body);
}

export async function getFeed(
  filter = 'following', lastPostId: string | null = null, itemLimit = 30,
  beforeRequest?: () => Promise<void>, requestClass: InvoRequestClass = 'FEED',
) {
  return post('/v1_0/posts/get_feed', {
    filter: { filter, assetTypes: [] },
    params: { lastPostId, itemLimit },
  }, false, beforeRequest, true, requestClass);
}

export async function getPortfolioInvestments(
  portfolioId: string, isOpen: boolean, page = 1, size = 100, beforeRequest?: () => Promise<void>,
  allowAuthRetry = true, requestClass: InvoRequestClass = INVO_PRIMARY_REQUEST_CLASS,
) {
  return post('/v1_0/investments/get_investments', {
    portfolioId,
    isOpen,
    params: { page, size },
  }, false, beforeRequest, allowAuthRetry, requestClass);
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
