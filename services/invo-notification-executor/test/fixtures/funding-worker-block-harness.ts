import { startFundingOracleWorker } from '../../src/funding-oracle-capture.js';
import { FundingBoundaryStore } from '../../src/funding-boundary-store.js';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';

const stagingPath = process.argv[2];
const boundary = Date.now() + 500;
const payload = encodeURIComponent(JSON.stringify([
  { universe: [{ name: 'BTC' }] }, [{ oraclePx: '123.5' }],
]));
let fatal = '';
const manager = startFundingOracleWorker(
  {
    maxDelayMs: 10_000, retryBaseMs: 10, intervalMs: 60_000, heartbeatMs: 500,
    initialBoundaryMs: boundary, endpoint: `data:application/json,${payload}`, stagingPath,
  },
  () => {},
  error => { fatal = error.message; },
);

Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10_100);
const result = new FundingBoundaryStore(stagingPath).read(boundary)?.result;
writeFileSync(join(stagingPath, 'harness-result.json'),
  `${JSON.stringify({ result, fatal, health: manager.health() })}\n`);
await manager.worker.terminate();
