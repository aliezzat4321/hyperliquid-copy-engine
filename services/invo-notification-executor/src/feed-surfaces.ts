import type { InvoSignal } from './notification-signal.js';

export const INVO_FEED_SURFACES = ['following', 'trending', 'fire_moves', 'most_recent'] as const;
export type InvoFeedSurface = typeof INVO_FEED_SURFACES[number];

const allowed = new Set<string>(INVO_FEED_SURFACES);

export function parseFeedSurface(value: string, setting: string): InvoFeedSurface {
  const normalized = value.trim().toLowerCase();
  if (!allowed.has(normalized)) throw new Error(`Invalid ${setting}: ${normalized}`);
  return normalized as InvoFeedSurface;
}

export function parseDiscoverySurfaces(value: string): InvoFeedSurface[] {
  const raw = value.split(',').map(item => item.trim().toLowerCase()).filter(Boolean);
  if (!raw.length || raw.some(item => !allowed.has(item))) {
    throw new Error(`Invalid NOTIFICATION_TRADER_DISCOVERY_SURFACES: ${raw.join(',')}`);
  }
  return [...new Set(raw)] as InvoFeedSurface[];
}

export function surfaceNeedsBaseline(hasDurableBaseline: boolean): boolean {
  return !hasDurableBaseline;
}

export function planSurfaceBaseline(
  signals: Array<InvoSignal | null>,
  ownsSource: (sourceBaseId: string) => boolean,
): { recoverableCloses: InvoSignal[]; skipped: InvoSignal[] } {
  const recoverableCloses: InvoSignal[] = [];
  const skipped: InvoSignal[] = [];
  for (const signal of signals) {
    if (!signal) continue;
    if (signal.action === 'close' && ownsSource(signal.sourceBaseId)) recoverableCloses.push(signal);
    else skipped.push(signal);
  }
  return { recoverableCloses, skipped };
}
