/**
 * One coordinated Invo request budget for the whole executor process.
 *
 * Before this module the feed poller and the elite direct watcher each managed their own
 * (or no) pacing against the *same* Invo account quota. A direct-watch hydration burst
 * could therefore consume the account's rate allowance and 429 the feed — the primary
 * shadow admission path since the feed-primary admission fix — while direct-watch's own
 * token bucket still reported itself healthy. Reconciliation traffic starving the primary
 * signal path is a correctness problem, not a tuning problem: a missed feed NEW/ADD is a
 * missed prospective observation that no later reconciliation can make causal again.
 *
 * Guarantees, in the order they matter:
 *
 * 1. **One bucket.** Every Invo request issued by this process is charged to a single
 *    token bucket, so the configured ceiling is the real account-wide footprint rather
 *    than a per-subsystem ceiling that silently sums.
 * 2. **Feed priority is structural, not advisory.** `DIRECT_WATCH` may only take a token
 *    while the bucket holds more than `feedReservedRequestsPerSecond` tokens, and it
 *    yields outright while any `FEED` acquirer is waiting. The reserved tokens are
 *    therefore unreachable by reconciliation traffic: a feed request can only ever wait
 *    behind *feed* requests, never behind direct-watch. That is a structural
 *    non-starvation proof, independent of arrival order, concurrency or backlog size.
 * 3. **Adaptive 429 handling.** A 429 honours a server `Retry-After` when present and
 *    otherwise escalates exponentially over consecutive rejections; it also halves the
 *    sustained rate (multiplicative decrease) and recovers it additively over quiet
 *    windows. The offending class serves the full cooldown; the peer class serves the
 *    full cooldown when the *feed* was rejected (the account is provably limited) but
 *    only `feedCooldownShare` of it when direct-watch was rejected, so reconciliation
 *    overconsumption cannot silence the primary path for the full penalty.
 * 4. **No request is issued into a known rejection.** While a class is in cooldown,
 *    `acquire` throws instead of spending a token, which removes traffic that is certain
 *    to be rejected.
 *
 * `minRequestsPerSecond` is the AIMD floor. Every static bound that must hold even while
 * the budget is degraded (the direct-watch loop watchdog) is computed from that floor,
 * never from the configured maximum.
 */

export const INVO_REQUEST_BUDGET_VERSION = 'lane3-invo-coordinated-request-budget-v1-20260924';

export type InvoRequestClass = 'FEED' | 'DIRECT_WATCH';

/** Ordered by priority: the feed is the primary shadow admission path. */
export const INVO_REQUEST_CLASSES: readonly InvoRequestClass[] = ['FEED', 'DIRECT_WATCH'];
export const INVO_PRIMARY_REQUEST_CLASS: InvoRequestClass = 'FEED';

export interface InvoRequestBudgetConfig {
  /** Account-wide sustained ceiling shared by every class. */
  maxRequestsPerSecond: number;
  burst: number;
  /** Tokens direct-watch may never consume, so the feed cannot be starved. */
  feedReservedRequestsPerSecond: number;
  /** AIMD floor. Static worst-case bounds must be derived from this, not from the max. */
  minRequestsPerSecond: number;
  baseCooldownMs: number;
  maxCooldownMs: number;
  /** Hard ceiling on an honoured server `Retry-After`, so a poisoned header cannot freeze the loop. */
  maxRetryAfterCooldownMs: number;
  /** Share of a direct-watch 429 cooldown the feed serves. */
  feedCooldownShare: number;
  rateDecreaseFactor: number;
  rateRecoveryStepPerSecond: number;
  rateRecoveryIntervalMs: number;
  /** Quiet window after which consecutive-429 escalation resets. */
  consecutiveDecayMs: number;
  /** Bounded per-request wait, so a saturated budget fails closed instead of hanging. */
  maxAcquireWaitMs: number;
}

export interface InvoRequestClassMetrics {
  requests: number;
  unbudgetedRequests: number;
  waits: number;
  waitMs: number;
  maxWaitMs: number;
  reserveYields: number;
  cooldownRejections: number;
  waitExceeded: number;
  http429s: number;
}

