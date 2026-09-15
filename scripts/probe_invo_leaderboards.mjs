#!/usr/bin/env node

const BASE = 'https://api.invoapp.com';
const APP_HEADERS = { 'x-app-version': '0.0.75', 'x-platform': 'web' };
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
  if (!refresh) {
    if (access) return;
    throw new Error('No Invo credential');
  }
  const r = await fetch(`${BASE}/v1_0/auth/refresh_token`, {
    headers: { Authorization: `Bearer ${refresh}`, ...APP_HEADERS },
    signal: AbortSignal.timeout(5000),
  });
  if (!r.ok) throw new Error(`refresh ${r.status}`);
  const data = await r.json();
  access = String(data.accessToken || '').replace(/^Bearer\s+/i, '');
  if (!access) throw new Error('refresh returned no access token');
}
async function post(path, body) {
  await ensureToken();
  try {
    const r = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${access}`, 'content-type': 'application/json', ...APP_HEADERS },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(3000),
    });
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch { data = text.slice(0, 400); }
    return { status: r.status, data };
  } catch (e) {
    return { status: 0, data: String(e) };
  }
}
function itemsOf(data) {
  for (const v of [data?.items, data?.portfolios, data?.data?.items, data?.data?.portfolios]) if (Array.isArray(v)) return v;
  return [];
}
function signature(data) {
  return itemsOf(data).slice(0, 10).map((x, i) => ({
    rank: i + 1,
    id: String(x?.id ?? x?.portfolioId ?? x?._id ?? ''),
    name: x?.name ?? x?.title ?? x?.portfolioName ?? null,
    pnl: x?.percentChange ?? x?.pnlPercent ?? x?.profitLossPercent ?? x?.roi ?? null,
    trades: x?.closedPositions ?? x?.closedTrades ?? x?.tradeCount ?? null,
  }));
}
function sigKey(sig) { return JSON.stringify(sig.map(x => [x.id, x.pnl])); }

const endpoint = '/v1_0/trending/get_portfolios_pl';
const baselineBody = { filter: 'trending', params: { page: 1, size: 10 } };
const baseline = await post(endpoint, baselineBody);
if (baseline.status >= 400 || baseline.status === 0) throw new Error(`baseline failed ${baseline.status}: ${JSON.stringify(baseline.data).slice(0, 400)}`);
const baselineSig = signature(baseline.data);
console.log(JSON.stringify({ type: 'BASELINE', status: baseline.status, responseKeys: Object.keys(baseline.data ?? {}).sort(), itemKeys: Object.keys(itemsOf(baseline.data)[0] ?? {}).sort(), top: baselineSig }));

const probes = [];
for (const value of ['all','trending','1D','1W','1M','1Y','AT']) {
  probes.push({ label: `filter=${value}`, body: { filter: value, params: { page: 1, size: 10 } } });
}
const values = ['1D','1W','1M','1Y','AT'];
for (const key of ['period','timeframe','timeFrame','interval','range','horizon']) {
  for (const value of values) probes.push({ label: `${key}=${value}`, body: { ...baselineBody, [key]: value } });
  for (const value of values) probes.push({ label: `params.${key}=${value}`, body: { filter: 'trending', params: { page: 1, size: 10, [key]: value } } });
}
for (const [label, days] of [['1D',1],['1W',7],['1M',30],['1Y',365],['AT',0]]) {
  probes.push({ label: `params.days=${days}(${label})`, body: { filter: 'trending', params: { page: 1, size: 10, days } } });
}

const seen = new Set();
let changed = 0;
for (const probe of probes) {
  const bodyKey = JSON.stringify(probe.body);
  if (seen.has(bodyKey)) continue;
  seen.add(bodyKey);
  const result = await post(endpoint, probe.body);
  const sig = signature(result.data);
  const isDifferent = result.status > 0 && result.status < 400 && sig.length > 0 && sigKey(sig) !== sigKey(baselineSig);
  if (isDifferent) changed++;
  console.log(JSON.stringify({
    type: isDifferent ? 'DIFFERENT' : result.status >= 400 || result.status === 0 ? 'REJECTED_OR_TIMEOUT' : 'SAME',
    label: probe.label,
    status: result.status,
    top: isDifferent ? sig : undefined,
    error: result.status >= 400 || result.status === 0 ? result.data : undefined,
  }));
}
console.log(JSON.stringify({ type: 'SUMMARY', probes: seen.size, changed }));
