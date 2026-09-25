import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');

test('exactly one coordinated Invo request budget paces both ingestion paths', () => {
  const constructions = source.match(/new InvoRequestBudget\(/g) ?? [];
  assert.equal(constructions.length, 1, 'a second bucket would make the configured ceiling a lie');
  assert.doesNotMatch(source, /DirectWatchRequestBudget/,
    'the per-subsystem direct-watch bucket must not come back');
  assert.match(source, /maxRequestsPerSecond: cfg\.invoRequestBudgetPerSecond/);
  assert.match(source, /feedReservedRequestsPerSecond: cfg\.invoFeedReservedPerSecond/);
  assert.match(source, /minRequestsPerSecond: cfg\.invoMinRequestBudgetPerSecond/);
});

test('every feed and direct-watch Invo request is charged to the shared budget', () => {
  assert.match(source, /async function acquireFeedRequestBudget\(\)[\s\S]*?invoRequestBudget\.acquire\('FEED'\)/,
    'feed page reads must acquire from the shared budget');
  assert.match(source, /invo\.getFeed\(feedFilter, lastPostId, cfg\.feedLimit, acquireFeedRequestBudget, 'FEED'\)/,
    'the backfill fetcher must be the budgeted one');
  const getBudgeted = source.indexOf('async function getBudgetedDirectInvestments(');
  const hydrate = source.indexOf('async function hydrateDirectTarget(');
  assert.ok(getBudgeted >= 0 && hydrate > getBudgeted);
  assert.match(source.slice(getBudgeted, hydrate), /invoRequestBudget\.acquire\('DIRECT_WATCH'\)/,
    'direct-watch hydration must draw from the lower-priority class of the same budget');
  const observer = source.indexOf('invo.setAuthRequestObserver({');
  assert.ok(observer >= 0);
  const observerBody = source.slice(observer, source.indexOf('});', observer));
  assert.match(observerBody, /charge: requestClass => \{[\s\S]*?invoRequestBudget\.chargeUnbudgeted\(requestClass\)/,
    'token refreshes must be counted against the account footprint without being gated');
  assert.match(observerBody,
    /rateLimited: \(requestClass, retryAfterMs\) => \{[\s\S]*?applyDirectWatchRateLimit\(Date\.now\(\), retryAfterMs, 'auth_refresh'\)[\s\S]*?applyFeedRateLimit\(Date\.now\(\), retryAfterMs, 'auth_refresh'\)/,
    'a 429 on the ungated refresh must still adapt the budget, in the class that paid for it');
  assert.doesNotMatch(source, /invoAuthRequestClass/,
    'a latched auth class would mis-attribute a refresh triggered by the concurrent peer loop');
});

test('a feed 429 gates reconciliation while a direct-watch 429 only costs the feed its share', () => {
  const apply = source.indexOf('function applyDirectWatchRateLimit(');
  const cooldown = source.indexOf('function directWatchCooldownUntilMs()');
  assert.ok(apply >= 0 && cooldown > apply);
  assert.match(source.slice(apply, cooldown),
    /invoRequestBudget\.note429\('DIRECT_WATCH', retryAfterMs, nowMs\)/,
    'the observed Retry-After must drive the adaptive cooldown');
  assert.match(source.slice(cooldown, cooldown + 400),
    /Math\.max\(directWatchBackoffUntilMs, invoRequestBudget\.cooldownUntilMs\('DIRECT_WATCH'\)\)/,
    'a feed 429 must also gate reconciliation scans');
  const scan = source.indexOf('async function scanEliteDirectWatch(');
  assert.match(source.slice(scan, scan + 300), /if \(nowMs < directWatchCooldownUntilMs\(\)\)/,
    'the scan gate must observe the coordinated cooldown, not only its own backoff');
  assert.match(source, /if \(status === 429 && err\?\.budgetCooldown !== true\) \{[\s\S]*?applyFeedRateLimit\(Date\.now\(\), err\?\.retryAfterMs \?\? null, current\.source\)/,
    'a real feed 429 must adapt the shared budget');
  const applyFeed = source.indexOf('function applyFeedRateLimit(');
  assert.ok(applyFeed >= 0);
  assert.match(source.slice(applyFeed, source.indexOf('\n}', applyFeed)),
    /invoRequestBudget\.note429\('FEED', retryAfterMs, nowMs\)/,
    'every feed-class 429, page read or ungated refresh, adapts one shared budget');
  assert.match(source, /error\?\.status === 429 && error\?\.budgetCooldown !== true && error\?\.cooldownUntilMs == null/,
    'a locally generated cooldown rejection must never escalate the cooldown that produced it');
});

test('feed backoff can never outlast the silence the feed watchdog was armed for', () => {
  assert.match(source, /const FEED_MAX_BACKOFF_MS = 30_000;/);
  assert.match(source, /feedWatchdogLimitMs\(\{ maxBackoffMs: FEED_MAX_BACKOFF_MS/,
    'the watchdog limit and the loop backoff must share one constant');
  assert.match(source, /function feedBackoffFromCooldown\(cooldownUntilMs: number\): number \{\s*return Math\.min\(FEED_MAX_BACKOFF_MS/,
    'a long coordinated cooldown must be served in watchdog-safe slices');
});

test('the direct-watch watchdog bound is taken at the budget rate floor, not the ceiling', () => {
  const directLimit = source.indexOf('watchdogLimits.direct_watch = directWatchdogLimitMs(');
  assert.ok(directLimit >= 0);
  const body = source.slice(directLimit, source.indexOf('});', directLimit));
  assert.match(body, /requestBudgetPerSecond: cfg\.invoMinRequestBudgetPerSecond/,
    'an adaptive rate reduction must not be able to trip a false stall and exit the process');
  assert.doesNotMatch(body, /requestBudgetPerSecond: cfg\.invoRequestBudgetPerSecond/);
});

test('only a rotating duplicate surface poll is suppressed, never a causal read', () => {
  const fetchAndProcess = source.indexOf('async function fetchAndProcess(');
  const wakeFn = source.indexOf('async function wake(');
  const body = source.slice(fetchAndProcess, wakeFn);
  const pendingCloses = body.indexOf('const pendingCloses =');
  const suppression = body.indexOf("source.startsWith('api_poll:')");
  const backfill = body.indexOf('fetchFeedBackfill(');
  assert.ok(pendingCloses >= 0 && suppression > pendingCloses && backfill > suppression,
    'suppression must sit after owned-close reconciliation and before the feed fetch');
  assert.match(body.slice(suppression, backfill), /state\.hasFeedBaseline\(feedFilter\)/,
    'a surface without a durable baseline must always be fetched');
  assert.match(body.slice(suppression, backfill), /sinceLastFetchMs < cfg\.feedMinSurfaceRepollMs/);
  assert.match(body.slice(suppression, backfill), /cursorAdvanced: false/,
    'a suppressed poll must not advance or rebase the durable cursor');
  assert.doesNotMatch(body.slice(suppression, backfill), /markSeen|setFeedCursor/,
    'a suppressed poll must never mark a post seen');
});

test('health reports the coordinated budget and its feed-priority proof', () => {
  const health = source.indexOf("req.url === '/health'");
  const traders = source.indexOf("req.url === '/traders'");
  const body = source.slice(health, traders);
  assert.match(body, /const budgetStatus = invoRequestBudget\.status\(healthNowMs\)/);
  assert.match(body, /invoRequestBudget: \{\s*\.\.\.budgetStatus,/,
    'the whole coordinated budget status must be exposed, including feedPriorityHealthy');
  assert.match(body, /redundantSurfacePollsSuppressed: feedRequestMetrics\.redundantSurfacePollsSuppressed/);
  assert.match(body, /authRefresh: \{ \.\.\.authRefreshMetrics \}/,
    'ungated precondition traffic must stay visible in the reported footprint');
  assert.match(body, /degradedResidentCap: directWatchDegradedCapacity\.provenResidentCap/,
    'residency that survives a degraded budget must be reported, not assumed');
  assert.match(body, /residentCountOversubscribed: directStatus\.targetCount > directWatchConfiguredCapacity\.provenResidentCap/,
    'an uncapped elite population must be reported honestly rather than capped');
  assert.match(body, /healthNowMs >= directWatchCooldownMs && directStatus\.admissionsHealthy/,
    'admission freshness must fail closed while the coordinated budget is in cooldown');
});

test('coordination changes no live-trading, routing or trader-cap invariant', () => {
  assert.match(source, /NOTIFICATION_TRADER_LIVE=true requires REAL_TRADING_ENABLED=YES/);
  assert.match(source, /if \(cfg\.live\) await pollLoop\(\);\s*else await Promise\.all\(\[pollLoop\(\), directWatchLoop\(\)\]\)/,
    'the direct watcher must remain shadow-only');
  const scan = source.indexOf('async function scanEliteDirectWatch(');
  assert.match(source.slice(scan, scan + 200), /if \(cfg\.live\) return;/);
  assert.match(source, /new EliteDirectWatchState\(\s*cfg\.directWatchStatePath, cfg\.directWatchAdmissionIndexPath,\s*Number\.MAX_SAFE_INTEGER,/,
    'no trader cap: every qualified elite stays resident');
  assert.doesNotMatch(source, /api\.hyperliquid|arbitrum|binance|bybit/i);
});
