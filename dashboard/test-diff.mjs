/** Lines of a prompt for the compare view. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { splitLines } from './diff.js';

describe('splitLines', () => {
  it('does not invent a line for a trailing newline, and keeps CRLF text whole', () => {
    assert.deepEqual(splitLines('a\nb\n'), ['a', 'b']);
    assert.deepEqual(splitLines('a\r\nb'), ['a', 'b']);
    assert.deepEqual(splitLines('\n'), ['']);
  });

  it('reads empty or missing text as no lines', () => {
    assert.deepEqual(splitLines(''), []);
    assert.deepEqual(splitLines(null), []);
    assert.deepEqual(splitLines(undefined), []);
  });
});
