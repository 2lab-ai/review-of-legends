/** Value formatting: labels, unknown-vs-zero, percent semantics, link safety. */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import {
  agentProgress,
  durationBetween,
  footerText,
  formatCount,
  formatDurationSeconds,
  formatEpochMillis,
  formatEpochSeconds,
  formatInt,
  formatPercent,
  formatTimestamp,
  prLabel,
  relativeFromEpochMillis,
  relativeTime,
  retryStatusLabel,
  safeHttpUrl,
  shortRunId,
  shortSha,
  statusLabel,
  statusTone,
  triggerLabel,
} from './format.js';

const BASE = '2026-09-11T06:00:00+00:00';
const BASE_MS = Date.parse(BASE);

describe('status vocabulary', () => {
  it('labels every status the store can write', () => {
    assert.equal(statusLabel('running'), 'Running');
    assert.equal(statusLabel('succeeded'), 'Succeeded');
    assert.equal(statusLabel('failed'), 'Failed');
    assert.equal(statusLabel('interrupted'), 'Interrupted');
    assert.equal(statusLabel('stale'), 'Stale');
    assert.equal(statusLabel('skipped'), 'Skipped');
  });

  it('shows an unknown status verbatim instead of hiding it', () => {
    assert.equal(statusLabel('quorum_pending'), 'quorum_pending');
    assert.equal(statusTone('quorum_pending'), 'muted');
  });

  it('treats empty or non-string statuses as Unknown', () => {
    assert.equal(statusLabel(''), 'Unknown');
    assert.equal(statusLabel(null), 'Unknown');
    assert.equal(statusTone(null), 'muted');
  });

  it('marks the three unreviewed outcomes for attention', () => {
    for (const status of ['failed', 'interrupted', 'stale']) {
      assert.equal(statusTone(status), 'attention');
    }
    assert.equal(statusTone('running'), 'active');
    assert.equal(statusTone('succeeded'), 'quiet');
  });

  it('says baseline rows were never reviewed', () => {
    assert.equal(triggerLabel('baseline'), 'Baseline, not reviewed');
    assert.equal(triggerLabel('legacy'), 'Legacy record');
    assert.equal(triggerLabel('retrigger'), 'Retrigger');
  });

  it('describes retry requests as dispatch, not as review outcome', () => {
    assert.equal(retryStatusLabel('queued'), 'Queued for dispatch');
    assert.equal(retryStatusLabel('succeeded'), 'Dispatch completed');
    assert.equal(retryStatusLabel('mystery'), 'mystery');
  });
});

describe('numbers', () => {
  it('renders a missing number as unknown, never as zero', () => {
    assert.equal(formatInt(null), 'unknown');
    assert.equal(formatInt(undefined), 'unknown');
    assert.equal(formatInt(Number.NaN), 'unknown');
    assert.equal(formatInt(Number.POSITIVE_INFINITY), 'unknown');
    assert.equal(formatInt(0), '0');
    assert.equal(formatInt(1234567), '1,234,567');
  });

  it('reads utilization as the 0..1 fraction llmux reports', () => {
    assert.equal(formatPercent(0.25), '25%');
    assert.equal(formatPercent(0.97), '97%');
    assert.equal(formatPercent(0.061), '6.1%');
    assert.equal(formatPercent(0), '0%');
    assert.equal(formatPercent(null), 'unknown');
  });

  it('does not clamp an out-of-range upstream fraction', () => {
    assert.equal(formatPercent(1.2), '120%');
  });

  it('suffixes loaded counts with + while the server has more pages', () => {
    assert.equal(formatCount(12, false), '12');
    assert.equal(formatCount(50, true), '50+');
    assert.equal(formatCount(null, true), 'unknown');
  });
});

