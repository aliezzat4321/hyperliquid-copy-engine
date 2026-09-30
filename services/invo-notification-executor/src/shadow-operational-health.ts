export interface ShadowHealthInput {
  live: boolean; initialized: boolean; fundingHealthy: boolean; directWatchCapacityHealthy: boolean;
  admissionsHealthy: boolean; admissionSuspensionReason: string | null; lastDirectWatchSuccessAtMs: number;
  directWatchFreshnessLimitMs: number; lastFeedSuccessAtMs: number; feedFreshnessLimitMs: number;
  feedEvidenceHealthy: boolean; unresolvedFeedGapSinceMs: number; nowMs: number;
}
export function evaluateShadowOperationalHealth(input: ShadowHealthInput) {
  const collectionFailures: string[] = [];
  const fallbackFailures: string[] = [];
  const directWatchSuccessAgeMs = input.lastDirectWatchSuccessAtMs > 0 ? Math.max(0, input.nowMs - input.lastDirectWatchSuccessAtMs) : null;
  const feedPollAgeMs = input.lastFeedSuccessAtMs > 0 ? Math.max(0, input.nowMs - input.lastFeedSuccessAtMs) : null;
  if (input.live) collectionFailures.push('real_trading_not_off');
  if (!input.initialized) collectionFailures.push('feed_surfaces_not_initialized');
  if (!input.fundingHealthy) collectionFailures.push('funding_oracle_unhealthy');
  if (feedPollAgeMs == null || feedPollAgeMs > input.feedFreshnessLimitMs) collectionFailures.push('feed_poll_stale_or_missing');
  if (!input.feedEvidenceHealthy) collectionFailures.push('feed_evidence_persistence_suspended');
  if (input.unresolvedFeedGapSinceMs > 0) collectionFailures.push('feed_continuity_gap_unresolved');
  if (!input.directWatchCapacityHealthy) fallbackFailures.push('direct_watch_capacity_unhealthy');
  if (!input.admissionsHealthy) fallbackFailures.push('direct_watch_admissions_unhealthy');
  if (input.admissionSuspensionReason != null) fallbackFailures.push('direct_watch_admission_suspended');
  if (directWatchSuccessAgeMs == null || directWatchSuccessAgeMs > input.directWatchFreshnessLimitMs) fallbackFailures.push('direct_watch_success_stale_or_missing');
  return {
    shadowOperationalReady: collectionFailures.length === 0,
    shadowOperationalFailures: collectionFailures,
    shadowDataCollectionReady: collectionFailures.length === 0,
    shadowDataCollectionFailures: collectionFailures,
    directWatchFallbackReady: fallbackFailures.length === 0,
    directWatchFallbackFailures: fallbackFailures,
    directWatchSuccessAgeMs, feedPollAgeMs,
    feedPollHealthy: !collectionFailures.includes('feed_poll_stale_or_missing'),
  };
}
