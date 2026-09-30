import assert from 'node:assert/strict';
import test from 'node:test';
import { evaluateShadowOperationalHealth } from '../src/shadow-operational-health.js';
const good = (override: Record<string, unknown> = {}) => evaluateShadowOperationalHealth({
  live: false, initialized: true, fundingHealthy: true, directWatchCapacityHealthy: true,
  admissionsHealthy: true, admissionSuspensionReason: null, lastDirectWatchSuccessAtMs: 990,
  directWatchFreshnessLimitMs: 50, lastFeedSuccessAtMs: 990, feedFreshnessLimitMs: 50,
  feedEvidenceHealthy: true, unresolvedFeedGapSinceMs: 0, nowMs: 1000, ...override,
});
test('primary SHADOW collection health fails closed on data-integrity inputs', () => {
  assert.equal(good().shadowOperationalReady, true);
  for (const override of [{ initialized: false }, { fundingHealthy: false },
    { lastFeedSuccessAtMs: 1 }, { feedEvidenceHealthy: false }, { unresolvedFeedGapSinceMs: 900 }, { live: true }])
    assert.equal(good(override).shadowOperationalReady, false, JSON.stringify(override));
});
test('direct-watch fallback degradation is visible but does not invalidate healthy primary feed collection', () => {
  for (const override of [{ directWatchCapacityHealthy: false }, { admissionsHealthy: false },
    { admissionSuspensionReason: 'successful_observation_overdue' }, { lastDirectWatchSuccessAtMs: 1 }]) {
    const health = good(override);
    assert.equal(health.shadowOperationalReady, true, JSON.stringify(override));
    assert.equal(health.shadowDataCollectionReady, true, JSON.stringify(override));
    assert.equal(health.directWatchFallbackReady, false, JSON.stringify(override));
    assert.ok(health.directWatchFallbackFailures.length > 0);
  }
});
