import assert from 'node:assert/strict';
import test from 'node:test';
import { liveScopeSkipReason } from '../src/live-scope.js';

const open = { action: 'open', username: 'alice' };

test('default copy-all scope applies only to the following surface', () => {
  const allow = new Set<string>();
  assert.equal(liveScopeSkipReason(open, 'following', allow, true), null);
  assert.equal(liveScopeSkipReason(open, 'all', allow, true), 'discovery_surface_not_live_scoped');
  assert.equal(liveScopeSkipReason(open, 'trending', allow, true), 'discovery_surface_not_live_scoped');
});

test('broad discovery surfaces require an explicit trader allowlist entry', () => {
  const allow = new Set(['alice']);
  assert.equal(liveScopeSkipReason(open, 'all', allow, true), null);
  assert.equal(liveScopeSkipReason(open, 'trending', allow, true), null);
  assert.equal(
    liveScopeSkipReason({ ...open, username: 'bob' }, 'all', allow, true),
    'discovery_surface_not_live_scoped',
  );
});

test('closes remain permitted on every surface so owned positions can unwind', () => {
  assert.equal(
    liveScopeSkipReason({ action: 'close', username: 'alice' }, 'trending', new Set(), false),
    null,
  );
});
