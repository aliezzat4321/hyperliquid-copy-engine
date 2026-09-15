import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('production partial-close path allocates costs proportionally and resets funding boundary', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const closeStart = serviceSource.indexOf("if (signal.action === 'close')");
  const liveCloseStart = serviceSource.indexOf('const sameCoinManaged =', closeStart);
  assert.ok(closeStart >= 0 && liveCloseStart > closeStart, 'shadow close branch must exist');

  const closeBranch = serviceSource.slice(closeStart, liveCloseStart);
  assert.match(closeBranch, /const fraction = Math\.min\(1, fill\.filledSize \/ size\)/);
  assert.match(closeBranch, /fundingUsd = funding\.fundingUsd \* fraction/);
  assert.match(closeBranch, /entryFeeUsd: Number\(managed\.entryFeeUsd \?\? 0\) \* fraction/);
  assert.match(closeBranch, /entryNotionalUsd: Number\(managed\.entryNotionalExecutedUsd\) \* fraction/);

  assert.match(closeBranch, /const remainingSize = fill\.unfilledSize/);
  assert.match(closeBranch, /const remainingFraction = remainingSize \/ size/);
  assert.match(closeBranch, /entryFeeUsd: Number\(managed\.entryFeeUsd \?\? 0\) \* remainingFraction/);
  assert.match(closeBranch, /entrySlippageUsd: Number\(managed\.entrySlippageUsd \?\? 0\) \* remainingFraction/);
  assert.match(closeBranch, /entryNotionalExecutedUsd: Number\(managed\.entryNotionalExecutedUsd \?\? 0\) \* remainingFraction/);
  assert.match(closeBranch, /marginUsd: Number\(managed\.marginUsd \?\? 0\) \* remainingFraction/);
  assert.match(closeBranch, /fundingCarryUsd: fullPositionFundingUsd == null[\s\S]*fullPositionFundingUsd \* remainingFraction/);
  assert.match(closeBranch, /fundingAccruedThroughMs: fill\.receivedAtMs/);
  assert.match(closeBranch, /fundingOracleCheckpoints: \[\]/);
  assert.match(closeBranch, /unresolvedAfterSourceClose: true/);
});

