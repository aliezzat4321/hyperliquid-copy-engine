import {
  closeSync,
  existsSync,
  fsyncSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import type { FundingOracleCaptureResult } from './funding-oracle-capture.js';

export interface FundingBoundaryRecord {
  version: 1;
  terminal: true;
  result: FundingOracleCaptureResult;
}

function validate(record: FundingBoundaryRecord, expectedBoundary?: number): FundingBoundaryRecord {
  const result = record?.result;
  if (record?.version !== 1 || record?.terminal !== true || result?.type !== 'funding_oracle_result') {
    throw new Error('invalid funding boundary record');
  }
  if (!Number.isFinite(result.fundingTimeMs) || (expectedBoundary != null && result.fundingTimeMs !== expectedBoundary)) {
    throw new Error('funding boundary record key mismatch');
  }
  if (!Number.isFinite(result.finalObservedAtMs) || !Number.isFinite(result.finalDelayMs)
      || !Array.isArray(result.attempts) || !Number.isFinite(result.retryCount)) {
    throw new Error('invalid funding boundary result metadata');
  }
  if (result.finalDelayMs !== result.finalObservedAtMs - result.fundingTimeMs
      || !Number.isSafeInteger(result.retryCount) || result.retryCount < 0) {
    throw new Error('inconsistent funding boundary result metadata');
  }
  if (result.failureClass == null) {
    const prices = Object.values(result.oraclePrices ?? {});
    if (prices.length === 0 || prices.some(price => !Number.isFinite(price) || price <= 0)) {
      throw new Error('successful funding boundary record requires positive oracle prices');
    }
  }
  return record;
}

/** Immutable, worker-owned terminal records. Hard-link publication cannot overwrite a prior result. */
export class FundingBoundaryStore {
  constructor(readonly directory: string) {}

  private path(fundingTimeMs: number): string {
    if (!Number.isSafeInteger(fundingTimeMs) || fundingTimeMs < 0) throw new Error(`invalid funding boundary ${fundingTimeMs}`);
    return join(this.directory, `${fundingTimeMs}.json`);
  }

  read(fundingTimeMs: number): FundingBoundaryRecord | null {
    const path = this.path(fundingTimeMs);
    if (!existsSync(path)) return null;
    try {
      return validate(JSON.parse(readFileSync(path, 'utf8')) as FundingBoundaryRecord, fundingTimeMs);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`corrupt funding boundary record ${path}: ${reason}`, { cause: error });
    }
  }

  publish(result: FundingOracleCaptureResult): FundingBoundaryRecord {
    const record = validate({ version: 1, terminal: true, result });
    mkdirSync(this.directory, { recursive: true });
    const finalPath = this.path(result.fundingTimeMs);
    const existing = this.read(result.fundingTimeMs);
    if (existing) return existing;
    const tempPath = join(this.directory, `.${result.fundingTimeMs}.${process.pid}.${randomUUID()}.tmp`);
    const fd = openSync(tempPath, 'wx', 0o600);
    try {
      writeFileSync(fd, `${JSON.stringify(record)}\n`);
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    try {
      linkSync(tempPath, finalPath);
      const dirFd = openSync(this.directory, 'r');
      try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
      return record;
    } catch (error: any) {
      if (error?.code !== 'EEXIST') throw error;
      return this.read(result.fundingTimeMs)!;
    } finally {
      try { unlinkSync(tempPath); } catch { /* publication may have failed before temp creation */ }
    }
  }

  boundaries(): number[] {
    if (!existsSync(this.directory)) return [];
    return readdirSync(this.directory)
      .map(name => /^(\d+)\.json$/.exec(name)?.[1])
      .filter((value): value is string => value != null)
      .map(Number)
      .filter(Number.isSafeInteger)
      .sort((a, b) => a - b);
  }
}

export function defaultFundingBoundaryPath(auditPath: string): string {
  return join(dirname(auditPath), 'funding-boundaries');
}
