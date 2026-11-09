/** Model catalog + model policy: pure projections and validation.
 *
 * Mirrors the server's rules (service.validate_model_policy) so the form can
 * refuse a bad draft before a request is made; the server remains the
 * authority and its 400 is rendered verbatim as the reason.
 *
 * Claude models are never selectable here. The review service has no Anthropic
 * account leg through llmux, so a Claude id cannot produce a review; the
 * catalog keeps those rows visible, greyed out, with the reason.
 */

export const EFFORTS = ['', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
export const MAX_FALLBACKS = 8;
export const CLAUDE_NOTE = 'Claude models cannot be used here: the review service has no Anthropic account through llmux, '
  + 'so any Claude id is refused by the service and shown greyed out in the catalog.';
export const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,79}$/;
const GROUP_ORDER = ['codex', 'grok', 'openrouter'];
const MAX_TEXT = 120;

function text(value, max = MAX_TEXT) {
  if (typeof value !== 'string') return null;
  const cleaned = value.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;
  return cleaned.length > max ? cleaned.slice(0, max) : cleaned;
}

function stringList(value, max = 16) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => typeof item === 'string' && item.trim() !== '').slice(0, max).map((item) => text(item, 80));
}

export function isClaudeId(modelId) {
  return typeof modelId === 'string' && modelId.trim().toLowerCase().startsWith('claude');
}

/** GET /api/models -> exactly the fields the form renders. */
export function projectCatalog(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const rows = Array.isArray(source.models) ? source.models : [];
  const models = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue;
    const id = text(row.id, 80);
    if (!id) continue;
    const aliases = stringList(row.aliases, 8);
    // An alias resolves on the proxy to the same leg as its id, so it is judged too.
    const usable = row.usable === true && ![id, ...aliases].some(isClaudeId);
    models.push({
      id,
      name: text(row.name) || id,
      group: (text(row.group, 32) || 'other').toLowerCase(),
      group_label: text(row.group_label) || text(row.group, 32) || 'Other',
      efforts: stringList(row.efforts, 12).filter((level) => EFFORTS.includes(level)),
      aliases,
      max_context: typeof row.max_context === 'number' && Number.isFinite(row.max_context) ? row.max_context : null,
      usable,
      reason: usable ? null : (text(row.reason, 300) || CLAUDE_NOTE),
    });
  }
  return {
    available: source.available === true,
    fetched_at: text(source.fetched_at, 64),
    error: text(source.error, 300),
    models,
  };
}

/** Usable groups first in a fixed order, Claude last so the greyed rows are visible but never in the way. */
export function groupCatalog(models) {
  const groups = new Map();
  for (const model of Array.isArray(models) ? models : []) {
    const key = model.group || 'other';
    if (!groups.has(key)) groups.set(key, { group: key, label: model.group_label || key, models: [] });
    groups.get(key).models.push(model);
  }
  const rank = (key) => {
    if (key === 'claude') return 100;
    const index = GROUP_ORDER.indexOf(key);
    return index === -1 ? 50 : index;
  };
  return [...groups.values()].sort((a, b) => rank(a.group) - rank(b.group) || a.label.localeCompare(b.label));
}

export function findModel(catalog, modelId) {
  const models = catalog && Array.isArray(catalog.models) ? catalog.models : [];
  return models.find((model) => model.id === modelId)
    || models.find((model) => model.aliases.includes(modelId))
    || null;
}

/** A catalog the retry combobox and the settings form can offer choices from. */
export function catalogUsable(catalog) {
  return !!(catalog && catalog.available === true && Array.isArray(catalog.models) && catalog.models.length > 0);
}

/** Whether the run view must (re)request the catalog for a retryable run.
 *
 * Judged on the run actually loaded, not on a cached row, and on whether the
 * catalog is usable rather than on whether a fetch once completed: a deep link
 * lands with no cached run, a run open in the tab can fail live, and a fetch
 * that answered `available: false` during an outage must not pin the combobox
 * disabled for the rest of the session. */
export function shouldLoadCatalog(settings, retryable) {
  if (!retryable || !settings || settings.loading) return false;
  return !catalogUsable(settings.catalog);
}

/** Efforts a model advertises, else the full menu; the empty value means "proxy default". */
export function effortOptions(catalog, modelId) {
  const model = findModel(catalog, modelId);
  const levels = model && model.efforts.length > 0 ? model.efforts : EFFORTS.filter((level) => level !== '');
  return ['', ...levels.filter((level) => level !== '')];
}

function policyShape(value) {
  const source = value && typeof value === 'object' ? value : {};
  const model = text(source.model, 80);
  if (!model) return null;
  return {
    model,
    effort: text(source.effort, 16) || '',
    fallback_models: stringList(source.fallback_models, MAX_FALLBACKS),
    source: text(source.source, 16) || null,
  };
}

/** GET/PUT/DELETE /api/settings/model-policy -> {policy, source, updated_at, updated_by, config_policy}. */
export function projectPolicyPayload(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  return {
    policy: policyShape(source.policy),
    source: text(source.source, 16) || 'config',
    updated_at: text(source.updated_at, 64),
    updated_by: text(source.updated_by, 254),
    config_policy: policyShape(source.config_policy),
  };
}

