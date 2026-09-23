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
 * Process-local liveness guard for loops whose awaited work can otherwise leave the
 * service alive but inert. The caller owns the timer and fail-fast action so tests can
 * deterministically inspect the boundary without terminating their process.
 */
export class LoopProgressWatchdog {
  private readonly progress = new Map<WatchedLoop, number>();

  constructor(
    private readonly limits: Record<WatchedLoop, number>,
    armedAtMs: number,
  ) {
    for (const loop of Object.keys(limits) as WatchedLoop[]) {
      if (!Number.isFinite(limits[loop]) || limits[loop] <= 0) {
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
      const lastProgressAtMs = this.progress.get(loop) ?? nowMs;
      const silenceMs = Math.max(0, nowMs - lastProgressAtMs);
      return {
        loop,
        armedAtMs: lastProgressAtMs,
        lastProgressAtMs,
        maxSilenceMs: this.limits[loop],
        silenceMs,
        stalled: silenceMs > this.limits[loop],
      };
    });
  }

  firstStall(nowMs: number): LoopWatchdogStatus | null {
    return this.status(nowMs).find(row => row.stalled) ?? null;
  }
}
