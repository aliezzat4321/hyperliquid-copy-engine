import type { InvoSignal } from './notification-signal.js';
import { runSignalBatchBySource } from './source-lifecycle.js';

export interface CapturedSignalBatch {
  portfolioId: string;
  signals: InvoSignal[];
  highWaterMs: number;
  commit: (highWaterMs: number) => void;
}

/**
 * Publishes healthy admissions before making any captured NEW/ADD copy decision.
 * A target watermark advances only after every signal has reached a durable terminal
 * disposition. Transient admission/execution failures therefore replay next scan.
 */
export async function publishThenFlushCapturedSignals(
  batches: CapturedSignalBatch[],
  publishHealthyAdmissions: () => void,
  execute: (signal: InvoSignal) => Promise<void>,
  isTerminal: (signal: InvoSignal) => boolean,
): Promise<{ committed: string[]; pending: string[] }> {
  publishHealthyAdmissions();
  const signals = batches.flatMap(batch => batch.signals);
  await runSignalBatchBySource(signals, execute);
  const committed: string[] = [];
  const pending: string[] = [];
  for (const batch of batches) {
    if (batch.signals.every(isTerminal)) {
      batch.commit(batch.highWaterMs);
      committed.push(batch.portfolioId);
    } else {
      pending.push(batch.portfolioId);
    }
  }
  return { committed, pending };
}
