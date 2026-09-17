import { isNonExecutableDust } from './shadow-execution.js';

export function shouldTerminallyDustReconcile(
  reason: string,
  requestedResidualSize: number,
  szDecimals: number,
  causalMidPx: number | null,
  minNotionalUsd: number,
): boolean {
  if (reason === 'lot_rounded_to_zero') return true;
  if (reason !== 'below_min_notional') return false;
  // `below_min_notional` can mean either the requested residual itself is too small
  // OR that the requested close was executable but the causal book exposed < minimum
  // total depth. Only the former is terminal dust. Missing mid evidence fails closed
  // into the retryable unresolved-exposure path.
  if (!(causalMidPx != null && Number.isFinite(causalMidPx) && causalMidPx > 0)) return false;
  return isNonExecutableDust(requestedResidualSize, szDecimals, causalMidPx, minNotionalUsd);
}
