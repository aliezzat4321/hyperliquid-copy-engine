import type { InvoSignal } from './notification-signal.js';

export interface UnrecoverableGapPlan {
  ownedCloses: InvoSignal[];
  cursorAdvanceAllowed: false;
}

export function canProspectivelyRebaseGap(
  live: boolean,
  managedCount: number,
  newestPostId: string | null,
): boolean {
  return !live && managedCount === 0 && Boolean(newestPostId);
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
