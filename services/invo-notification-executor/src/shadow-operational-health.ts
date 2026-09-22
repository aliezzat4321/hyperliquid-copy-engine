export interface ShadowHealthInput {
  live: boolean; initialized: boolean; fundingHealthy: boolean; directWatchCapacityHealthy: boolean;
  admissionsHealthy: boolean; admissionSuspensionReason: string | null; lastDirectWatchSuccessAtMs: number;
  directWatchFreshnessLimitMs: number; lastFeedSuccessAtMs: number; feedFreshnessLimitMs: number;
  feedEvidenceHealthy: boolean; nowMs: number;
}
export function evaluateShadowOperationalHealth(input: ShadowHealthInput) {
  const failures: string[] = [];
  const directWatchSuccessAgeMs = input.lastDirectWatchSuccessAtMs > 0 ? Math.max(0, input.nowMs - input.lastDirectWatchSuccessAtMs) : null;
  const feedPollAgeMs = input.lastFeedSuccessAtMs > 0 ? Math.max(0, input.nowMs - input.lastFeedSuccessAtMs) : null;
  if (input.live) failures.push('real_trading_not_off');
  if (!input.initialized) failures.push('feed_surfaces_not_initialized');
  if (!input.fundingHealthy) failures.push('funding_oracle_unhealthy');
  if (!input.directWatchCapacityHealthy) failures.push('direct_watch_capacity_unhealthy');
  if (!input.admissionsHealthy) failures.push('direct_watch_admissions_unhealthy');
  if (input.admissionSuspensionReason != null) failures.push('direct_watch_admission_suspended');
  if (directWatchSuccessAgeMs == null || directWatchSuccessAgeMs > input.directWatchFreshnessLimitMs) failures.push('direct_watch_success_stale_or_missing');
  if (feedPollAgeMs == null || feedPollAgeMs > input.feedFreshnessLimitMs) failures.push('feed_poll_stale_or_missing');
  if (!input.feedEvidenceHealthy) failures.push('feed_evidence_persistence_suspended');
  return { shadowOperationalReady: failures.length === 0, shadowOperationalFailures: failures,
    directWatchSuccessAgeMs, feedPollAgeMs, feedPollHealthy: !failures.includes('feed_poll_stale_or_missing') };
}
