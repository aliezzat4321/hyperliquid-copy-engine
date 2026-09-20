import { isMainThread, parentPort, workerData } from 'node:worker_threads';
import {
  FUNDING_INTERVAL_MS,
  type FundingOracleAttempt,
  type FundingOracleCaptureResult,
  type FundingOracleWorkerOptions,
} from './funding-oracle-capture.js';

const DEFAULT_ENDPOINT = 'https://api.hyperliquid.xyz/info';

function errorClass(error: unknown): string {
  if (error instanceof DOMException && error.name === 'TimeoutError') return 'timeout';
  if (error instanceof TypeError) return 'network';
  const message = error instanceof Error ? error.message : String(error);
  const match = /^Hyperliquid info HTTP (\d+)/.exec(message);
  return match ? `http_${match[1]}` : 'unexpected';
}

export function parseOraclePrices(payload: unknown): Record<string, number> {
  if (!Array.isArray(payload) || payload.length < 2) throw new Error('Invalid Hyperliquid metaAndAssetCtxs payload');
  const [meta, contexts] = payload;
  if (!Array.isArray(meta?.universe) || !Array.isArray(contexts)) {
    throw new Error('Invalid Hyperliquid metaAndAssetCtxs shape');
  }
  const prices: Record<string, number> = {};
  for (let index = 0; index < meta.universe.length; index += 1) {
    const coin = String(meta.universe[index]?.name ?? '');
    const oraclePx = Number(contexts[index]?.oraclePx);
    if (coin && Number.isFinite(oraclePx) && oraclePx > 0) prices[coin] = oraclePx;
  }
  return prices;
}

export class FundingBoundaryCapturer {
  private readonly inFlight = new Map<number, Promise<FundingOracleCaptureResult>>();

  constructor(
    private readonly options: Required<Pick<FundingOracleWorkerOptions, 'maxDelayMs' | 'retryBaseMs' | 'endpoint'>>,
    private readonly now: () => number = Date.now,
    private readonly sleep: (delayMs: number) => Promise<void> = delayMs => new Promise(resolve => setTimeout(resolve, delayMs)),
    private readonly request: (endpoint: string, timeoutMs: number) => Promise<Record<string, number>> = requestOraclePrices,
  ) {}

  capture(fundingTimeMs: number): Promise<FundingOracleCaptureResult> {
    const existing = this.inFlight.get(fundingTimeMs);
    if (existing) return existing;
    const pending = this.captureUnlocked(fundingTimeMs).finally(() => this.inFlight.delete(fundingTimeMs));
    this.inFlight.set(fundingTimeMs, pending);
    return pending;
  }

  private async captureUnlocked(fundingTimeMs: number): Promise<FundingOracleCaptureResult> {
    const deadlineMs = fundingTimeMs + this.options.maxDelayMs;
    const attempts: FundingOracleAttempt[] = [];
    if (this.now() > deadlineMs) return this.failure(fundingTimeMs, attempts, 'startup_after_deadline');

    let lastError = '';
    while (this.now() <= deadlineMs) {
      const requestedAtMs = this.now();
      try {
        const oraclePrices = await this.request(this.options.endpoint, Math.max(1, deadlineMs - requestedAtMs));
        const completedAtMs = this.now();
        attempts.push({ requestedAtMs, completedAtMs, requestLatencyMs: completedAtMs - requestedAtMs, outcome: 'success' });
        if (completedAtMs <= deadlineMs) {
          return {
            type: 'funding_oracle_result', fundingTimeMs, attempts,
            retryCount: Math.max(0, attempts.length - 1), finalObservedAtMs: completedAtMs,
            finalDelayMs: completedAtMs - fundingTimeMs, oraclePrices,
          };
        }
        lastError = 'oracle response completed after strict deadline';
      } catch (error) {
        const completedAtMs = this.now();
        lastError = error instanceof Error ? error.message : String(error);
        attempts.push({
          requestedAtMs, completedAtMs, requestLatencyMs: completedAtMs - requestedAtMs,
          outcome: 'error', errorClass: errorClass(error),
        });
      }
      const remainingMs = deadlineMs - this.now();
      if (remainingMs <= 0) break;
      const retryDelayMs = Math.min(remainingMs, this.options.retryBaseMs * 2 ** Math.min(4, attempts.length - 1));
      await this.sleep(retryDelayMs);
    }
    return this.failure(fundingTimeMs, attempts, 'deadline_exhausted', lastError);
  }

  private failure(
    fundingTimeMs: number,
    attempts: FundingOracleAttempt[],
    failureClass: FundingOracleCaptureResult['failureClass'],
    error?: string,
  ): FundingOracleCaptureResult {
    const finalObservedAtMs = this.now();
    return {
      type: 'funding_oracle_result', fundingTimeMs, attempts,
      retryCount: Math.max(0, attempts.length - 1), finalObservedAtMs,
      finalDelayMs: finalObservedAtMs - fundingTimeMs, failureClass, error,
    };
  }
}

async function requestOraclePrices(endpoint: string, timeoutMs: number): Promise<Record<string, number>> {
  const response = endpoint.startsWith('data:')
    ? await fetch(endpoint, { signal: AbortSignal.timeout(timeoutMs) })
    : await fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'metaAndAssetCtxs' }), signal: AbortSignal.timeout(timeoutMs),
    });
  if (!response.ok) throw new Error(`Hyperliquid info HTTP ${response.status}: ${await response.text()}`);
  return parseOraclePrices(await response.json());
}

function runWorker(options: FundingOracleWorkerOptions): void {
  const intervalMs = options.intervalMs ?? FUNDING_INTERVAL_MS;
  const capturer = new FundingBoundaryCapturer({
    maxDelayMs: options.maxDelayMs,
    retryBaseMs: options.retryBaseMs ?? 250,
    endpoint: options.endpoint ?? DEFAULT_ENDPOINT,
  });
  const arm = (boundaryMs: number) => {
    const delayMs = Math.max(0, boundaryMs - Date.now());
    setTimeout(async () => {
      parentPort?.postMessage(await capturer.capture(boundaryMs));
      arm(boundaryMs + intervalMs);
    }, delayMs);
  };
  // Always adjudicate the current boundary on startup. If its strict window has
  // elapsed, capture() emits startup_after_deadline without making a request.
  arm(Math.floor(Date.now() / intervalMs) * intervalMs);
}

if (!isMainThread && workerData?.kind === 'funding-oracle-worker') {
  runWorker(workerData.options as FundingOracleWorkerOptions);
}
