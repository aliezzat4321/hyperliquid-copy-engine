import type { FundingOracleWorkerManager } from './funding-oracle-capture.js';

/** Arm funding capture synchronously before beginning any Invo authentication work. */
export async function startFundingBeforeInvoAuthentication(
  startCapture: () => FundingOracleWorkerManager,
  ensureInvoToken: () => Promise<void>,
  onCaptureStarted: (manager: FundingOracleWorkerManager) => void,
): Promise<void> {
  const manager = startCapture();
  onCaptureStarted(manager);
  await ensureInvoToken();
}