export interface InvoRequestBudgetStatus {
  version: string;
  coordinated: true;
  primaryClass: InvoRequestClass;
  maxRequestsPerSecond: number;
  minRequestsPerSecond: number;
  effectiveRequestsPerSecond: number;
  burst: number;
  availableTokens: number;
  reservedForFeedRequestsPerSecond: number;
  directWatchUsableRequestsPerSecond: number;
  degradedDirectWatchUsableRequestsPerSecond: number;
  maxAcquireWaitMs: number;
  feedCooldownShare: number;
  consecutive429s: number;
  total429s: number;
  retryAfterHonoredCount: number;
  rateReductions: number;
  rateRecoveries: number;
  feedWaiters: number;
  cooldownUntilMs: Record<InvoRequestClass, number>;
  cooldownRemainingMs: Record<InvoRequestClass, number>;
  classes: Record<InvoRequestClass, InvoRequestClassMetrics>;
  /** Feed priority is only healthy while the feed is provably not the throttled class. */
  feedPriorityHealthy: boolean;
  feedPriorityFailures: string[];
  feedStarvationGuard: string;
}

export interface InvoRateLimitDecision {
  requestClass: InvoRequestClass;
  cooldownMs: number;
  cooldownUntilMs: number;
  peerClass: InvoRequestClass;
  peerCooldownUntilMs: number;
  effectiveRequestsPerSecond: number;
  consecutive429s: number;
  retryAfterHonored: boolean;
}

export interface InvoBudgetRateLimitError extends Error {
  status: 429;
  /** Marks a locally generated rejection so callers never escalate their own cooldown. */
  budgetCooldown: true;
  requestClass: InvoRequestClass;
  cooldownUntilMs: number;
  reason: 'class_cooldown' | 'budget_wait_exceeded';
}

interface QueuedRequest {
  requestClass: InvoRequestClass;
  startedAtMs: number;
  resolve: () => void;
  reject: (error: InvoBudgetRateLimitError) => void;
}

function emptyMetrics(): InvoRequestClassMetrics {
  return {
    requests: 0, unbudgetedRequests: 0, waits: 0, waitMs: 0, maxWaitMs: 0,
    reserveYields: 0, cooldownRejections: 0, waitExceeded: 0, http429s: 0,
  };
}

function budgetError(
  requestClass: InvoRequestClass, cooldownUntilMs: number,
  reason: InvoBudgetRateLimitError['reason'], message: string,
): InvoBudgetRateLimitError {
  const error = new Error(message) as InvoBudgetRateLimitError;
  error.name = 'InvoBudgetRateLimitError';
  error.status = 429;
  error.budgetCooldown = true;
  error.requestClass = requestClass;
  error.cooldownUntilMs = cooldownUntilMs;
  error.reason = reason;
  return error;
}

export class InvoRequestBudget {
  private tokens: number;
  private lastRefillMs: number;
  private effectiveRate: number;
  private readonly cooldowns: Record<InvoRequestClass, number> = { FEED: 0, DIRECT_WATCH: 0 };
  private readonly metrics: Record<InvoRequestClass, InvoRequestClassMetrics> = {
    FEED: emptyMetrics(), DIRECT_WATCH: emptyMetrics(),
  };
  private readonly queues: Record<InvoRequestClass, QueuedRequest[]> = { FEED: [], DIRECT_WATCH: [] };
  private pumping = false;
  private consecutive429s = 0;
  private last429AtMs = 0;
  private lastRecoveryAtMs: number;
  private total429sCount = 0;
  private retryAfterHonoredCount = 0;
  private rateReductions = 0;
  private rateRecoveries = 0;

