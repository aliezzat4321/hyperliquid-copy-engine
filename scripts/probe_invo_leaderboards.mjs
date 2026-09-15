#!/usr/bin/env node

const BASE = 'https://api.invoapp.com';
const HEADERS = { 'x-app-version': '0.0.75', 'x-platform': 'web' };
let access = (process.env.INVO_ACCESS_TOKEN || process.env.INVO_TOKEN || '').replace(/^Bearer\s+/i, '');
let refresh = (process.env.INVO_REFRESH_TOKEN || '').replace(/^Bearer\s+/i, '');

function jwtFresh(token) {
  if (!token) return false;
  try {
    const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(Buffer.from(part + '='.repeat((4 - part.length % 4) % 4), 'base64').toString('utf8'));
    const exp = Number(payload.expires ?? payload.exp ?? 0);
    return exp - Date.now() / 1000 > 30;
  } catch { return false; }
}
async function ensureToken() {
  if (jwtFresh(access)) return;
  if (!refresh) throw new Error('No usable Invo refresh token');
  const r = await fetch(`${BASE}/v1_0/auth/refresh_token`, { headers: { Authorization: `Bearer ${refresh}`, ...HEADERS }, signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error(`refresh ${r.status}`);
  const data = await r.json();
  access = String(data.accessToken || '').replace(/^Bearer\s+/i, '');
  if (!access) throw new Error('refresh returned no access token');
}
async function post(body) {
  await ensureToken();
  const r = await fetch(`${BASE}/v1_0/trending/get_portfolios_pl`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${access}`, 'content-type': 'application/json', ...HEADERS },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(5000),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(`${r.status}: ${JSON.stringify(data).slice(0, 300)}`);
  return data;
}

for (const horizon of ['1D','1W','1M','1Y','AT']) {
  const pages = [];
  for (let page = 1; page <= 5; page++) {
    const data = await post({ filter: horizon, params: { page, size: 50 } });
    const items = Array.isArray(data.items) ? data.items : [];
    pages.push({
      requestedPage: page,
      responsePage: data.page ?? null,
      responseSize: data.size ?? null,
      itemCount: items.length,
      firstId: items[0]?.id ?? null,
      lastId: items.at(-1)?.id ?? null,
    });
    if (!items.length) break;
  }
  const distinctFirstIds = new Set(pages.map(p => p.firstId).filter(Boolean)).size;
  console.log(JSON.stringify({ horizon, pages, distinctFirstIds, paginationExists: distinctFirstIds > 1 }));
}