test('health bounds MTM concurrency and excludes unresolved source-close exposure', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const healthStart = serviceSource.indexOf("req.method === 'GET' && req.url === '/health'");
  const tradersStart = serviceSource.indexOf("req.method === 'GET' && req.url === '/traders'", healthStart);
  assert.ok(healthStart >= 0 && tradersStart > healthStart, 'health handler must exist');
  const healthBranch = serviceSource.slice(healthStart, tradersStart);

  assert.match(healthBranch, /mapConcurrent\(markablePaperPositions, HEALTH_MTM_CONCURRENCY, async position => \{/);
  assert.match(healthBranch, /try \{[\s\S]*return await markShadowPosition\(position, shadowPolicy\);[\s\S]*\} catch \(err\) \{/);
  assert.match(healthBranch, /shadowOpenExposureCount: paperPositions\.length/);
  assert.match(healthBranch, /shadowMtmEligibleCount: markablePaperPositions\.length/);
  assert.match(healthBranch, /unresolvedSourceCloseExposureCount: unresolvedPaperPositions\.length/);
  assert.match(healthBranch, /closedOnlyProfitabilityForbidden: true/);
});

test('transient source-close book failures remain unseen and retry with bounded backoff', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const closeStart = serviceSource.indexOf("if (signal.action === 'close')");
  const liveCloseStart = serviceSource.indexOf('const sameCoinManaged =', closeStart);
  const closeBranch = serviceSource.slice(closeStart, liveCloseStart);
  assert.match(serviceSource, /SOURCE_CLOSE_RETRY_MAX_MS = 30_000/);
  assert.match(serviceSource, /Math\.min\(SOURCE_CLOSE_RETRY_MAX_MS/);
  assert.match(closeBranch, /pendingSourceClose: signal/);
  assert.doesNotMatch(closeBranch, /allowBelowMinNotional/);
  assert.match(closeBranch, /INCOMPLETE_DUST_RECONCILIATION/);
  assert.match(closeBranch, /shadow_close_dust_reconciled/);
  assert.match(closeBranch, /if \(!fill\.partial \|\| residualDustReason\) state\.markSeen\(signal\.key\)/);
  assert.match(closeBranch, /state\.clearManagedBySource\(signal\.sourceBaseId\)/);
  assert.match(closeBranch, /sourceCloseNextRetryAtMs/);
  assert.match(serviceSource, /source_close_reconciliation/);
});

test('below-minimum source close is quarantined without fabricated execution or orphan state', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const dustStart = serviceSource.indexOf('const dustReason =');
  const retryStart = serviceSource.indexOf('const retryable = true;', dustStart);
  assert.ok(dustStart >= 0 && retryStart > dustStart, 'dust branch must precede ordinary retry');
  const dustBranch = serviceSource.slice(dustStart, retryStart);

  assert.match(dustBranch, /lot_rounded_to_zero/);
  assert.match(dustBranch, /below_min_notional/);
  assert.match(dustBranch, /result\.detail\?\.requestedNotionalUsd != null/);
  assert.match(dustBranch, /state\.clearManagedBySource\(signal\.sourceBaseId\)/);
  assert.match(dustBranch, /state\.markSeen\(signal\.key\)/);
  assert.match(dustBranch, /type: 'shadow_close_dust_reconciled'/);
  assert.match(dustBranch, /economicsCompleteness: 'INCOMPLETE_DUST_RECONCILIATION'/);
  assert.match(dustBranch, /executionSimulated: false/);
  assert.match(dustBranch, /grossPnlUsd: null, grossReturnBps: null/);
  assert.match(dustBranch, /totalExplicitCostUsd: null, netPnlUsd: null, netReturnBps: null/);
  assert.doesNotMatch(dustBranch, /simulateL2Fill\(/);
  assert.doesNotMatch(dustBranch, /setManaged\(/);
});

test('post-partial non-executable residue is quarantined while executable residue keeps retrying', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const residualStart = serviceSource.indexOf('const residualDustReason =');
  const liveCloseStart = serviceSource.indexOf('const sameCoinManaged =', residualStart);
  assert.ok(residualStart >= 0 && liveCloseStart > residualStart, 'residual dust branch must exist');
  const residualBranch = serviceSource.slice(residualStart, liveCloseStart);

  assert.match(residualBranch, /if \(fill\.partial && !residualDustReason\) \{[\s\S]*state\.setManaged\(/);
  assert.match(residualBranch, /else \{[\s\S]*state\.clearManagedBySource\(signal\.sourceBaseId\)/);
  assert.match(residualBranch, /if \(!fill\.partial \|\| residualDustReason\) state\.markSeen\(signal\.key\)/);
  assert.match(residualBranch, /method: 'post_partial_close_residual_dust_quarantine'/);
  assert.match(residualBranch, /dustReconciledSize: remainingSize/);
  assert.match(residualBranch, /executableClosedSize: fill\.filledSize/);
  assert.match(residualBranch, /economicsCompleteness: 'INCOMPLETE_DUST_RECONCILIATION'/);
});

test('health and startup evidence expose the deployment-backed book-age changeover', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  assert.match(serviceSource, /priorMaxBookAgeMs: 750/);
  assert.match(serviceSource, /currentMaxBookAgeMs: 1000/);
  assert.match(serviceSource, /deployRun: '34992024173'/);
  assert.match(serviceSource, /effectiveUtc: '2026-09-15T15:59:18Z'/);
  assert.match(serviceSource, /commit: '90ee58d679a093c86543e09a6afc6e3238bc3a74'/);
  assert.equal((serviceSource.match(/bookAgePolicyChangeover: BOOK_AGE_POLICY_CHANGEOVER/g) ?? []).length, 2);
});