  constructor(
    readonly config: InvoRequestBudgetConfig,
    private readonly now: () => number = Date.now,
    private readonly sleep: (ms: number) => Promise<void> = ms => new Promise(resolve => setTimeout(resolve, ms)),
  ) {
    const c = config;
    if (!(c.maxRequestsPerSecond > 0)) throw new Error('invo request budget requires a positive maxRequestsPerSecond');
    if (!(c.minRequestsPerSecond > 0) || c.minRequestsPerSecond > c.maxRequestsPerSecond) {
      throw new Error('invo request budget requires 0 < minRequestsPerSecond <= maxRequestsPerSecond');
    }
    if (!(c.feedReservedRequestsPerSecond >= 0)) throw new Error('invo request budget requires a non-negative feed reserve');
    // Direct-watch must retain at least one request per second even at the AIMD floor,
    // otherwise a degraded budget silently becomes a permanent reconciliation outage.
    if (c.minRequestsPerSecond - c.feedReservedRequestsPerSecond < 1) {
      throw new Error('invo request budget feed reserve leaves direct-watch below 1 request/second at the rate floor');
    }
    if (!(c.burst >= c.feedReservedRequestsPerSecond + 1)) {
      throw new Error('invo request budget burst cannot satisfy the feed reserve plus one request');
    }
    if (!(c.baseCooldownMs > 0) || c.maxCooldownMs < c.baseCooldownMs) throw new Error('invalid invo request budget cooldown bounds');
    if (c.maxRetryAfterCooldownMs < c.maxCooldownMs) throw new Error('invalid invo request budget retry-after ceiling');
    if (!(c.feedCooldownShare > 0) || c.feedCooldownShare > 1) throw new Error('invalid invo request budget feed cooldown share');
    if (!(c.rateDecreaseFactor > 0) || c.rateDecreaseFactor >= 1) throw new Error('invalid invo request budget rate decrease factor');
    if (!(c.rateRecoveryStepPerSecond > 0) || !(c.rateRecoveryIntervalMs > 0)) throw new Error('invalid invo request budget recovery policy');
    if (!(c.consecutiveDecayMs > 0)) throw new Error('invalid invo request budget 429 decay window');
    if (!(c.maxAcquireWaitMs > 0)) throw new Error('invalid invo request budget wait bounds');
    this.tokens = c.burst;
    this.effectiveRate = c.maxRequestsPerSecond;
    this.lastRefillMs = this.now();
    this.lastRecoveryAtMs = this.lastRefillMs;
  }

  /** Refill against the rate that was in force, then apply additive recovery and 429 decay. */
  private advance(atMs: number) {
    const elapsedMs = Math.max(0, atMs - this.lastRefillMs);
    this.tokens = Math.min(this.config.burst, this.tokens + elapsedMs * this.effectiveRate / 1000);
    this.lastRefillMs = atMs;
    if (this.consecutive429s > 0 && this.last429AtMs > 0
      && atMs - this.last429AtMs >= this.config.consecutiveDecayMs) {
      this.consecutive429s = 0;
    }
    const quietSinceMs = Math.max(this.last429AtMs, this.lastRecoveryAtMs);
    if (this.effectiveRate < this.config.maxRequestsPerSecond
      && atMs - quietSinceMs >= this.config.rateRecoveryIntervalMs) {
      this.effectiveRate = Math.min(
        this.config.maxRequestsPerSecond, this.effectiveRate + this.config.rateRecoveryStepPerSecond,
      );
      this.lastRecoveryAtMs = atMs;
      this.rateRecoveries += 1;
    }
  }

  private queued(): number {
    return this.queues.FEED.length + this.queues.DIRECT_WATCH.length;
  }

  private grant(entry: QueuedRequest, atMs: number) {
    const metrics = this.metrics[entry.requestClass];
    metrics.requests += 1;
    const waitMs = Math.max(0, atMs - entry.startedAtMs);
    if (waitMs > 0) {
      metrics.waits += 1;
      metrics.waitMs += waitMs;
      metrics.maxWaitMs = Math.max(metrics.maxWaitMs, waitMs);
    }
    entry.resolve();
  }

