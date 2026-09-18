import type { InvoSignal } from './notification-signal.js';

/** Action-aware identity keeps OPEN and CLOSE distinct at the same source time. */
export function sourceEventKey(signal: InvoSignal): string | null {
  if (signal.sourceTimeMs == null) return null;
  const resultingSizeValue = signal.resultingSourceSize ?? signal.entrySize;
  const resultingSize = signal.action === 'increase' && resultingSizeValue != null
    ? `:size-${resultingSizeValue}`
    : '';
  return `source-event:${signal.sourceBaseId}:${signal.action}:${signal.sourceTimeMs}${resultingSize}`;
}

export function legacySourceEventKey(signal: InvoSignal): string | null {
  return signal.sourceTimeMs == null ? null : `source-event:${signal.sourceBaseId}:${signal.sourceTimeMs}`;
}

export function closeLifecycleKey(signal: InvoSignal): string | null {
  return signal.action === 'close' ? `source-close:${signal.sourceBaseId}` : null;
}

/**
 * Canonical durable completion check for a CLOSED source lifecycle. A close may
 * arrive from feed and direct polling with different timestamps, so either its
 * action-aware event identity or the timestamp-independent lifecycle identity
 * proves that the lifecycle was already accounted for.
 */
export function closedLifecycleWasHandled(
  signal: InvoSignal,
  hasSeen: (key: string) => boolean,
): boolean {
  if (signal.action !== 'close') return false;
  const eventKey = sourceEventKey(signal);
  const lifecycleKey = closeLifecycleKey(signal);
  return (eventKey != null && hasSeen(eventKey))
    || (lifecycleKey != null && hasSeen(lifecycleKey));
}

/**
 * Legacy event keys are honored for OPEN to prevent replay after upgrade.
 * They are intentionally not consulted for CLOSE because an old OPEN key is
 * ambiguous and must never suppress a later close at the same timestamp.
 */
export function signalWasSeen(signal: InvoSignal, hasSeen: (key: string) => boolean): boolean {
  const eventKey = sourceEventKey(signal);
  const legacyKey = legacySourceEventKey(signal);
  return hasSeen(signal.key)
    || (eventKey != null && hasSeen(eventKey))
    || (signal.action === 'open' && legacyKey != null && hasSeen(legacyKey))
    || closedLifecycleWasHandled(signal, hasSeen);
}
