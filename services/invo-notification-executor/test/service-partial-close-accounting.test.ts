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

  assert.match(closeBranch, /const remainingSize = Math\.max\(0, size - fill\.filledSize\)/);
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

test('health marks each paper position independently so one failure cannot fail the endpoint', () => {
  const serviceSource = readFileSync(new URL('../../src/service.ts', import.meta.url), 'utf8');
  const healthStart = serviceSource.indexOf("req.method === 'GET' && req.url === '/health'");
  const tradersStart = serviceSource.indexOf("req.method === 'GET' && req.url === '/traders'", healthStart);
  assert.ok(healthStart >= 0 && tradersStart > healthStart, 'health handler must exist');
  const healthBranch = serviceSource.slice(healthStart, tradersStart);

  assert.match(healthBranch, /Promise\.all\(paperPositions\.map\(async position => \{/);
  assert.match(healthBranch, /try \{[\s\S]*return await markShadowPosition\(position, shadowPolicy\);[\s\S]*\} catch \(err\) \{/);
  assert.match(healthBranch, /shadowOpenExposureCount: paperPositions\.length/);
  assert.match(healthBranch, /closedOnlyProfitabilityForbidden: true/);
});