  /**
   * Serves queued requests in strict priority order, FIFO within a class.
   *
   * FIFO within a class matters as much as priority across classes: a poll-and-retry
   * design lets whichever waiter happens to compute the shortest sleep overtake its peers,
   * which starves individual direct-watch targets at high resident counts even though the
   * aggregate rate is fine. One ordered queue makes each waiter's delay bounded by the
   * work already ahead of it.
   */
  private serve(atMs: number) {
    const feedActive = this.queues.FEED.length > 0;
    for (const requestClass of INVO_REQUEST_CLASSES) {
      const queue = this.queues[requestClass];
      const isPrimary = requestClass === INVO_PRIMARY_REQUEST_CLASS;
      while (queue.length) {
        if (atMs < this.cooldowns[requestClass]) {
          const entry = queue.shift()!;
          this.metrics[requestClass].cooldownRejections += 1;
          entry.reject(budgetError(requestClass, this.cooldowns[requestClass], 'class_cooldown',
            `invo request budget cooldown for ${requestClass} until ${this.cooldowns[requestClass]}`));
          continue;
        }
        const floorTokens = isPrimary ? 0 : this.config.feedReservedRequestsPerSecond;
        if (this.tokens < 1 + floorTokens) {
          // Observability for the priority contract: reconciliation stood still while the
          // primary path was drawing on the same budget.
          if (!isPrimary && feedActive) this.metrics[requestClass].reserveYields += 1;
          break;
        }
        // Strict priority. The primary pass above already drains every servable feed
        // request, so this is a belt-and-braces guarantee that reconciliation can never
        // overtake a queued feed request if that pass order ever changes.
        if (!isPrimary && this.queues.FEED.length > 0) {
          this.metrics[requestClass].reserveYields += 1;
          break;
        }
        this.tokens -= 1;
        this.grant(queue.shift()!, atMs);
      }
    }
  }

  /** A saturated budget must fail closed on a bounded wait, never hang a watched loop. */
  private expire(atMs: number) {
    for (const requestClass of INVO_REQUEST_CLASSES) {
      const queue = this.queues[requestClass];
      for (let index = queue.length - 1; index >= 0; index -= 1) {
        if (atMs - queue[index].startedAtMs < this.config.maxAcquireWaitMs) continue;
        const [entry] = queue.splice(index, 1);
        this.metrics[requestClass].waitExceeded += 1;
        entry.reject(budgetError(requestClass, atMs, 'budget_wait_exceeded',
          `invo request budget wait exceeded ${this.config.maxAcquireWaitMs}ms for ${requestClass}`));
      }
    }
  }

