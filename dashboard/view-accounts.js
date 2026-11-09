/** llmux account usage.
 *
 * A numeric table, deliberately: the payload has no time series, so any gauge or
 * chart here would be invented data. Every cell is either a real upstream value
 * or the word "unknown" - never 0, never "unlimited".
 *
 * Two kinds of number are kept apart because they mean different things:
 *   - window utilization/reset: the provider's own limit accounting, as observed
 *     by llmux (with its own fetch timestamp and source, shown per row),
 *   - totals: counters the llmux proxy has accumulated over its lifetime, which
 *     are not a bill and not a provider quota.
 */

import { cell, el, headerCell } from './dom.js';
import {
  UNKNOWN,
  formatDurationSeconds,
  formatEpochMillis,
  formatEpochSeconds,
  formatInt,
  formatPercent,
  formatTimestamp,
  relativeFromEpochMillis,
  relativeTime,
} from './format.js';
import { WINDOW_LABELS, accountCondition, cooldownState, windowFreshness } from './usage.js';

function windowCell(label, window, now) {
  if (!window) {
    return cell(label, [el('span', { class: 'quiet', text: UNKNOWN })]);
  }
  const freshness = windowFreshness(window, now);
  return cell(label, [
    el('span', { class: 'mono', text: formatPercent(window.utilization) }),
    el('div', { class: 'row__meta' }, [
      el('span', {
        text: window.resets_in_secs === null
          ? 'reset unknown'
          : `resets in ${formatDurationSeconds(window.resets_in_secs)}`,
        title: formatEpochSeconds(window.resets_at),
      }),
      el('span', { text: `source ${window.source === null ? UNKNOWN : window.source}` }),
      el('span', {
        text: freshness.known
          ? `read ${relativeFromEpochMillis(window.fetched_at_ms, now)}${freshness.stale ? ', stale reading' : ''}`
          : 'read time unknown',
        title: formatEpochMillis(window.fetched_at_ms),
      }),
    ]),
  ], { mono: true });
}

function accountRow(account, now) {
  const cooldown = cooldownState(account, now);
  return el('tr', { class: 'row' }, [
    cell('Account', [
      el('span', { class: 'mono', text: account.name }),
      el('div', { class: 'row__meta' }, [
        el('span', { text: `type ${account.type === null ? UNKNOWN : account.type}` }),
      ]),
    ], { mono: true, wide: true }),
    cell('Condition', [
      el('span', { text: accountCondition(account, now) }),
      el('div', { class: 'row__meta' }, [
        el('span', {
          text: account.healthy === null ? 'health unknown' : account.healthy ? 'healthy' : 'not healthy',
        }),
        el('span', {
          text: account.paused === null ? 'pause state unknown' : account.paused ? 'paused' : 'not paused',
        }),
        cooldown.active
          ? el('span', { text: `cooldown ${formatDurationSeconds(cooldown.secondsLeft)} left`, title: formatEpochSeconds(account.cooldown_until) })
          : el('span', { text: 'no cooldown' }),
      ]),
    ]),
    cell('In flight', [el('span', { class: 'mono', text: formatInt(account.in_flight) })], { mono: true }),
    windowCell(WINDOW_LABELS.five_hour, account.windows.five_hour, now),
    windowCell(WINDOW_LABELS.seven_day, account.windows.seven_day, now),
    cell('Proxy-lifetime totals', [
      el('div', { class: 'stack' }, [
        el('span', { class: 'mono', text: `requests ${formatInt(account.totals.requests)}` }),
        el('span', { class: 'mono', text: `in ${formatInt(account.totals.input_tokens)}` }),
        el('span', { class: 'mono', text: `out ${formatInt(account.totals.output_tokens)}` }),
      ]),
    ], { mono: true }),
    cell('Proxy session', [
      el('div', { class: 'stack' }, [
        el('span', { class: 'mono', text: `requests ${formatInt(account.session.requests)}` }),
        el('span', { class: 'mono', text: `ok ${formatInt(account.session.ok)}` }),
        el('span', { class: 'mono', text: `errors ${formatInt(account.session.errors)}` }),
        el('span', { class: 'mono', text: `in ${formatInt(account.session.tokens_in)}` }),
        el('span', { class: 'mono', text: `out ${formatInt(account.session.tokens_out)}` }),
      ]),
    ], { mono: true }),
  ]);
}

export function renderAccounts({ usage, loading, error, now = Date.now(), onReload }) {
  const body = [];

  if (error) {
    body.push(el('div', { class: 'notice notice--strong', role: 'alert' }, [
      el('strong', { text: error.title }),
      error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
      el('span', { class: 'notice__detail', text: error.recovery }),
      el('button', { class: 'button', type: 'button', text: 'Retry loading', on: { click: onReload } }),
    ]));
  }

  if (usage && !usage.available) {
    body.push(el('div', { class: 'notice notice--strong', role: 'status' }, [
      el('strong', { text: 'Usage is unavailable' }),
      el('span', {
        class: 'notice__detail',
        text: usage.error || 'The server could not read authorised llmux telemetry.',
      }),
      el('span', {
        class: 'notice__detail',
        text: 'Unavailable means unknown. No usage number is shown, and none of these accounts '
          + 'should be read as idle or at zero.',
      }),
      el('button', { class: 'button', type: 'button', text: 'Retry loading', on: { click: onReload } }),
    ]));
  } else if (usage && usage.accounts.length === 0 && !loading) {
    body.push(el('p', { class: 'empty', text: 'llmux reported no accounts.' }));
  } else if (usage) {
    body.push(el('p', { class: 'tablenote' }, [
      el('span', {
        text: `read from llmux ${relativeTime(usage.fetched_at, now)}`,
        title: formatTimestamp(usage.fetched_at),
      }),
      el('span', { text: 'window values are the provider limit accounting llmux observed, each with its own read time' }),
      el('span', { text: 'totals are counters the llmux proxy accumulated, not a bill and not a provider quota' }),
      usage.dropped > 0
        ? el('span', { text: `${usage.dropped} account entries were dropped as unrecognised` })
        : null,
    ]));
    body.push(el('div', { class: 'tablewrap' }, [
      el('table', { class: 'table' }, [
        el('caption', { class: 'sr-only', text: 'llmux accounts, usage windows and proxy counters' }),
        el('thead', {}, [
          el('tr', {}, [
            headerCell('Account', { mono: true }),
            headerCell('Condition'),
            headerCell('In flight', { mono: true }),
            headerCell(WINDOW_LABELS.five_hour, { mono: true }),
            headerCell(WINDOW_LABELS.seven_day, { mono: true }),
            headerCell('Proxy-lifetime totals', { mono: true }),
            headerCell('Proxy session', { mono: true }),
          ]),
        ]),
        el('tbody', {}, usage.accounts.map((account) => accountRow(account, now))),
      ]),
    ]));
  }

  if (loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading account usage...' }));

  return el('section', { class: 'panel', 'aria-labelledby': 'accounts-heading' }, [
    el('div', { class: 'panel__head' }, [
      el('h1', { class: 'panel__title', id: 'accounts-heading', text: 'Accounts' }),
      el('p', {
        class: 'panel__intent',
        text: 'Provider accounts the review service borrows through llmux. This page is a reading of '
          + 'llmux state, not billing, and it never displays credentials.',
      }),
    ]),
    ...body,
  ]);
}
