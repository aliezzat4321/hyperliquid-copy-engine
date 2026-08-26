export type SignalAction = 'open' | 'increase' | 'close';

export interface InvoSignal {
  key: string;
  postId: string;
  action: SignalAction;
  observedAtMs: number;
  sourceTimeMs: number | null;
  ownerId: string;
  username: string;
  portfolioId: string;
  sourceBaseId: string;
  sourceBaseShortId: string;
  coin: string;
  side: 'long' | 'short';
  leverage: number;
  entryPrice: number | null;
  closingPrice: number | null;
  entrySize: number | null;
}

export interface NotificationHints {
  username?: string;
  ticker?: string;
  portfolioId?: string;
  postId?: string;
  baseId?: string;
}

function toMs(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v < 10_000_000_000 ? v * 1000 : v;
  if (typeof v !== 'string' || !v) return null;
  const asNum = Number(v);
  if (Number.isFinite(asNum) && asNum > 0) return asNum < 10_000_000_000 ? asNum * 1000 : asNum;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : null;
}

function numeric(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function signalFromFeedPost(post: any, observedAtMs = Date.now()): InvoSignal | null {
  const update = post?.update;
  if (!update?.ticker || update.verifiedTrade !== true) return null;
  if (typeof update.directionLong !== 'boolean') return null;
  const parsedLeverage = numeric(update.leverage);
  if (parsedLeverage == null || parsedLeverage <= 0) return null;

  const isOpen = update.isOpen === true;
  const isClosed = update.isOpen === false && update.closingPrice != null;
  const action: SignalAction = isClosed
    ? 'close'
    : update.changes?.isAdded === false && isOpen
      ? 'increase'
      : 'open';

  const postId = String(post.id ?? update.id ?? '');
  const sourceBaseId = String(update.baseId ?? update.id ?? '');
  const sourceBaseShortId = String(update.baseShortId ?? '');
  const coin = String(update.ticker).toUpperCase();
  const ownerId = String(update.owner?.id ?? post.owner?.id ?? '');
  const username = String(update.owner?.username ?? post.owner?.username ?? '').replace(/^@/, '').toLowerCase();
  const portfolioId = String(update.portfolio?.id ?? '');

  if (!postId || !sourceBaseId || !coin || !portfolioId || !ownerId) return null;

  const sourceTimeMs = toMs(
    update.updatedAt ?? update.createdAt ?? post.updatedAt ?? post.createdAt ?? post.date,
  );

  return {
    key: `${postId}:${action}:${sourceBaseId}`,
    postId,
    action,
    observedAtMs,
    sourceTimeMs,
    ownerId,
    username,
    portfolioId,
    sourceBaseId,
    sourceBaseShortId,
    coin,
    side: update.directionLong ? 'long' : 'short',
    leverage: Math.max(1, Math.trunc(parsedLeverage)),
    entryPrice: numeric(update.entryPrice),
    closingPrice: numeric(update.closingPrice),
    entrySize: numeric(update.entrySize),
  };
}

function flattenStrings(value: unknown, out: string[], depth = 0) {
  if (depth > 4 || value == null) return;
  if (typeof value === 'string') { out.push(value); return; }
  if (typeof value === 'number' || typeof value === 'boolean') { out.push(String(value)); return; }
  if (Array.isArray(value)) {
    for (const item of value) flattenStrings(item, out, depth + 1);
    return;
  }
  if (typeof value === 'object') {
    for (const item of Object.values(value as Record<string, unknown>)) flattenStrings(item, out, depth + 1);
  }
}

/** Notification contents are hints only; orders always hydrate canonical Invo API data. */
export function extractNotificationHints(payload: any): NotificationHints {
  const strings: string[] = [];
  flattenStrings(payload, strings);
  const blob = strings.join(' ');

  const username = blob.match(/@([a-zA-Z0-9_.-]{2,40})/)?.[1]?.toLowerCase();
  const explicitTicker = payload?.data?.ticker ?? payload?.ticker ?? payload?.extras?.ticker;
  const ticker = explicitTicker
    ? String(explicitTicker).toUpperCase()
    : blob.match(/\b(BTC|ETH|SOL|HYPE|XRP|DOGE|AVAX|SUI|ARB|OP|LINK|ENA|AAVE|LTC|BNB)\b/i)?.[1]?.toUpperCase();

  const portfolioId = payload?.data?.portfolioId ?? payload?.portfolioId ?? payload?.extras?.portfolioId;
  const postId = payload?.data?.postId ?? payload?.postId ?? payload?.extras?.postId;
  const baseId = payload?.data?.baseId ?? payload?.baseId ?? payload?.extras?.baseId;

  return {
    ...(username ? { username } : {}),
    ...(ticker ? { ticker } : {}),
    ...(portfolioId ? { portfolioId: String(portfolioId) } : {}),
    ...(postId ? { postId: String(postId) } : {}),
    ...(baseId ? { baseId: String(baseId) } : {}),
  };
}

export function hintsMatchSignal(h: NotificationHints | undefined, s: InvoSignal): boolean {
  if (!h) return true;
  if (h.postId && h.postId !== s.postId) return false;
  if (h.baseId && h.baseId !== s.sourceBaseId) return false;
  if (h.portfolioId && h.portfolioId !== s.portfolioId) return false;
  if (h.username && h.username !== s.username) return false;
  if (h.ticker && h.ticker !== s.coin) return false;
  return true;
}