  private nextWakeMs(atMs: number): number {
    const candidates: number[] = [];
    const blocking: InvoRequestClass | null = this.queues.FEED.length ? 'FEED'
      : this.queues.DIRECT_WATCH.length ? 'DIRECT_WATCH' : null;
    if (blocking) {
      const floorTokens = blocking === INVO_PRIMARY_REQUEST_CLASS
        ? 0 : this.config.feedReservedRequestsPerSecond;
      const deficitTokens = Math.max(0, 1 + floorTokens - this.tokens);
      candidates.push(Math.ceil(deficitTokens * 1000 / this.effectiveRate));
    }
    for (const requestClass of INVO_REQUEST_CLASSES) {
      const head = this.queues[requestClass][0];
      if (!head) continue;
      candidates.push(Math.max(0, head.startedAtMs + this.config.maxAcquireWaitMs - atMs));
    }
    return Math.max(1, Math.min(...candidates));
  }

  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    try {
      while (this.queued() > 0) {
        const atMs = this.now();
        this.advance(atMs);
        this.serve(atMs);
        this.expire(atMs);
        if (this.queued() === 0) return;
        await this.sleep(this.nextWakeMs(this.now()));
      }
    } finally {
      this.pumping = false;
    }
  }

  /**
   * Charge one request of `requestClass`, waiting in its class queue for capacity. Rejects
   * with a 429-shaped `InvoBudgetRateLimitError` while that class is in cooldown, or once
   * the bounded wait is exhausted, so callers fail closed rather than stalling a loop.
   */
  acquire(requestClass: InvoRequestClass): Promise<void> {
    const startedAtMs = this.now();
    if (startedAtMs < this.cooldowns[requestClass]) {
      this.metrics[requestClass].cooldownRejections += 1;
      return Promise.reject(budgetError(requestClass, this.cooldowns[requestClass], 'class_cooldown',
        `invo request budget cooldown for ${requestClass} until ${this.cooldowns[requestClass]}`));
    }
    return new Promise<void>((resolve, reject) => {
      this.queues[requestClass].push({ requestClass, startedAtMs, resolve, reject });
      // Serve inline before touching the pump. A pump already asleep on the direct-watch
      // head's refill deficit must not make an arriving feed request wait for that wake-up
      // when a token is available right now — that would reintroduce exactly the
      // reconciliation-induced delay on the primary path this budget exists to remove.
      const atMs = this.now();
      this.advance(atMs);
      this.serve(atMs);
      if (this.queued() > 0) void this.pump();
    });
  }

  /**
   * Charge a request that must not be gated: the Invo access-token refresh is a hard
   * precondition of every other request, so blocking it behind a cooldown would deadlock
   * the class that needs it. It is still charged so the reported footprint stays truthful.
   */
  chargeUnbudgeted(requestClass: InvoRequestClass): void {
    const atMs = this.now();
    this.advance(atMs);
    this.tokens = Math.max(0, this.tokens - 1);
    this.metrics[requestClass].unbudgetedRequests += 1;
  }

  /**
   * Record an observed Invo 429 and return the cooldown it produced. `retryAfterMs` is
   * the server-stated delay when one was parsed, and is honoured in preference to the
   * local exponential escalation (bounded by `maxRetryAfterCooldownMs`).
   */
  note429(
    requestClass: InvoRequestClass, retryAfterMs: number | null, atMs: number = this.now(),
  ): InvoRateLimitDecision {
    this.advance(atMs);
    if (this.consecutive429s > 0 && this.last429AtMs > 0
      && atMs - this.last429AtMs >= this.config.consecutiveDecayMs) {
      this.consecutive429s = 0;
    }
    this.consecutive429s += 1;
    this.last429AtMs = atMs;
    this.total429sCount += 1;
    this.metrics[requestClass].http429s += 1;
    const exponentialMs = Math.min(
      this.config.maxCooldownMs,
      this.config.baseCooldownMs * 2 ** Math.min(16, this.consecutive429s - 1),
    );
    const observedMs = retryAfterMs != null && Number.isFinite(retryAfterMs) && retryAfterMs > 0
      ? Math.min(this.config.maxRetryAfterCooldownMs, Math.max(this.config.baseCooldownMs, retryAfterMs))
      : null;
    if (observedMs != null) this.retryAfterHonoredCount += 1;
    const cooldownMs = Math.max(observedMs ?? 0, exponentialMs);
    // Multiplicative decrease: the configured ceiling was demonstrably too high for the
    // account right now, so stop treating it as proven until quiet windows earn it back.
    const reducedRate = Math.max(this.config.minRequestsPerSecond, this.effectiveRate * this.config.rateDecreaseFactor);
    if (reducedRate < this.effectiveRate) this.rateReductions += 1;
    this.effectiveRate = reducedRate;
    this.tokens = 0;
    const peerClass: InvoRequestClass = requestClass === 'FEED' ? 'DIRECT_WATCH' : 'FEED';
    // A rejected feed request proves the account is limited, so reconciliation serves the
    // full penalty. A rejected direct-watch request must not silence the primary path for
    // the full penalty, so the feed serves only its share.
    const peerCooldownMs = requestClass === 'FEED'
      ? cooldownMs : Math.ceil(cooldownMs * this.config.feedCooldownShare);
    this.cooldowns[requestClass] = Math.max(this.cooldowns[requestClass], atMs + cooldownMs);
    this.cooldowns[peerClass] = Math.max(this.cooldowns[peerClass], atMs + peerCooldownMs);
    // Requests already queued for a class that just entered cooldown are rejected now
    // rather than at the pump's next wake, so callers fail closed without extra latency.
    if (this.queued() > 0) {
      this.serve(atMs);
      this.expire(atMs);
    }
    return {
      requestClass, cooldownMs, cooldownUntilMs: this.cooldowns[requestClass],
      peerClass, peerCooldownUntilMs: this.cooldowns[peerClass],
      effectiveRequestsPerSecond: this.effectiveRate,
      consecutive429s: this.consecutive429s,
      retryAfterHonored: observedMs != null,
    };
  }

  cooldownUntilMs(requestClass: InvoRequestClass): number {
    return this.cooldowns[requestClass];
  }

  inCooldown(requestClass: InvoRequestClass, atMs: number = this.now()): boolean {
    return atMs < this.cooldowns[requestClass];
  }

  get effectiveRequestsPerSecond(): number {
    return this.effectiveRate;
  }

  /** Direct-watch's usable steady rate once the feed reserve is withheld. */
  get directWatchUsableRequestsPerSecond(): number {
    return Math.max(0, this.effectiveRate - this.config.feedReservedRequestsPerSecond);
  }

  /** The same figure at the AIMD floor: the value static worst-case bounds must use. */
  get degradedDirectWatchUsableRequestsPerSecond(): number {
    return Math.max(0, this.config.minRequestsPerSecond - this.config.feedReservedRequestsPerSecond);
  }

  status(atMs: number = this.now()): InvoRequestBudgetStatus {
    const feed = this.metrics.FEED;
    const feedPriorityFailures: string[] = [];
    if (this.config.feedReservedRequestsPerSecond < 1) feedPriorityFailures.push('feed_reserve_not_configured');
    if (feed.waitExceeded > 0) feedPriorityFailures.push('feed_budget_wait_exceeded');
    if (this.cooldowns.FEED > this.cooldowns.DIRECT_WATCH) feedPriorityFailures.push('feed_cooldown_exceeds_direct_watch');
    if (feed.maxWaitMs >= this.config.maxAcquireWaitMs) feedPriorityFailures.push('feed_wait_reached_bound');
    return {
      version: INVO_REQUEST_BUDGET_VERSION,
      coordinated: true,
      primaryClass: INVO_PRIMARY_REQUEST_CLASS,
      maxRequestsPerSecond: this.config.maxRequestsPerSecond,
      minRequestsPerSecond: this.config.minRequestsPerSecond,
      effectiveRequestsPerSecond: this.effectiveRate,
      burst: this.config.burst,
      availableTokens: this.tokens,
      reservedForFeedRequestsPerSecond: this.config.feedReservedRequestsPerSecond,
      directWatchUsableRequestsPerSecond: this.directWatchUsableRequestsPerSecond,
      degradedDirectWatchUsableRequestsPerSecond: this.degradedDirectWatchUsableRequestsPerSecond,
      maxAcquireWaitMs: this.config.maxAcquireWaitMs,
      feedCooldownShare: this.config.feedCooldownShare,
      consecutive429s: this.consecutive429s,
      total429s: this.total429sCount,
      retryAfterHonoredCount: this.retryAfterHonoredCount,
      rateReductions: this.rateReductions,
      rateRecoveries: this.rateRecoveries,
      feedWaiters: this.queues.FEED.length,
      cooldownUntilMs: { ...this.cooldowns },
      cooldownRemainingMs: {
        FEED: Math.max(0, this.cooldowns.FEED - atMs),
        DIRECT_WATCH: Math.max(0, this.cooldowns.DIRECT_WATCH - atMs),
      },
      classes: { FEED: { ...this.metrics.FEED }, DIRECT_WATCH: { ...this.metrics.DIRECT_WATCH } },
      feedPriorityHealthy: feedPriorityFailures.length === 0,
      feedPriorityFailures,
      feedStarvationGuard: 'direct_watch_may_not_consume_reserved_feed_tokens_or_overtake_a_waiting_feed_request',
    };
  }
}

/** Parses an Invo/HTTP `Retry-After` (delta-seconds or HTTP-date) into milliseconds. */
export function parseRetryAfterMs(value: unknown, nowMs: number): number | null {
  if (value == null) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds)) return seconds > 0 ? Math.round(seconds * 1000) : null;
  const dateMs = Date.parse(raw);
  if (!Number.isFinite(dateMs)) return null;
  const deltaMs = dateMs - nowMs;
  return deltaMs > 0 ? deltaMs : null;
}
