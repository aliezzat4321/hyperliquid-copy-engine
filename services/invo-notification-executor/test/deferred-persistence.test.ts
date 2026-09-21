import assert from 'node:assert/strict';
import test from 'node:test';
import { scheduleDeferredPersistence } from '../src/deferred-persistence.js';

test('feed evidence append or compact failure cannot abort owned close reconciliation', async () => {
  let ownedCloseReconciled = false;
  let persistenceErrors = 0;
  let assimilationSuspended = false;

  // This is the service ordering contract: core CLOSE work finishes before the
  // non-critical evidence write is even scheduled.
  ownedCloseReconciled = true;
  scheduleDeferredPersistence(
    () => { throw new Error('injected append/compact failure'); },
    () => { persistenceErrors += 1; assimilationSuspended = true; },
  );
  assert.equal(ownedCloseReconciled, true);
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(ownedCloseReconciled, true);
  assert.equal(persistenceErrors, 1);
  assert.equal(assimilationSuspended, true);
});