/** A retry request's model_policy (already validated by the server) or null. */
export function projectRequestPolicy(value) {
  const shaped = policyShape(value);
  if (!shaped) return null;
  delete shaped.source;
  return shaped;
}

export function policyEquals(a, b) {
  if (!a || !b) return a === b;
  return a.model === b.model
    && (a.effort || '') === (b.effort || '')
    && a.fallback_models.length === b.fallback_models.length
    && a.fallback_models.every((item, index) => item === b.fallback_models[index]);
}

export function describePolicy(policy) {
  if (!policy || !policy.model) return 'unknown';
  const effort = policy.effort ? ` · ${policy.effort}` : ' · proxy default effort';
  const chain = policy.fallback_models && policy.fallback_models.length > 0
    ? ` → ${policy.fallback_models.join(' → ')}`
    : ' (no fallback)';
  return `${policy.model}${effort}${chain}`;
}

/** Client-side mirror of service.validate_model_policy. Returns {ok, errors}. */
export function validatePolicyDraft(draft, catalog = null) {
  const errors = [];
  const source = draft && typeof draft === 'object' ? draft : {};
  const model = typeof source.model === 'string' ? source.model.trim() : '';
  const fallbacks = Array.isArray(source.fallback_models) ? source.fallback_models.map((item) => String(item || '').trim()) : [];
  const effort = typeof source.effort === 'string' ? source.effort.trim().toLowerCase() : '';

  const checkId = (id, label) => {
    if (id === '') {
      errors.push(`${label}: choose a model`);
      return;
    }
    if (!MODEL_ID.test(id)) errors.push(`${label}: "${id}" is not a valid model id`);
    if (isClaudeId(id)) errors.push(`${label}: ${CLAUDE_NOTE}`);
    const known = findModel(catalog, id);
    if (known && !known.usable) errors.push(`${label}: ${known.reason || 'not usable'}`);
  };

  checkId(model, 'Default model');
  if (!EFFORTS.includes(effort)) errors.push(`Effort: "${effort}" is not a known level`);
  if (fallbacks.length > MAX_FALLBACKS) errors.push(`Fallbacks: at most ${MAX_FALLBACKS} models`);
  const seen = new Set([model]);
  fallbacks.forEach((id, index) => {
    const label = `Fallback ${index + 1}`;
    checkId(id, label);
    if (id !== '' && seen.has(id)) errors.push(`${label}: "${id}" is already in the ladder`);
    seen.add(id);
  });
  return { ok: errors.length === 0, errors };
}

/** What the API accepts: trimmed, lower-cased effort, no source. */
export function policyRequestBody(draft) {
  return {
    model: String(draft.model || '').trim(),
    effort: String(draft.effort || '').trim().toLowerCase(),
    fallback_models: (Array.isArray(draft.fallback_models) ? draft.fallback_models : []).map((item) => String(item || '').trim()),
  };
}

/* ------------------------------------------------ draft edits (pure) */

export function draftFromPolicy(policy) {
  return policy
    ? { model: policy.model, effort: policy.effort || '', fallback_models: [...policy.fallback_models] }
    : { model: '', effort: '', fallback_models: [] };
}

export function setDraftModel(draft, model, catalog = null) {
  const next = { ...draft, model };
  // Keep the effort only if the new model advertises it; otherwise fall back to the proxy default.
  if (next.effort && !effortOptions(catalog, model).includes(next.effort)) next.effort = '';
  return next;
}

export function setDraftFallback(draft, index, model) {
  const fallback_models = [...draft.fallback_models];
  fallback_models[index] = model;
  return { ...draft, fallback_models };
}

export function addDraftFallback(draft, model = '') {
  if (draft.fallback_models.length >= MAX_FALLBACKS) return draft;
  return { ...draft, fallback_models: [...draft.fallback_models, model] };
}

export function removeDraftFallback(draft, index) {
  return { ...draft, fallback_models: draft.fallback_models.filter((_item, i) => i !== index) };
}

export function moveDraftFallback(draft, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= draft.fallback_models.length) return draft;
  const fallback_models = [...draft.fallback_models];
  [fallback_models[index], fallback_models[target]] = [fallback_models[target], fallback_models[index]];
  return { ...draft, fallback_models };
}

/** Tokens on an agent row or a totals object: "in 1,000 / out 200" or null when unreported. */
export function formatTokens(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const int = (value) => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null);
  const input = int(entry.input_tokens);
  const output = int(entry.output_tokens);
  if (input === null && output === null) return null;
  const parts = [`in ${(input || 0).toLocaleString('en-US')}`, `out ${(output || 0).toLocaleString('en-US')}`];
  const cacheRead = int(entry.cache_read_tokens);
  if (cacheRead) parts.push(`cache read ${cacheRead.toLocaleString('en-US')}`);
  const cacheWrite = int(entry.cache_write_tokens);
  if (cacheWrite) parts.push(`cache write ${cacheWrite.toLocaleString('en-US')}`);
  return parts.join(' / ');
}