describe('time', () => {
  it('formats absolute UTC for tooltips', () => {
    assert.equal(formatTimestamp('2026-09-11T06:13:00+00:00'), '2026-09-11 06:13:00 UTC');
    assert.equal(formatTimestamp('not a date'), 'unknown');
    assert.equal(formatTimestamp(null), 'unknown');
  });

  it('treats llmux resets_at as epoch seconds and fetched_at_ms as millis', () => {
    assert.equal(formatEpochSeconds(2000000000), '2033-05-18 03:33:20 UTC');
    assert.equal(formatEpochMillis(1700000000000), '2023-11-14 22:13:20 UTC');
    assert.equal(formatEpochSeconds(null), 'unknown');
    assert.equal(formatEpochMillis(null), 'unknown');
  });

  it('renders relative time in both directions', () => {
    assert.equal(relativeTime(BASE, BASE_MS + 5000), 'just now');
    assert.equal(relativeTime(BASE, BASE_MS + 45000), '45s ago');
    assert.equal(relativeTime(BASE, BASE_MS + 5 * 60000), '5m ago');
    assert.equal(relativeTime(BASE, BASE_MS + 3 * 3600000), '3h ago');
    assert.equal(relativeTime(BASE, BASE_MS + 2 * 86400000), '2d ago');
    assert.equal(relativeTime(BASE, BASE_MS - 30000), 'in 30s');
    assert.equal(relativeTime('garbage', BASE_MS), 'unknown');
  });

  it('reports signal age from epoch millis', () => {
    assert.equal(relativeFromEpochMillis(BASE_MS, BASE_MS + 60000), '1m ago');
    assert.equal(relativeFromEpochMillis(null, BASE_MS), 'unknown');
  });

  it('formats coarse durations', () => {
    assert.equal(formatDurationSeconds(45), '45s');
    assert.equal(formatDurationSeconds(60), '1m');
    assert.equal(formatDurationSeconds(90), '1m 30s');
    assert.equal(formatDurationSeconds(3600), '1h');
    assert.equal(formatDurationSeconds(3660), '1h 1m');
    assert.equal(formatDurationSeconds(null), 'unknown');
    assert.equal(durationBetween(BASE, '2026-09-11T06:02:30+00:00'), '2m 30s');
    assert.equal(durationBetween(BASE, null), 'unknown');
  });
});

describe('identifiers and links', () => {
  it('shortens shas and attempt ids without losing the full value', () => {
    assert.equal(shortSha('2252e94aa1bb'), '2252e94');
    assert.equal(shortSha(''), 'unknown');
    assert.equal(shortRunId('6f1c2d3e4f5a6b7c'), '6f1c2d3e');
    assert.equal(shortRunId(null), 'unknown');
  });

  it('renders only http(s) targets as links', () => {
    assert.equal(
      safeHttpUrl('https://github.com/owner/repo/pull/12'),
      'https://github.com/owner/repo/pull/12',
    );
    assert.equal(safeHttpUrl('http://127.0.0.1:8840/x'), 'http://127.0.0.1:8840/x');
    assert.equal(safeHttpUrl('javascript:alert(1)'), null);
    assert.equal(safeHttpUrl('data:text/html,<script>'), null);
    assert.equal(safeHttpUrl('file:///etc/passwd'), null);
    assert.equal(safeHttpUrl('/relative/path'), null);
    assert.equal(safeHttpUrl(''), null);
    assert.equal(safeHttpUrl(null), null);
  });

  it('labels pull requests only when the number is real', () => {
    assert.equal(prLabel({ pr: 12 }), '#12');
    assert.equal(prLabel({ pr: null }), 'unknown');
    assert.equal(prLabel(null), 'unknown');
  });
});

describe('agent progress', () => {
  it('states the fraction rather than implying success', () => {
    assert.equal(agentProgress({ successful_agents: 3, total_agents: 5 }).label, '3/5 agents succeeded');
  });

  it('keeps an unknown numerator unknown', () => {
    assert.equal(agentProgress({ successful_agents: null, total_agents: 5 }).label, 'unknown/5 agents succeeded');
  });

  it('returns nothing when the totals were never reported', () => {
    assert.equal(agentProgress({ successful_agents: 2, total_agents: null }), null);
    assert.equal(agentProgress({ successful_agents: 0, total_agents: 0 }), null);
    assert.equal(agentProgress(null), null);
  });
});

describe('footer', () => {
  it('names the version /healthz reported', () => {
    assert.equal(footerText('v1.0.3'), 'Review of Legends admin dashboard · v1.0.3');
  });

  it('keeps the static text when there is no version to name', () => {
    for (const missing of ['', null, undefined, 3, {}]) {
      assert.equal(footerText(missing), 'Review of Legends admin dashboard');
    }
  });

  it('falls back to the text index.html ships, so a failed /healthz changes nothing', () => {
    const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
    assert.ok(html.includes(`<span id="footer-version">${footerText(null)}</span>`));
  });

  it('is filled by app.js boot() from /healthz before the sign-in check', () => {
    const app = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
    assert.ok(app.includes("footerVersion: document.getElementById('footer-version')"));
    const start = app.indexOf('async function boot(');
    const boot = app.slice(start, app.indexOf('\nboot();', start));
    const health = boot.indexOf('api.health()');
    const fill = boot.indexOf('dom.footerVersion.textContent = footerText(health && health.version);');
    assert.ok(start !== -1 && health !== -1 && fill > health, 'boot() writes the /healthz version into the footer');
    const me = boot.indexOf('await api.me()');
    assert.ok(me !== -1 && health < me, 'before api.me(), so the sign-in page shows it too');
  });
});
