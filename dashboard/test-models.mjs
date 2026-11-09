/** Model catalog + policy helpers: projection, grouping, validation, draft edits. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  CLAUDE_NOTE,
  addDraftFallback,
  catalogUsable,
  findModel,
  shouldLoadCatalog,
  describePolicy,
  draftFromPolicy,
  effortOptions,
  formatTokens,
  groupCatalog,
  moveDraftFallback,
  policyEquals,
  policyRequestBody,
  projectCatalog,
  projectPolicyPayload,
  projectRequestPolicy,
  removeDraftFallback,
  setDraftModel,
  validatePolicyDraft,
} from './models.js';

const CATALOG = projectCatalog({
  available: true,
  fetched_at: '2026-09-17T01:00:00+00:00',
  models: [
    { id: 'claude-opus-5[1m]', name: 'Claude Opus 5 [1M]', group: 'claude', group_label: 'Anthropic Claude', efforts: ['low', 'high'], usable: false, reason: 'Claude models are not available to this service' },
    { id: 'gpt-6-astra[1m]', name: 'GPT-6-Astra [1M]', group: 'codex', group_label: 'OpenAI Codex', efforts: ['low', 'xhigh', 'ultra'], max_context: 1000000, usable: true, reason: null },
    { id: 'grok-4.6', name: 'Grok 4.6', group: 'grok', group_label: 'xAI Grok', efforts: ['low', 'xhigh'], max_context: 500000, usable: true },
    { id: 'or-free', name: 'Free router', group: 'openrouter', group_label: 'OpenRouter', efforts: [], usable: true },
    { id: '', name: 'broken' },
    'junk',
  ],
});

describe('projectCatalog', () => {
  it('keeps only well-formed rows and never lets a claude id be usable', () => {
    assert.deepEqual(CATALOG.models.map((m) => m.id), ['claude-opus-5[1m]', 'gpt-6-astra[1m]', 'grok-4.6', 'or-free']);
    assert.equal(CATALOG.models[0].usable, false);
    assert.match(CATALOG.models[0].reason, /Claude models are not available/);
    const smuggled = projectCatalog({ available: true, models: [{ id: 'claude-x', usable: true }] });
    assert.equal(smuggled.models[0].usable, false);
    assert.equal(smuggled.models[0].reason, CLAUDE_NOTE);
  });

  it('reports unavailability explicitly', () => {
    const down = projectCatalog({ available: false, error: 'llmux model catalog could not be reached', models: [] });
    assert.equal(down.available, false);
    assert.equal(down.error, 'llmux model catalog could not be reached');
    assert.deepEqual(projectCatalog(null).models, []);
  });

  it('groups usable providers first and claude last', () => {
    assert.deepEqual(groupCatalog(CATALOG.models).map((g) => g.group), ['codex', 'grok', 'openrouter', 'claude']);
  });

  it('offers the model efforts plus the proxy default', () => {
    assert.deepEqual(effortOptions(CATALOG, 'grok-4.6'), ['', 'low', 'xhigh']);
    assert.deepEqual(effortOptions(CATALOG, 'or-free'), ['', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
    assert.deepEqual(effortOptions(CATALOG, 'unknown-model'), ['', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
  });
});

describe('policy payloads', () => {
  it('projects the settings payload and describes the ladder', () => {
    const projected = projectPolicyPayload({
      policy: { model: 'gpt-6-astra[1m]', effort: 'xhigh', fallback_models: ['grok-4.6'], source: 'dashboard' },
      source: 'dashboard',
      updated_at: '2026-09-17T01:00:00+00:00',
      updated_by: 'owner@company.example',
      config_policy: { model: 'gpt-6-astra[1m]', effort: 'xhigh', fallback_models: ['grok-4.6'], source: 'config' },
    });
    assert.equal(projected.source, 'dashboard');
    assert.equal(projected.updated_by, 'owner@company.example');
    assert.equal(describePolicy(projected.policy), 'gpt-6-astra[1m] · xhigh → grok-4.6');
    assert.equal(describePolicy({ model: 'grok-4.6', effort: '', fallback_models: [] }), 'grok-4.6 · proxy default effort (no fallback)');
    assert.equal(describePolicy(null), 'unknown');
    assert.equal(projectPolicyPayload({}).policy, null);
  });

  it('projects a request policy without a source', () => {
    assert.deepEqual(projectRequestPolicy({ model: 'grok-4.6', effort: 'xhigh', fallback_models: [], source: 'request' }),
      { model: 'grok-4.6', effort: 'xhigh', fallback_models: [] });
    assert.equal(projectRequestPolicy(null), null);
  });

  it('compares policies by ladder and effort only', () => {
    const a = { model: 'grok-4.6', effort: 'xhigh', fallback_models: ['gpt-5.5'], source: 'config' };
    assert.ok(policyEquals(a, { ...a, source: 'dashboard' }));
    assert.ok(!policyEquals(a, { ...a, fallback_models: [] }));
    assert.ok(!policyEquals(a, { ...a, effort: '' }));
  });
});

describe('validatePolicyDraft', () => {
  it('accepts the shipped ladder', () => {
    assert.deepEqual(validatePolicyDraft({ model: 'gpt-6-astra[1m]', effort: 'xhigh', fallback_models: ['grok-4.6'] }, CATALOG), { ok: true, errors: [] });
  });

  it('refuses claude anywhere, duplicates, unknown efforts and empty slots', () => {
    const claude = validatePolicyDraft({ model: 'claude-opus-5[1m]', effort: '', fallback_models: [] }, CATALOG);
    assert.equal(claude.ok, false);
    assert.ok(claude.errors.some((e) => e.includes('Claude')));
    const dup = validatePolicyDraft({ model: 'grok-4.6', effort: 'xhigh', fallback_models: ['grok-4.6'] }, CATALOG);
    assert.ok(dup.errors.some((e) => e.includes('already in the ladder')));
    const level = validatePolicyDraft({ model: 'grok-4.6', effort: 'ludicrous', fallback_models: [] }, CATALOG);
    assert.ok(level.errors.some((e) => e.startsWith('Effort')));
    const empty = validatePolicyDraft({ model: 'grok-4.6', effort: '', fallback_models: [''] }, CATALOG);
    assert.ok(empty.errors.some((e) => e === 'Fallback 1: choose a model'));
    const tooMany = validatePolicyDraft({ model: 'grok-4.6', effort: '', fallback_models: Array.from({ length: 9 }, (_v, i) => `m${i}`) });
    assert.ok(tooMany.errors.some((e) => e.includes('at most 8')));
  });

  it('builds the request body the API accepts', () => {
    assert.deepEqual(policyRequestBody({ model: ' grok-4.6 ', effort: 'XHIGH', fallback_models: ['gpt-5.5 '] }), { model: 'grok-4.6', effort: 'xhigh', fallback_models: ['gpt-5.5'] });
  });
});

describe('draft edits', () => {
  const base = draftFromPolicy({ model: 'gpt-6-astra[1m]', effort: 'ultra', fallback_models: ['grok-4.6', 'or-free'] });

  it('starts from the policy in effect', () => {
    assert.deepEqual(base, { model: 'gpt-6-astra[1m]', effort: 'ultra', fallback_models: ['grok-4.6', 'or-free'] });
    assert.deepEqual(draftFromPolicy(null), { model: '', effort: '', fallback_models: [] });
  });

  it('drops an effort the new model does not advertise', () => {
    assert.equal(setDraftModel(base, 'grok-4.6', CATALOG).effort, '');
    assert.equal(setDraftModel({ ...base, effort: 'xhigh' }, 'grok-4.6', CATALOG).effort, 'xhigh');
  });

  it('adds, moves, removes fallbacks without mutating the input', () => {
    const added = addDraftFallback(base, 'gpt-5.5');
    assert.deepEqual(added.fallback_models, ['grok-4.6', 'or-free', 'gpt-5.5']);
    assert.deepEqual(base.fallback_models, ['grok-4.6', 'or-free']);
    assert.deepEqual(moveDraftFallback(base, 1, -1).fallback_models, ['or-free', 'grok-4.6']);
    assert.equal(moveDraftFallback(base, 0, -1), base);
    assert.deepEqual(removeDraftFallback(base, 0).fallback_models, ['or-free']);
    const full = { ...base, fallback_models: Array.from({ length: 8 }, (_v, i) => `m${i}`) };
    assert.equal(addDraftFallback(full), full);
  });
});

describe('formatTokens', () => {
  it('renders reported counters and nothing for unreported rows', () => {
    assert.equal(formatTokens({ input_tokens: 1000, output_tokens: 200, cache_read_tokens: 300 }), 'in 1,000 / out 200 / cache read 300');
    assert.equal(formatTokens({ input_tokens: 5 }), 'in 5 / out 0');
    assert.equal(formatTokens({ status: 'failed' }), null);
    assert.equal(formatTokens(null), null);
  });
});

describe('findModel', () => {
  const catalog = projectCatalog({
    available: true,
    models: [
      { id: 'claude-opus-5[1m]', aliases: ['opus'], group: 'claude', usable: false, reason: 'no' },
      { id: 'gpt-6-astra[1m]', aliases: ['astra'], group: 'codex', usable: true },
      { id: 'x-legacy', aliases: ['claude-x'], group: 'other', usable: true },
    ],
  });

  it('resolves an alias to its row so the usability verdict covers the alias', () => {
    assert.equal(findModel(catalog, 'astra').id, 'gpt-6-astra[1m]');
    assert.equal(findModel(catalog, 'opus').usable, false);
    assert.equal(findModel(catalog, 'nope'), null);
  });

  it('judges a row unusable when any of its aliases is a Claude id', () => {
    assert.equal(findModel(catalog, 'x-legacy').usable, false);
    assert.deepEqual(validatePolicyDraft({ model: 'opus', effort: '', fallback_models: [] }, catalog).ok, false);
  });
});

describe('shouldLoadCatalog', () => {
  const usable = { available: true, models: [{ id: 'grok-4.6' }] };

  it('is false for a run that cannot be retried, whatever the catalog state', () => {
    assert.equal(shouldLoadCatalog({ loading: false, loaded: false, catalog: null }, false), false);
    assert.equal(shouldLoadCatalog(null, true), false);
  });

  it('asks for the catalog when a retryable run has none, even after a completed fetch', () => {
    assert.equal(shouldLoadCatalog({ loading: false, loaded: false, catalog: null }, true), true);
    // A fetch that answered during an llmux outage must not pin the combobox disabled.
    assert.equal(shouldLoadCatalog({ loading: false, loaded: true, catalog: { available: false, models: [] } }, true), true);
    assert.equal(shouldLoadCatalog({ loading: false, loaded: true, catalog: { available: true, models: [] } }, true), true);
  });

  it('never double-fetches and stops once a usable catalog is held', () => {
    assert.equal(shouldLoadCatalog({ loading: true, loaded: false, catalog: null }, true), false);
    assert.equal(shouldLoadCatalog({ loading: false, loaded: true, catalog: usable }, true), false);
    assert.equal(catalogUsable(usable), true);
    assert.equal(catalogUsable({ available: true, models: [] }), false);
  });
});
