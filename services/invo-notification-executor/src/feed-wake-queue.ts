/**
 * Keep at most one pending hydration wake per feed surface.
 *
 * A single replaceable pending slot can silently discard a rotating-surface poll when a
 * push notification arrives during another hydration.  Coalescing only within the same
 * surface keeps the queue bounded while preserving every surface's cursor reconciliation.
 */
export class FeedWakeQueue<T extends { feedFilter: string; source: string }> {
  private readonly pending: T[] = [];

  enqueue(wake: T): void {
    const existing = this.pending.findIndex(item => item.feedFilter === wake.feedFilter);
    if (existing >= 0) {
      if (this.pending[existing].source === "push_notification" && wake.source !== "push_notification") return;
      this.pending[existing] = wake;
      return;
    }
    this.pending.push(wake);
  }

  shift(): T | null {
    return this.pending.shift() ?? null;
  }

  get length(): number {
    return this.pending.length;
  }
}
