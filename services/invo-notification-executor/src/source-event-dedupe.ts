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
 * Legacy event keys are honored for OPEN to prevent replay after upgrade.
 * They are intentionally not consulted for CLOSE because an old OPEN key is
 * ambiguous and must never suppress a later close at the same timestamp.
 */
export function signalWasSeen(signal: InvoSignal, hasSeen: (key: string) => boolean): boolean {
  const eventKey = sourceEventKey(signal);
  const legacyKey = legacySourceEventKey(signal);
  const lifecycleKey = closeLifecycleKey(signal);
  return hasSeen(signal.key)
    || (eventKey != null && hasSeen(eventKey))
    || (signal.action === 'open' && legacyKey != null && hasSeen(legacyKey))
    || (lifecycleKey != null && hasSeen(lifecycleKey));
}
