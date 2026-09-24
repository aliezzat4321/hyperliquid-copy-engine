export type WatchedLoop = 'feed' | 'direct_watch';

export interface LoopWatchdogStatus {
  loop: WatchedLoop;
  armedAtMs: number;
  lastProgressAtMs: number;
  maxSilenceMs: number;
  silenceMs: number;
  stalled: boolean;
}

/**
 * The feed loop now beats progress per backfill page and per processed signal (see
 * `fetchFeedBackfill`'s `onPage` and `runSignalBatchBySource`'s `onProgress`), so the
 * watchdog only ever needs to bound the silence between two consecutive heartbeats, not
 * the wall time of an entire wake() cycle. That silence window is at most one page fetch
 * or one signal's bounded external-request budget — never the total page/signal count —
 * so a legitimately large backlog (or a future live topology with far more traders/
 * signals per cycle) cannot by itself trip a false stall/restart.
 */
export function feedWatchdogLimitMs(input: {
  maxBackoffMs: number; pollMs: number;
  requestTimeoutMs: number; maxRequestsPerPage: number;
  signalProcessingBudgetMs: number; processingMarginMs: number;
}): number {
  const perPageMs = input.maxRequestsPerPage * input.requestTimeoutMs;
  const perHeartbeatMs = Math.max(perPageMs, input.signalProcessingBudgetMs);
  return input.maxBackoffMs + input.pollMs + perHeartbeatMs + input.processingMarginMs;
}

export function directWatchdogLimitMs(input: {
  openHydrates: number; closedHydrates: number;
  openMaxPages: number; closedMaxPages: number; maxAttemptsPerPage: number;
  concurrency: number; requestTimeoutMs: number;
  requestBudgetPerSecond: number; requestBudgetBurst: number; fixedReserveRequestsPerSecond: number;
  fixedOverheadMs: number; postScanFlushBudgetMs: number; processingMarginMs: number;
}): number {
  const openRequests = input.openHydrates * input.openMaxPages * input.maxAttemptsPerPage;
  const closedRequests = input.closedHydrates * input.closedMaxPages * input.maxAttemptsPerPage;
  const openWallMs = Math.ceil(input.openHydrates / input.concurrency)
    * input.openMaxPages * input.maxAttemptsPerPage * input.requestTimeoutMs;
  const closedWallMs = Math.ceil(input.closedHydrates / input.concurrency)
    * input.closedMaxPages * input.maxAttemptsPerPage * input.requestTimeoutMs;
  const usableRate = input.requestBudgetPerSecond - input.fixedReserveRequestsPerSecond;
  if (usableRate <= 0) throw new Error('direct-watch watchdog requires positive request budget');
  const budgetWaitMs = Math.ceil(Math.max(0,
    openRequests + closedRequests - input.requestBudgetBurst) / usableRate * 1000);
  return input.fixedOverheadMs + openWallMs + closedWallMs + budgetWaitMs
    + input.postScanFlushBudgetMs + input.processingMarginMs;
}

/**
 * Process-local liveness guard for loops whose awaited work can otherwise leave the
 * service alive but inert. The caller owns the timer and fail-fast action so tests can
 * deterministically inspect the boundary without terminating their process.
 */
export class LoopProgressWatchdog {
  private readonly progress = new Map<WatchedLoop, number>();

  constructor(
    private readonly limits: Partial<Record<WatchedLoop, number>>,
    armedAtMs: number,
  ) {
    for (const loop of Object.keys(limits) as WatchedLoop[]) {
      const limit = limits[loop] as number;
      if (!Number.isFinite(limit) || limit <= 0) {
        throw new Error(`invalid ${loop} watchdog limit`);
      }
      this.progress.set(loop, armedAtMs);
    }
  }

  beat(loop: WatchedLoop, atMs: number) {
    if (!Number.isFinite(atMs)) throw new Error(`invalid ${loop} watchdog heartbeat`);
    this.progress.set(loop, atMs);
  }

  status(nowMs: number): LoopWatchdogStatus[] {
    return (Object.keys(this.limits) as WatchedLoop[]).map(loop => {
      const maxSilenceMs = this.limits[loop] as number;
      const lastProgressAtMs = this.progress.get(loop) ?? nowMs;
      const silenceMs = Math.max(0, nowMs - lastProgressAtMs);
      return {
        loop,
        armedAtMs: lastProgressAtMs,
        lastProgressAtMs,
        maxSilenceMs,
        silenceMs,
        stalled: silenceMs > maxSilenceMs,
      };
    });
  }

  firstStall(nowMs: number): LoopWatchdogStatus | null {
    return this.status(nowMs).find(row => row.stalled) ?? null;
  }
}
