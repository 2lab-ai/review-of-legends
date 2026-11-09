/** Every dashboard module parses.
 *
 * test-view-agents.mjs, test-view-queue.mjs and test-view-run.mjs (via
 * dom-shim.js) and test-agents-handlers.mjs execute the Agents, queue and
 * attempt views and the Agents handlers. This `node --check` gate still catches
 * parse errors in modules the executable tests might not import.
 */

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

const dir = fileURLToPath(new URL('./', import.meta.url));

describe('dashboard modules parse', () => {
  const modules = readdirSync(dir).filter((name) => name.endsWith('.js')).sort();
  assert.ok(modules.includes('app.js') && modules.includes('view-agents.js'), 'the DOM-bound modules are in the set');
  for (const name of modules) {
    it(name, () => {
      execFileSync(process.execPath, ['--check', name], { cwd: dir, stdio: ['ignore', 'ignore', 'pipe'] });
    });
  }
});
