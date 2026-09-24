import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

/**
 * Enforced inventory of every Invo API surface and every call site of one.
 *
 * The coordinated request budget only bounds the account footprint if *all* Invo traffic
 * flows through it. That is not a property of the budget module; it is a property of the
 * call sites, and it is exactly the property that silently decays — a new endpoint, a
 * second `getFeed` call, a raw `fetch` "just for this one thing", and the configured
 * ceiling becomes a claim rather than a bound again. So the inventory below is checked
 * against the source in both directions: an unlisted entry point fails, and a listed entry
 * point that disappeared or moved class fails too.
 *
 * Adding an Invo endpoint or call site is therefore a deliberate act that must state which
 * budget class pays for it.
 */

const clientSource = readFileSync(new URL('../../src/invo-client.ts', import.meta.url), 'utf8');
const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
const cliSource = readFileSync(new URL('../../src/portfolio-candidate-cli.ts', import.meta.url), 'utf8');

type BudgetClass =
  /** Charged to the primary class before the request leaves the process. */
  | 'BUDGETED_FEED'
  /** Charged to the reconciliation class, which may never take the feed's reserve. */
  | 'BUDGETED_DIRECT_WATCH'
  /** A hard precondition of every other request: never gated, always charged. */
  | 'UNBUDGETED_AUTH_PRECONDITION'
  /** Unreachable while `NOTIFICATION_TRADER_LIVE=false`; not on any shadow path. */
  | 'LIVE_ONLY'
  /** Issued by a separate short-lived CLI process, not by the paced service. */
  | 'OFFLINE_CLI';

interface EntryPoint {
  budgetClass: BudgetClass;
  /** Why this classification is safe. Read this before changing one. */
  reason: string;
  /** How many times the long-running service may reference it. */
  serviceCallSites: number;
  /** Must appear in the enclosing service function before a LIVE_ONLY call site. */
  liveGateProof?: RegExp;
}

