import type { InvoSignal } from './notification-signal.js';

/** Serializes one source lifecycle across independent ingress paths. */
export class SourceLifecycleQueue {
  private readonly tails = new Map<string, Promise<void>>();

  async run<T>(sourceBaseId: string, work: () => Promise<T>): Promise<T> {
    const prior = this.tails.get(sourceBaseId) ?? Promise.resolve();
    let release!: () => void;
    const turn = new Promise<void>(resolve => { release = resolve; });
    const tail = prior.catch(() => {}).then(() => turn);
    this.tails.set(sourceBaseId, tail);
    await prior.catch(() => {});
    try {
      return await work();
    } finally {
      release();
      if (this.tails.get(sourceBaseId) === tail) this.tails.delete(sourceBaseId);
    }
  }
}

/** Preserves lifecycle order per source while allowing cross-source parallelism. */
export async function runSignalBatchBySource(
  signals: InvoSignal[],
  run: (signal: InvoSignal) => Promise<void>,
): Promise<void> {
  const groups = new Map<string, InvoSignal[]>();
  for (const signal of signals) groups.set(signal.sourceBaseId, [...(groups.get(signal.sourceBaseId) ?? []), signal]);
  const priority: Record<InvoSignal['action'], number> = { open: 0, increase: 1, close: 2 };
  await Promise.all([...groups.values()].map(async group => {
    group.sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs)
      || priority[a.action] - priority[b.action] || a.key.localeCompare(b.key));
    for (const signal of group) await run(signal);
  }));
}
