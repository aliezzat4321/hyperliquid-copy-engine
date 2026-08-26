import 'dotenv/config';

export const INVO_TOKEN = process.env.INVO_ACCESS_TOKEN ?? process.env.INVO_TOKEN ?? '';
export const INVO_REFRESH_TOKEN = process.env.INVO_REFRESH_TOKEN ?? '';
export const HL_AGENT_KEY = process.env.HL_AGENT_KEY ?? '';

function jwtPayload(token: string): any | null {
  try {
    const raw = token.replace(/^Bearer\s+/i, '').split('.')[1];
    if (!raw) return null;
    const normalized = raw.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
    return JSON.parse(Buffer.from(padded, 'base64').toString('utf8'));
  } catch {
    return null;
  }
}

export function resolveWalletAddress(): string {
  const explicit = (process.env.WALLET_ADDRESS ?? '').trim();
  if (explicit) return explicit;
  for (const token of [INVO_TOKEN, INVO_REFRESH_TOKEN]) {
    const payload = jwtPayload(token);
    const address = payload?.trading_account?.wallet_address ?? payload?.trading_account?.wallet?.evm_address;
    if (typeof address === 'string' && /^0x[a-fA-F0-9]{40}$/.test(address)) return address;
  }
  return '';
}

export function validateEnv(live: boolean) {
  const missing: string[] = [];
  if (!INVO_TOKEN && !INVO_REFRESH_TOKEN) missing.push('INVO_ACCESS_TOKEN/INVO_TOKEN or INVO_REFRESH_TOKEN');
  if (!resolveWalletAddress()) missing.push('WALLET_ADDRESS (or a JWT containing trading_account.wallet_address)');
  if (live && !HL_AGENT_KEY) missing.push('HL_AGENT_KEY');
  if (missing.length) throw new Error(`Missing environment variables: ${missing.join(', ')}`);
}
