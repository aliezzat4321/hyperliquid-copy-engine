import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Lane3RuntimeEvidence } from '../src/runtime-evidence.js';

const population = (discovered: number) => ({
  policy: { minEvents: 20, minObservationDays: 7, staleAfterMs: 259_200_000, inactiveAfterMs: 1_209_600_000 },
  funnel: { discovered, trackable: discovered, receivingNotifications: 1, shadowAssessable: 0 },
  assessmentQueue: [],
  traders: [{ id: 'invo-user:1', eventCount: 1, observationDays: ['2026-09-09'], missingOrFailedReasons: {} }],
});

test('persists atomic latest and append-only prospective population evidence', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-evidence-'));
  const latestPath = join(dir, 'latest.json');
  const historyPath = join(dir, 'history.jsonl');
  const evidence = new Lane3RuntimeEvidence({
    latestPath, historyPath, historyIntervalMs: 60_000, live: false,
    discoverySurfaces: ['following', 'all', 'trending'],
  }, 1_000);
  evidence.recordAuditEvent({ type: 'shadow_opened', signal: { key: 'event-1' }, wakeSource: 'push_notification' });
  evidence.recordAuditEvent({ type: 'skip', reason: 'stale_signal', signal: { key: 'event-2' } });
  evidence.recordPoll('all', true, population(2), 1, 2_000);
  evidence.recordPoll('trending', true, population(3), 1, 3_000);

  const latest = JSON.parse(readFileSync(latestPath, 'utf8'));
  assert.equal(latest.realTradingEnabled, false);
  assert.equal(latest.collectionIndependentOfAcceptance, true);
  assert.equal(latest.counters.eligibleSignals, 2);
  assert.equal(latest.counters.causalDecisions, 2);
  assert.equal(latest.counters.explicitRejectsOrGaps, 1);
  assert.equal(latest.counters.notificationWakes, 1);
  assert.equal(latest.currentFunnel.discovered, 3);
  assert.equal(latest.funnelDelta.discovered, 1);
  assert.equal(latest.unresolvedExposureCount, 1);
  assert.equal(latest.evidencePolicy.minObservationDays, 7);
  assert.equal(readFileSync(historyPath, 'utf8').trim().split('\n').length, 1);
});

test('continues the same durable window after restart', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-evidence-'));
  const options = { latestPath: join(dir, 'latest.json'), historyPath: join(dir, 'history.jsonl'), historyIntervalMs: 60_000, live: false, discoverySurfaces: ['all'] };
  const first = new Lane3RuntimeEvidence(options, 1_000);
  first.recordPoll('all', true, population(1), 0, 2_000);
  const restarted = new Lane3RuntimeEvidence(options, 3_000);
  restarted.recordPoll('all', true, population(2), 0, 4_000);
  assert.equal(restarted.snapshot()!.counters.successfulPolls, 2);
  assert.equal(restarted.snapshot()!.funnelDelta?.discovered, 1);
});

test('does not produce acceptance evidence in live mode', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lane3-evidence-'));
  const evidence = new Lane3RuntimeEvidence({
    latestPath: join(dir, 'latest.json'), historyPath: join(dir, 'history.jsonl'),
    historyIntervalMs: 60_000, live: true, discoverySurfaces: ['all'],
  });
  evidence.recordPoll('all', true, population(1), 0);
  assert.equal(evidence.snapshot(), null);
});
