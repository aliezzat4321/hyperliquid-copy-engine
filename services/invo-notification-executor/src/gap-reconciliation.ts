import type { InvoSignal } from './notification-signal.js';

export interface UnrecoverableGapPlan {
  ownedCloses: InvoSignal[];
  cursorAdvanceAllowed: false;
}

export interface UnrecoverableGapResult {
  ownedCloseKeys: string[];
  reconciledOwnedCloses: number;
  cursorAdvanceAllowed: false;
}

/**
 * Fail-closed plan for a fetched window that did not reach the durable feed cursor.
 * We may reconcile closes for exposure we already own because ignoring those closes
 * creates artificial exposure. We must not consume opens/increases/unowned closes and
 * must never advance the cursor across the missing historical range.
 */
export function planUnrecoverableGap(
  posts: any[],
  parseSignal: (post: any) => InvoSignal | null,
  hasManagedSource: (sourceBaseId: string) => boolean,
  hasSeen: (key: string) => boolean,
): UnrecoverableGapPlan {
  const ownedCloses: InvoSignal[] = [];
  const keys = new Set<string>();
  for (const post of posts) {
    const signal = parseSignal(post);
    if (!signal || signal.action !== 'close') continue;
    if (hasSeen(signal.key) || keys.has(signal.key)) continue;
    if (!hasManagedSource(signal.sourceBaseId)) continue;
    keys.add(signal.key);
    ownedCloses.push(signal);
  }
  ownedCloses.sort((a, b) => (a.sourceTimeMs ?? a.observedAtMs) - (b.sourceTimeMs ?? b.observedAtMs));
  return { ownedCloses, cursorAdvanceAllowed: false };
}

/**
 * Execute the production fail-closed gap path. Keeping planning, execution and
 * seen-state accounting together makes the service wiring independently testable.
 * Cursor mutation is deliberately absent from this contract.
 */
export async function reconcileUnrecoverableGap(
  posts: any[],
  parseSignal: (post: any) => InvoSignal | null,
  hasManagedSource: (sourceBaseId: string) => boolean,
  hasSeen: (key: string) => boolean,
  execute: (signal: InvoSignal) => Promise<void>,
): Promise<UnrecoverableGapResult> {
  const plan = planUnrecoverableGap(posts, parseSignal, hasManagedSource, hasSeen);
  for (const signal of plan.ownedCloses) await execute(signal);
  return {
    ownedCloseKeys: plan.ownedCloses.map(signal => signal.key),
    reconciledOwnedCloses: plan.ownedCloses.filter(signal => hasSeen(signal.key)).length,
    cursorAdvanceAllowed: false,
  };
}