const INVO_ENTRY_POINTS: Record<string, EntryPoint> = {
  getFeed: {
    budgetClass: 'BUDGETED_FEED',
    reason: 'primary shadow admission path; acquires a FEED token before every page',
    serviceCallSites: 1,
  },
  getPortfolioInvestments: {
    budgetClass: 'BUDGETED_DIRECT_WATCH',
    reason: 'reconciliation hydration; yields to the feed and cannot take its reserve',
    serviceCallSites: 1,
  },
  ensureToken: {
    budgetClass: 'UNBUDGETED_AUTH_PRECONDITION',
    reason: 'gating a refresh behind a cooldown deadlocks the class that needs the token',
    // Startup: once as a funding-ordering callback reference, once on the live branch.
    serviceCallSites: 2,
  },
  ensureTokenFreshFor: {
    budgetClass: 'UNBUDGETED_AUTH_PRECONDITION',
    reason: 'proves one token covers a whole direct-watch scan horizon before it starts',
    serviceCallSites: 1,
  },
  checkAccountReady: {
    budgetClass: 'LIVE_ONLY',
    reason: 'real-account readiness probe; runs only on the live startup branch',
    serviceCallSites: 1,
    liveGateProof: /if \(cfg\.live\) \{/,
  },
  recordOpen: {
    budgetClass: 'LIVE_ONLY',
    reason: 'reports a real Hyperliquid fill; reached only after a real order was placed',
    serviceCallSites: 1,
    liveGateProof: /hl\.placeMarketOrder\(/,
  },
  recordClose: {
    budgetClass: 'LIVE_ONLY',
    reason: 'reports a real Hyperliquid close; reached only after a real close was placed',
    serviceCallSites: 1,
    liveGateProof: /hl\.closePosition\(/,
  },
  discoverPortfolios: {
    budgetClass: 'OFFLINE_CLI',
    reason: 'portfolio research CLI runs as its own bounded process, not in the paced service',
    serviceCallSites: 0,
  },
};

/** Every function declaration, exported or not, so a private helper cannot hide inside a slice. */
function functionBodies(source: string): Array<{ name: string; exported: boolean; body: string }> {
  const starts: Array<{ name: string; exported: boolean; index: number }> = [];
  const re = /^(export )?(?:async )?function (\w+)\(/gm;
  let match: RegExpExecArray | null;
  while ((match = re.exec(source)) !== null) {
    starts.push({ name: match[2], exported: match[1] != null, index: match.index });
  }
  return starts.map((entry, index) => ({
    name: entry.name,
    exported: entry.exported,
    body: source.slice(entry.index, index + 1 < starts.length ? starts[index + 1].index : source.length),
  }));
}

/** An exported surface counts as account traffic if it can reach the network at all. */
function issuesAccountTraffic(body: string): boolean {
  return /\bpost\(/.test(body) || /\bfetch\(/.test(body) || /\brefreshAccessToken\(/.test(body);
}

function enclosingBodyBefore(source: string, index: number): string {
  const starts = [...source.slice(0, index).matchAll(/^(?:export )?(?:async )?function (\w+)\(/gm)];
  const last = starts[starts.length - 1];
  return last?.index == null ? source.slice(0, index) : source.slice(last.index, index);
}

test('the Invo client exposes exactly the inventoried account-traffic surfaces', () => {
  const discovered = functionBodies(clientSource)
    .filter(entry => entry.exported && issuesAccountTraffic(entry.body))
    .map(entry => entry.name)
    .sort();
  assert.deepEqual(discovered, Object.keys(INVO_ENTRY_POINTS).sort(),
    'an Invo surface was added, removed or renamed without declaring which budget class pays for it');
});

test('no Invo request can bypass the single client transport', () => {
  const fetchers = functionBodies(clientSource)
    .filter(entry => /\bfetch\(/.test(entry.body))
    .map(entry => entry.name)
    .sort();
  assert.deepEqual(fetchers, ['post', 'refreshAccessToken'],
    'every endpoint must funnel through post(), which threads the budget hook and request class');
  for (const [name, source] of [['service.ts', serviceSource], ['portfolio-candidate-cli.ts', cliSource]] as const) {
    assert.doesNotMatch(source, /api\.invoapp\.com/,
      `${name} must reach Invo through invo-client, never by its own request`);
  }
});

test('each budgeted surface forwards both the budget hook and its request class', () => {
  const bodies = new Map(functionBodies(clientSource).map(entry => [entry.name, entry.body]));
  for (const [name, entry] of Object.entries(INVO_ENTRY_POINTS)) {
    const body = bodies.get(name);
    assert.ok(body, `${name} is inventoried but no longer defined`);
    if (!entry.budgetClass.startsWith('BUDGETED_')) continue;
    assert.match(body, /beforeRequest\?: \(\) => Promise<void>/,
      `${name} must accept the budget acquisition hook`);
    assert.match(body, /requestClass: InvoRequestClass/,
      `${name} must accept the class its traffic is charged to`);
    assert.match(body, /beforeRequest,[^)]*requestClass\)/,
      `${name} must forward both to post(), or the token is spent outside the budget`);
  }
});

test('an auth refresh is attributed to the request that triggered it, not to process state', () => {
  // Both ingestion loops run concurrently, so a module-level "current class" is attributed
  // by whichever loop wrote it last. That never mis-gated anything — refreshes are
  // deliberately ungated — but it made the per-class footprint metric untrue, which is the
  // one thing this subsystem exists to report honestly.
  assert.match(clientSource, /async function refreshAccessToken\(requestClass: InvoRequestClass\)/);
  assert.match(clientSource, /authRequestObserver\?\.\(requestClass\)/,
    'the observer must receive the triggering class');
  assert.doesNotMatch(clientSource, /^let\s+(?:current|active|last)\w*RequestClass/m,
    'a latched request class would reintroduce the cross-loop attribution race');
  for (const name of ['ensureToken', 'ensureTokenFreshFor', 'post']) {
    const body = functionBodies(clientSource).find(entry => entry.name === name)?.body;
    assert.ok(body, `${name} must exist`);
    assert.match(body, /refreshAccessToken\(requestClass\)/,
      `${name} must pass through the caller's class`);
  }
  assert.match(clientSource, /await ensureToken\(requestClass\);/,
    'the pre-request freshness check must charge the calling class');
  assert.match(clientSource,
    /return post\(path, body, true, beforeRequest, allowAuthRetry, requestClass\);/,
    'the 401 retry must keep the original class rather than falling back to the default');
});

test('every service call site of an Invo surface matches its inventoried class', () => {
  const referenced = new Set(
    [...serviceSource.matchAll(/\binvo\.(\w+)/g)].map(match => match[1]),
  );
  for (const name of referenced) {
    if (!(name in INVO_ENTRY_POINTS)) {
      // Non-traffic exports (setToken, the timeout constant) are fine; a traffic surface
      // reaching the service without an inventory entry is not.
      const body = functionBodies(clientSource).find(entry => entry.name === name)?.body;
      assert.ok(body == null || !issuesAccountTraffic(body),
        `service.ts calls un-inventoried Invo surface ${name}`);
    }
  }
  for (const [name, entry] of Object.entries(INVO_ENTRY_POINTS)) {
    const sites = [...serviceSource.matchAll(new RegExp(`\\binvo\\.${name}\\b`, 'g'))];
    assert.equal(sites.length, entry.serviceCallSites,
      `service.ts has ${sites.length} references to invo.${name}, inventory declares ${entry.serviceCallSites}`);
    if (entry.budgetClass !== 'LIVE_ONLY') continue;
    for (const site of sites) {
      assert.match(enclosingBodyBefore(serviceSource, site.index!), entry.liveGateProof!,
        `invo.${name} must stay unreachable on every shadow path`);
    }
  }
});

test('the budgeted call sites name their class explicitly at the call', () => {
  assert.match(serviceSource,
    /invo\.getFeed\(feedFilter, lastPostId, cfg\.feedLimit, acquireFeedRequestBudget, 'FEED'\)/,
    'the only feed read must acquire a FEED token and be charged to FEED');
  assert.match(serviceSource,
    /invo\.getPortfolioInvestments\([\s\S]*?invoRequestBudget\.acquire\('DIRECT_WATCH'\)[\s\S]*?\}, false, 'DIRECT_WATCH'\)/,
    'the only direct-watch read must acquire a DIRECT_WATCH token and be charged to DIRECT_WATCH');
  assert.match(serviceSource, /ensureTokenFreshFor\([\s\S]*?, 'DIRECT_WATCH'\);/,
    "a direct-watch scan's preflight refresh must not be billed to the feed");
});

test('the research CLI is the only discovery caller and is not part of the paced service', () => {
  assert.match(cliSource, /invo\.discoverPortfolios\(/);
  assert.doesNotMatch(serviceSource, /portfolio-candidate-cli/,
    'discovery must stay in its own bounded process rather than sharing the service loops');
  assert.doesNotMatch(cliSource, /InvoRequestBudget/,
    'the CLI must not construct a second bucket against the same account quota');
});
