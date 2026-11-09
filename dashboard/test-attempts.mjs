/** Failure detail on an agent row: cause labels, success hops excluded, agent-level output. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { failureDetail, hopCause, hopFacts, omittedNote } from './attempts.js';

const REJECTED = 'invalid_request_error: codex upstream: {"detail":"The \'gpt-6-astra\' model is not supported when using Codex with a ChatGPT account."}';

describe('hopCause', () => {
  it('separates out-of-quota from every other failure', () => {
    assert.equal(
      hopCause({ outcome: 'quota_exhausted', retry_after_seconds: 3600 }),
      'Out of quota — the proxy has no usage left for this model (retry after 3600s)',
    );
    assert.equal(hopCause({ outcome: 'quota_exhausted' }), 'Out of quota — the proxy has no usage left for this model');
    assert.equal(hopCause({ outcome: 'rate_limited', retry_after_seconds: 2.4 }), 'Rate-limited (burst) — the proxy asked to wait 2s');
    assert.equal(hopCause({ outcome: 'rate_limited' }), 'Rate-limited (burst)');
    assert.doesNotMatch(hopCause({ outcome: 'rate_limited', retry_after_seconds: 2 }), /retried/, 'a last attempt is never retried');
    assert.equal(hopCause({ outcome: 'transient_error' }), 'Transient — timeout / 5xx / connection');
    assert.equal(hopCause({ outcome: 'no_json' }), 'Reply had no JSON object');
    assert.equal(hopCause({ outcome: 'no_json_after_nudge' }), 'Reply had no JSON object');
    assert.equal(hopCause({ outcome: 'nudge_failed' }), 'The follow-up asking for JSON failed');
    assert.equal(hopCause({ outcome: 'effort_rejected' }), 'The effort setting was rejected — retried without it');
    assert.equal(hopCause({ outcome: 'failed_without_effort' }), 'Failed again without the effort setting');
    assert.equal(hopCause({ outcome: 'proxy_went_away' }), 'proxy went away');
    assert.equal(hopCause({}), 'unknown outcome');
  });
});

describe('permanent errors by status', () => {
  it('calls a 4xx a rejection', () => {
    assert.equal(hopCause({ outcome: 'permanent_error', http_status: 400 }), 'Rejected — the upstream refused the request (not a quota problem)');
    assert.equal(failureDetail({ attempt_log: [{ model: 'a', attempt: 1, outcome: 'permanent_error', http_status: 404 }] }).summary,
      'Why: a — rejected by upstream (HTTP 404)');
  });

  it('calls a 2xx it could not use an unusable response, not a rejection', () => {
    assert.equal(
      hopCause({ outcome: 'permanent_error', http_status: 200 }),
      'Unusable response — the upstream answered but the reply could not be used (not a quota problem)',
    );
    assert.equal(failureDetail({ attempt_log: [{ model: 'a', attempt: 1, outcome: 'permanent_error', http_status: 204 }] }).summary,
      'Why: a — unusable response (HTTP 204)');
  });

  it('stays neutral when the status is unknown or not an answer', () => {
    for (const hop of [{ outcome: 'permanent_error' }, { outcome: 'permanent_error', http_status: '400' }, { outcome: 'permanent_error', http_status: 302 }]) {
      assert.equal(hopCause(hop), 'Failed — not a quota problem');
    }
    assert.equal(failureDetail({ attempt_log: [{ model: 'a', attempt: 1, outcome: 'permanent_error' }] }).summary, 'Why: a — failed');
  });
});

describe('rate-limited hops', () => {
  it('name the announced window without claiming a retry happened', () => {
    const detail = failureDetail({ attempt_log: [{ model: 'grok-4.6', attempt: 1, outcome: 'rate_limited', http_status: 429, retry_after_seconds: 7 }] });
    assert.equal(detail.summary, 'Why: grok-4.6 — rate-limited (asked to wait 7s) (HTTP 429)');
    assert.equal(detail.hops[0].cause, 'Rate-limited (burst) — the proxy asked to wait 7s');
    assert.equal(failureDetail({ attempt_log: [{ model: 'grok-4.6', attempt: 1, outcome: 'rate_limited' }] }).summary, 'Why: grok-4.6 — rate-limited');
  });
});

describe('failureDetail', () => {
  it('explains the reported route: astra refused, grok answered', () => {
    const detail = failureDetail({
      id: 'correctness',
      route: 'gpt-6-astra[1m] ✗ permanent error → grok-4.6 ✓',
      attempt_log: [
        { model: 'gpt-6-astra[1m]', attempt: 1, outcome: 'permanent_error', http_status: 400, elapsed_seconds: 0.412, error: REJECTED },
        { model: 'grok-4.6', attempt: 1, outcome: 'succeeded', http_status: 200, elapsed_seconds: 349.7 },
      ],
    });
    assert.equal(detail.summary, 'Why: gpt-6-astra[1m] — rejected by upstream (HTTP 400)');
    assert.equal(detail.hops.length, 1, 'the succeeded hop is not a failure');
    assert.equal(detail.hops[0].text, REJECTED);
    assert.equal(detail.hops[0].cause, 'Rejected — the upstream refused the request (not a quota problem)');
    assert.deepEqual(hopFacts(detail.hops[0]), ['attempt 1', 'HTTP 400', '0.4s']);
    assert.equal(detail.error, null);
    assert.equal(detail.omitted, 0);
  });

  it('says how many failed hops the row had no room for', () => {
    const detail = failureDetail({
      attempt_log: [{ model: 'grok-4.6', attempt: 14, outcome: 'transient_error', http_status: 503 }],
      attempt_log_omitted: 13,
    });
    assert.equal(detail.omitted, 13);
    assert.equal(omittedNote(detail.omitted), '13 earlier hops not shown');
    assert.equal(omittedNote(1), '1 earlier hop not shown');
    assert.equal(failureDetail({ attempt_log_omitted: 2 }).summary, 'Why: 2 earlier hops not shown');
    for (const junk of [0, -1, 1.5, '3', true]) {
      assert.equal(failureDetail({ attempt_log: [{ model: 'a', attempt: 1, outcome: 'no_json' }], attempt_log_omitted: junk }).omitted, 0);
    }
  });

  it('excludes every succeeded variant and returns null when nothing failed', () => {
    const agent = {
      attempt_log: [
        { model: 'a', attempt: 1, outcome: 'succeeded' },
        { model: 'a', attempt: 1, outcome: 'succeeded_after_nudge' },
        { model: 'a', attempt: 1, outcome: 'succeeded_without_effort' },
      ],
    };
    assert.equal(failureDetail(agent), null);
    assert.equal(failureDetail({}), null);
    assert.equal(failureDetail(null), null);
    assert.equal(failureDetail({ attempt_log: 'junk', error: '  ' }), null);
  });

  it('labels quota apart from a burst and reads repeated retries once', () => {
    const detail = failureDetail({
      attempt_log: [
        { model: 'grok-4.6', attempt: 1, outcome: 'rate_limited', http_status: 429, retry_after_seconds: 2 },
        { model: 'grok-4.6', attempt: 2, outcome: 'transient_error', http_status: 503, error: 'HTTP 503: upstream down' },
        { model: 'grok-4.6', attempt: 3, outcome: 'transient_error', http_status: 503, error: 'HTTP 503: upstream down' },
        { model: 'grok-4.5', attempt: 1, outcome: 'quota_exhausted', http_status: 429, retry_after_seconds: 3600 },
        null,
        { model: 'x', attempt: 1 },
      ],
    });
    assert.equal(
      detail.summary,
      'Why: grok-4.6 — rate-limited (asked to wait 2s) (HTTP 429); grok-4.6 — transient error (HTTP 503); grok-4.5 — out of quota (HTTP 429)',
    );
    assert.equal(detail.hops.length, 4);
    assert.deepEqual(hopFacts(detail.hops[3]), ['attempt 1', 'HTTP 429', 'retry after 3600s']);
    assert.equal(detail.hops[3].text, null);
  });

  it('shows an agent-level failure even without a failed hop', () => {
    const detail = failureDetail({ status: 'failed', summary: 'agent timed out after 1800s', error: 'timed out after 1800 seconds' });
    assert.equal(detail.summary, 'Why: the agent failed (output below)');
    assert.deepEqual(detail.hops, []);
    assert.equal(detail.error, 'timed out after 1800 seconds');
  });

  it('never trusts the types it is given', () => {
    const detail = failureDetail({
      attempt_log: [{ model: 7, attempt: '1', outcome: 'no_json', http_status: '200', elapsed_seconds: -1, retry_after_seconds: Infinity, error: 42 }],
    });
    assert.deepEqual(detail.hops[0], {
      model: 'unknown model',
      outcome: 'no_json',
      cause: 'Reply had no JSON object',
      attempt: null,
      http_status: null,
      retry_after_seconds: null,
      elapsed_seconds: null,
      text: null,
    });
    assert.deepEqual(hopFacts(detail.hops[0]), []);
    assert.equal(detail.summary, 'Why: unknown model — no JSON in the reply');
  });
});
