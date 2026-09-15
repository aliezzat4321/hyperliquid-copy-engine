export interface UnresolvedCloseState {
  signal: Record<string, unknown>;
  firstRejectedAtMs: number;
  lastAttemptAtMs: number;
  attempts: number;
  nextAttemptAtMs: number;
  lastReason: string;
}

export function scheduleUnresolvedClose(
  signal: Record<string, unknown>,
  lastReason: string,
  nowMs: number,
  baseDelayMs: number,
  maxDelayMs: number,
  prior?: UnresolvedCloseState,
): UnresolvedCloseState {
  const attempts = (prior?.attempts ?? 0) + 1;
  const delayMs = Math.min(maxDelayMs, baseDelayMs * (2 ** Math.min(attempts - 1, 10)));
  return {
    signal,
    firstRejectedAtMs: prior?.firstRejectedAtMs ?? nowMs,
    lastAttemptAtMs: nowMs,
    attempts,
    nextAttemptAtMs: nowMs + delayMs,
    lastReason,
  };
}
