export type LiveScopeSignal = {
  action: string;
  username: string;
};

export function liveScopeSkipReason(
  signal: LiveScopeSignal,
  feedFilter: string,
  allow: ReadonlySet<string>,
  copyAllFollowed: boolean,
): string | null {
  // Always permit closes to unwind positions already owned by this service.
  if (signal.action === 'close') return null;

  const explicitlyAllowlisted = allow.has(signal.username);
  if (feedFilter !== 'following' && !explicitlyAllowlisted) {
    return 'discovery_surface_not_live_scoped';
  }
  if (allow.size > 0 && !explicitlyAllowlisted) return 'trader_not_allowlisted';
  if (allow.size === 0 && !copyAllFollowed) return 'no_live_trader_scope';
  return null;
}
