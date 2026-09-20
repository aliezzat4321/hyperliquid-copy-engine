import { Worker } from 'node:worker_threads';

export const FUNDING_INTERVAL_MS = 60 * 60 * 1000;

export type FundingOracleFailureClass =
  | 'startup_after_deadline'
  | 'deadline_exhausted'
  | 'worker_error';

export interface FundingOracleAttempt {
  requestedAtMs: number;
  completedAtMs: number;
  requestLatencyMs: number;
  outcome: 'success' | 'error';
  errorClass?: string;
}

export interface FundingOracleCaptureResult {
  type: 'funding_oracle_result';
  fundingTimeMs: number;
  attempts: FundingOracleAttempt[];
  retryCount: number;
  finalObservedAtMs: number;
  finalDelayMs: number;
  oraclePrices?: Record<string, number>;
  failureClass?: FundingOracleFailureClass;
  error?: string;
}

export interface FundingOracleWorkerOptions {
  maxDelayMs: number;
  retryBaseMs?: number;
  intervalMs?: number;
  endpoint?: string;
}

/**
 * The worker owns only timers and read-only HTTP. All durable position writes remain on
 * the main thread, so a delayed worker message cannot race another process/state copy.
 */
export function startFundingOracleWorker(
  options: FundingOracleWorkerOptions,
  onResult: (result: FundingOracleCaptureResult) => void,
  onWorkerError: (error: Error) => void,
): Worker {
  const worker = new Worker(new URL('./funding-oracle-worker.js', import.meta.url), {
    workerData: { kind: 'funding-oracle-worker', options },
    // Node's test runner flags are process-launch concerns and are invalid when an
    // application worker loads one concrete module.
    execArgv: process.execArgv.filter(argument => !argument.startsWith('--test')),
  });
  let reportedFailure = false;
  worker.on('message', (message: unknown) => {
    if ((message as FundingOracleCaptureResult | undefined)?.type === 'funding_oracle_result') {
      onResult(message as FundingOracleCaptureResult);
    }
  });
  worker.on('error', error => {
    reportedFailure = true;
    onWorkerError(error instanceof Error ? error : new Error(String(error)));
  });
  worker.on('exit', code => {
    if (code !== 0 && !reportedFailure) onWorkerError(new Error(`funding oracle worker exited with code ${code}`));
  });
  return worker;
}
