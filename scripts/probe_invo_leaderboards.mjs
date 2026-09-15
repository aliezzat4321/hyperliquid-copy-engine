#!/usr/bin/env node

const BASE = 'https://api.invoapp.com';
const APP_HEADERS = { 'x-app-version': '0.0.75', 'x-platform': 'web' };
let access = (process.env.INVO_ACCESS_TOKEN || process.env.INVO_TOKEN || '').replace(/^Bearer\s+/i, '');
let refresh = (process.env.INVO_REFRESH_TOKEN || '').replace(/^Bearer\s+/i, '');

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
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
    signal: AbortSignal.timeout(10000),
  });
  if (!r.ok) throw new Error(`refresh ${r.status}`);
  const data = await r.json();
  access = String(data.accessToken || '').replace(/^Bearer\s+/i, '');
  if (!access) throw new Error('refresh returned no access token');
}
async function post(path, body) {
  await ensureToken();
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${access}`, 'content-type': 'application/json', ...APP_HEADERS },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text.slice(0, 500); }
  return { status: r.status, data };
}
function itemsOf(data) {
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data?.portfolios)) return data.portfolios;
  if (Array.isArray(data?.data?.items)) return data.data.items;
  if (Array.isArray(data?.data?.portfolios)) return data.data.portfolios;
  return [];
}
function signature(data) {
  const items = itemsOf(data);
  return items.slice(0, 10).map((x, i) => ({
    r: i + 1,
    id: String(x?.id ?? x?.portfolioId ?? x?._id ?? ''),
    name: x?.name ?? x?.title ?? x?.portfolioName ?? null,
    pnl: x?.percentChange ?? x?.pnlPercent ?? x?.profitLossPercent ?? x?.roi ?? null,
    closed: x?.closedPositions ?? x?.closedTrades ?? null,
  }));
}
function sigKey(sig) { return JSON.stringify(sig.map(x => [x.id, x.pnl])); }

const baselineBody = { filter: 'trending', params: { page: 1, size: 10 } };
const baseline = await post('/v1_0/trending/get_portfolios_pl', baselineBody);
if (baseline.status >= 400) throw new Error(`baseline failed ${baseline.status}: ${JSON.stringify(baseline.data).slice(0, 500)}`);
const baselineSig = signature(baseline.data);
console.log(JSON.stringify({ type: 'BASELINE', body: baselineBody, status: baseline.status, top: baselineSig }));

const probes = [];
const filterValues = ['all','trending','top','leaderboard','daily','weekly','monthly','yearly','alltime','all_time','today','day','week','month','year','1D','1W','1M','1Y','AT','1d','1w','1m','1y','at'];
for (const value of filterValues) probes.push({ label: `filter=${value}`, body: { filter: value, params: { page: 1, size: 10 } } });

const horizonValues = ['1D','1W','1M','1Y','AT','1d','1w','1m','1y','at','day','week','month','year','all','daily','weekly','monthly','yearly','alltime'];
const topKeys = ['period','timeframe','timeFrame','range','interval','horizon','window','plPeriod','pnlPeriod','timePeriod'];
const paramKeys = ['period','timeframe','timeFrame','range','interval','horizon','window','plPeriod','pnlPeriod','timePeriod'];
for (const key of topKeys) {
  for (const value of horizonValues.slice(0, 10)) probes.push({ label: `${key}=${value}`, body: { ...baselineBody, [key]: value } });
}
for (const key of paramKeys) {
  for (const value of horizonValues.slice(0, 10)) probes.push({ label: `params.${key}=${value}`, body: { filter: 'trending', params: { page: 1, size: 10, [key]: value } } });
}
for (const [label, days] of [['1D',1],['1W',7],['1M',30],['1Y',365],['AT',0]]) {
  for (const key of ['days','periodDays','lookbackDays']) probes.push({ label: `params.${key}=${days}(${label})`, body: { filter: 'trending', params: { page: 1, size: 10, [key]: days } } });
}

const seenBodies = new Set();
const interesting = [];
for (const probe of probes) {
  const bodyKey = JSON.stringify(probe.body);
  if (seenBodies.has(bodyKey)) continue;
  seenBodies.add(bodyKey);
  const result = await post('/v1_0/trending/get_portfolios_pl', probe.body);
  const sig = signature(result.data);
  const changed = result.status < 400 && sig.length > 0 && sigKey(sig) !== sigKey(baselineSig);
  const rejected = result.status >= 400;
  if (changed || rejected) {
    const row = { type: changed ? 'DIFFERENT' : 'REJECTED', label: probe.label, body: probe.body, status: result.status, top: sig, error: rejected ? result.data : undefined };
    interesting.push(row);
    console.log(JSON.stringify(row));
  }
  await sleep(80);
}

// Probe likely dedicated read-only leaderboard routes with one conservative body each.
const routes = [
  '/v1_0/trending/get_portfolios',
  '/v1_0/trending/get_top_portfolios',
  '/v1_0/portfolios/get_top_portfolios',
  '/v1_0/portfolios/get_leaderboard',
  '/v1_0/trending/get_leaderboard',
];
for (const path of routes) {
  const result = await post(path, { params: { page: 1, size: 10 }, filter: 'trending' });
  const sig = signature(result.data);
  console.log(JSON.stringify({ type: 'ROUTE_PROBE', path, status: result.status, itemCount: sig.length, top: sig, error: result.status >= 400 ? result.data : undefined }));
  await sleep(100);
}

console.log(JSON.stringify({ type: 'SUMMARY', probes: seenBodies.size, interesting: interesting.length, note: 'Unknown fields may be ignored by API; only ranked-set changes or explicit validation errors are evidence.' }));
