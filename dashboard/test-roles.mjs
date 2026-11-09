import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { canAct, describeRole, isOwner, resolveView, roleOf, visibleViews } from './roles.js';

describe('roles', () => {
  it('reads only the two server roles and treats anything else as unknown', () => {
    assert.equal(roleOf({ role: 'admin' }), 'admin');
    assert.equal(roleOf({ role: 'viewer' }), 'viewer');
    assert.equal(roleOf({ role: 'root' }), null);
    assert.equal(roleOf(null), null);
  });

  it('lets only the admin act', () => {
    assert.equal(canAct({ role: 'admin' }), true);
    assert.equal(canAct({ role: 'viewer' }), false);
    assert.equal(canAct({}), false);
  });

  it('tells the owner from an added admin; only an admin can be the owner', () => {
    assert.equal(isOwner({ role: 'admin', owner: true }), true);
    assert.equal(isOwner({ role: 'admin', owner: false }), false);
    assert.equal(isOwner({ role: 'admin' }), false);
    assert.equal(isOwner({ role: 'viewer', owner: true }), false);
    assert.equal(isOwner({ role: 'admin', owner: 'true' }), false);
    assert.equal(isOwner(null), false);
    assert.equal(describeRole({ role: 'admin', owner: true }), 'owner');
    assert.equal(describeRole({ role: 'admin', owner: false }), 'admin');
    // An added admin acts and sees every admin view, like the owner.
    assert.equal(canAct({ role: 'admin', owner: false }), true);
    assert.deepEqual(visibleViews({ role: 'admin', owner: false }), visibleViews({ role: 'admin', owner: true }));
  });

  it('labels and scopes the navigation per role', () => {
    assert.equal(describeRole({ role: 'viewer' }), 'read-only');
    assert.equal(describeRole({ role: 'admin' }), 'admin');
    assert.deepEqual(visibleViews({ role: 'admin' }), ['active', 'attention', 'history', 'accounts', 'agents', 'settings']);
    assert.deepEqual(visibleViews({ role: 'viewer' }), ['active', 'attention', 'history', 'agents', 'settings']);
  });

  it('sends a viewer deep-linking an admin-only view to the queue, admin stays put', () => {
    assert.equal(resolveView({ role: 'viewer' }, 'accounts'), 'active');
    assert.equal(resolveView({ role: 'viewer' }, 'settings'), 'settings');
    assert.equal(resolveView({ role: 'viewer' }, 'agents'), 'agents', 'a viewer reads the agents like Settings');
    assert.equal(resolveView({ role: 'viewer' }, 'history'), 'history');
    assert.equal(resolveView({ role: 'admin' }, 'accounts'), 'accounts');
    assert.equal(resolveView(null, 'accounts'), 'active');
    // Attempt detail is not a nav entry and must stay reachable for every role
    // (review round on PR #16: the first cut bounced `run` for admins too).
    assert.equal(resolveView({ role: 'viewer' }, 'run'), 'run');
    assert.equal(resolveView({ role: 'admin' }, 'run'), 'run');
    assert.equal(resolveView(null, 'run'), 'run');
  });

  it('bounces before the accounts branch can load usage (caller ordering)', () => {
    // app.js is DOM-bound and has no unit harness. Pin the live defect: the role
    // bounce must be real code (not a comment), must navigate and return, and must
    // sit immediately before the accounts branch that calls loadUsage().
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const handler = app.slice(app.indexOf('function onRouteChange('));
    const block = handler.match(
      /^[ \t]*if \(resolveView\(state\.identity, route\.view\) !== route\.view\) \{\n([\s\S]*?)\n[ \t]*\}\n[ \t]*if \(route\.view === 'accounts'\)/m,
    );
    assert.ok(block, 'role bounce is an if-block directly before the accounts branch');
    const code = block[1].split('\n').filter((line) => !line.trim().startsWith('//')).join('\n');
    assert.match(code, /navigate\(\{ view: resolveView\(state\.identity, route\.view\) \}\);/);
    assert.match(code, /^\s*return;\s*$/m);
  });

  it('keeps a role-hidden nav entry off screen despite the flex display rule', () => {
    // Live regression 2026-09-18: `.nav__item { display: flex }` beat the UA
    // `[hidden]` rule, so the viewer still saw the Accounts tab.
    const css = readFileSync(fileURLToPath(new URL('./styles.css', import.meta.url)), 'utf8');
    assert.match(css, /\.nav__item\[hidden\]\s*\{\s*display:\s*none;/);
  });
});
