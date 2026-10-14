import type { InvoFeedSurface } from './feed-surfaces.js';

export interface SurfaceTimeoutState {
  consecutiveTimeouts: number;
  cooldownUntilMs: number;
}

/**
 * Quarantines only the rotating poll surface whose transport timed out.
 *
 * Push hydration deliberately does not consult this state: a notification can provide
 * the prompt that makes a previously slow surface useful again.  Nor does this class
 * mutate feed cursors; the failed fetch remains unread and is retried after cooldown.
 */
export class FeedSurfaceTimeoutRecovery {
  private readonly state = new Map<InvoFeedSurface, SurfaceTimeoutState>();

  constructor(
    private readonly baseCooldownMs: number,
    private readonly maxCooldownMs: number,
  ) {
    if (baseCooldownMs < 1 || maxCooldownMs < baseCooldownMs) {
      throw new Error('invalid feed surface timeout cooldown bounds');
    }
  }

  noteTimeout(surface: InvoFeedSurface, nowMs: number): SurfaceTimeoutState {
    const consecutiveTimeouts = (this.state.get(surface)?.consecutiveTimeouts ?? 0) + 1;
    const cooldownMs = Math.min(
      this.maxCooldownMs,
      this.baseCooldownMs * (2 ** Math.min(20, consecutiveTimeouts - 1)),
    );
    const next = { consecutiveTimeouts, cooldownUntilMs: nowMs + cooldownMs };
    this.state.set(surface, next);
    return next;
  }

  noteSuccess(surface: InvoFeedSurface): void {
    this.state.delete(surface);
  }

  rotatingPollCooldown(surface: InvoFeedSurface, nowMs: number): SurfaceTimeoutState | null {
    const current = this.state.get(surface);
    return current && current.cooldownUntilMs > nowMs ? { ...current } : null;
  }

  snapshot(): Partial<Record<InvoFeedSurface, SurfaceTimeoutState>> {
    return Object.fromEntries([...this.state].map(([surface, value]) => [surface, { ...value }]));
  }
}

export function isInvoTransportTimeout(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const name = 'name' in error ? String(error.name) : '';
  return name === 'AbortError' || name === 'TimeoutError';
}
