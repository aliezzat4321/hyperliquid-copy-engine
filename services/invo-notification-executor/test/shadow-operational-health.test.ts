import assert from 'node:assert/strict';
import test from 'node:test';
import { evaluateShadowOperationalHealth } from '../src/shadow-operational-health.js';
const good = (override: Record<string, unknown> = {}) => evaluateShadowOperationalHealth({
  live: false, initialized: true, fundingHealthy: true, directWatchCapacityHealthy: true,
  admissionsHealthy: true, admissionSuspensionReason: null, lastDirectWatchSuccessAtMs: 990,
  directWatchFreshnessLimitMs: 50, lastFeedSuccessAtMs: 990, feedFreshnessLimitMs: 50,
  feedEvidenceHealthy: true, nowMs: 1000, ...override,
});
test('operational readiness is integrated and fail closed', () => {
  assert.equal(good().shadowOperationalReady, true);
  for (const override of [{ initialized: false }, { fundingHealthy: false },
    { directWatchCapacityHealthy: false }, { admissionsHealthy: false },
    { admissionSuspensionReason: 'scan_failure' }, { lastDirectWatchSuccessAtMs: 1 },
    { lastFeedSuccessAtMs: 1 }, { feedEvidenceHealthy: false }, { live: true }])
    assert.equal(good(override).shadowOperationalReady, false, JSON.stringify(override));
});
