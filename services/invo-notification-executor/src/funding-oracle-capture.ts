import { Worker } from 'node:worker_threads';

export const FUNDING_INTERVAL_MS = 60 * 60 * 1000;
export const MAX_FUNDING_ORACLE_DELAY_MS = 10_000;

export function clampFundingOracleMaxDelayMs(value: number): number {
  if (!Number.isFinite(value)) throw new Error(`invalid funding oracle max delay: ${value}`);
  return Math.min(MAX_FUNDING_ORACLE_DELAY_MS, Math.max(500, value));
}

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
  stagingPath: string;
  retryBaseMs?: number;
  intervalMs?: number;
  endpoint?: string;
  heartbeatMs?: number;
  /** Worker-owned liveness counter. Injected by the manager; never configured by operators. */
  livenessBuffer?: SharedArrayBuffer;
  // Test/recovery harness override. Production omits this and uses the current hourly boundary.
  initialBoundaryMs?: number;
}

export interface FundingOracleHeartbeat {
  type: 'funding_oracle_heartbeat';
  observedAtMs: number;
  schedulerNextBoundaryMs: number;
}

export interface FundingOracleWorkerHealth {
  healthy: boolean;
  startedAtMs: number;
  lastMessageAtMs: number;
  lastHeartbeatAtMs: number;
  lastResultAtMs: number | null;
  failure: string | null;
}

type WorkerLike = Pick<Worker, 'on' | 'terminate'>;
type WorkerFactory = (options: FundingOracleWorkerOptions) => WorkerLike;

export class FundingOracleWorkerManager {
  readonly worker: WorkerLike;
  private readonly startedAtMs: number;
  private lastMessageAtMs: number;
  private lastHeartbeatAtMs: number;
  private lastResultAtMs: number | null = null;
  private failure: Error | null = null;
  private readonly heartbeatTimer: NodeJS.Timeout;
  private readonly liveness: Int32Array;
  private lastLivenessCounter = 0;
  private missedLivenessChecks = 0;

  constructor(
    options: FundingOracleWorkerOptions,
    private readonly onResult: (result: FundingOracleCaptureResult) => void,
    private readonly onFatal: (error: Error) => void,
    factory: WorkerFactory = createFundingOracleWorker,
    private readonly now: () => number = Date.now,
  ) {
    this.startedAtMs = this.now();
    this.lastMessageAtMs = this.startedAtMs;
    this.lastHeartbeatAtMs = this.startedAtMs;
    const heartbeatMs = options.heartbeatMs ?? 1_000;
    this.liveness = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
    this.worker = factory({ ...options, livenessBuffer: this.liveness.buffer as SharedArrayBuffer });
    this.worker.on('message', (message: unknown) => this.handleMessage(message));
    this.worker.on('error', (error: Error) => this.fail(error instanceof Error ? error : new Error(String(error))));
    this.worker.on('exit', (code: number) => this.fail(new Error(`funding oracle worker exited with code ${code}`)));
    this.heartbeatTimer = setInterval(() => {
      const counter = Atomics.load(this.liveness, 0);
      if (counter !== this.lastLivenessCounter) {
        this.lastLivenessCounter = counter;
        this.missedLivenessChecks = 0;
        this.lastHeartbeatAtMs = this.now();
      } else {
        this.missedLivenessChecks += 1;
      }
      // Delivery of worker messages depends on the main event loop. The shared atomic
      // does not, so a long main-thread pause observes accumulated worker progress
      // instead of falsely declaring the worker dead after unblocking.
      if (this.missedLivenessChecks > 3) {
        this.fail(new Error(`funding oracle worker missed ${this.missedLivenessChecks} atomic heartbeats`));
      }
    }, heartbeatMs);
    this.heartbeatTimer.unref();
  }

  private handleMessage(message: unknown): void {
    this.lastMessageAtMs = this.now();
    if ((message as FundingOracleHeartbeat | undefined)?.type === 'funding_oracle_heartbeat') {
      this.lastHeartbeatAtMs = this.now();
      return;
    }
    if ((message as FundingOracleCaptureResult | undefined)?.type === 'funding_oracle_result') {
      this.lastResultAtMs = this.now();
      this.onResult(message as FundingOracleCaptureResult);
    }
  }

  private fail(error: Error): void {
    if (this.failure) return;
    this.failure = error;
    clearInterval(this.heartbeatTimer);
    this.onFatal(error);
  }

  health(): FundingOracleWorkerHealth {
    return {
      healthy: this.failure == null,
      startedAtMs: this.startedAtMs,
      lastMessageAtMs: this.lastMessageAtMs,
      lastHeartbeatAtMs: this.lastHeartbeatAtMs,
      lastResultAtMs: this.lastResultAtMs,
      failure: this.failure?.message ?? null,
    };
  }
}

/**
 * The worker owns only timers and read-only HTTP. All durable position writes remain on
 * the main thread, so a delayed worker message cannot race another process/state copy.
 */
function createFundingOracleWorker(options: FundingOracleWorkerOptions): Worker {
  return new Worker(new URL('./funding-oracle-worker.js', import.meta.url), {
    workerData: { kind: 'funding-oracle-worker', options },
    // Node's test runner flags are process-launch concerns and are invalid when an
    // application worker loads one concrete module.
    execArgv: process.execArgv.filter(argument => !argument.startsWith('--test')),
  });
}

export function startFundingOracleWorker(
  options: FundingOracleWorkerOptions,
  onResult: (result: FundingOracleCaptureResult) => void,
  onWorkerError: (error: Error) => void,
): FundingOracleWorkerManager {
  return new FundingOracleWorkerManager(options, onResult, onWorkerError);
}
