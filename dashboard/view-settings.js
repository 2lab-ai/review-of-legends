/** Settings: the model policy the review service runs with, the repositories
 * it reviews, who is admin, and what PR authors set and wrote through
 * `@review-of-legends` comments (read-only for everyone). Reviewer agents
 * have their own page (view-agents.js).
 *
 * The form edits a draft; nothing changes on the server until Save. What is in
 * effect right now is always printed above the form, with its source, so an
 * operator can tell a saved dashboard policy from the config default at a glance.
 */

import {
  OWNER_NOTE,
  adminControls,
  adminFormState,
  adminRules,
  describeAdminRow,
} from './admins.js';
import {
  describeRepositoryAgents,
  describeRepositoryProblem,
  describeRepositorySource,
  repositoryFormState,
} from './repositories.js';
import {
  FEEDBACK_NOTE,
  FEEDBACK_RESET_NOTE,
  READ_ONLY_NOTE,
  authorsCappedNote,
  describeFeedbackSource,
  describeLanguage,
  describeWelcome,
  feedbackLink,
} from './authors.js';
import { cell, el, field, headerCell, replace, statusChip } from './dom.js';
import { formatTimestamp, relativeTime } from './format.js';
import {
  CLAUDE_NOTE,
  MAX_FALLBACKS,
  describePolicy,
  effortOptions,
  groupCatalog,
  policyEquals,
  validatePolicyDraft,
} from './models.js';

export function modelOptions(catalog, selected, { allowEmpty = false, emptyLabel = 'Choose a model' } = {}) {
  const options = [];
  if (allowEmpty || !selected) options.push(el('option', { value: '', text: emptyLabel, selected: !selected }));
  const known = new Set();
  for (const group of groupCatalog(catalog ? catalog.models : [])) {
    options.push(el('optgroup', { label: group.label }, group.models.map((model) => {
      known.add(model.id);
      return el('option', {
        value: model.id,
        selected: model.id === selected,
        disabled: !model.usable,
        text: model.usable ? `${model.id} — ${model.name}` : `${model.id} — not available (Claude)`,
      });
    })));
  }
  if (selected && !known.has(selected)) {
    options.push(el('option', { value: selected, selected: true, text: `${selected} (not in the current catalog)` }));
  }
  return options;
}

export function effortSelect(id, catalog, model, value, onChange, { disabled = false } = {}) {
  return el('select', { class: 'control control--select', id, disabled, on: { change: (event) => onChange(event.target.value) } },
    effortOptions(catalog, model).map((level) => el('option', {
      value: level,
      selected: level === (value || ''),
      text: level === '' ? 'proxy default' : level,
    })));
}

function fallbackRow(index, draft, catalog, disabled, onDraft) {
  const id = `policy-fallback-${index}`;
  return el('li', { class: 'policy__row' }, [
    el('span', { class: 'policy__index mono', text: `${index + 1}` }),
    el('select', {
      class: 'control control--select policy__select',
      id,
      'aria-label': `Fallback model ${index + 1}`,
      disabled,
      on: { change: (event) => onDraft({ kind: 'fallback', index, model: event.target.value }) },
    }, modelOptions(catalog, draft.fallback_models[index], { allowEmpty: true })),
    // Position-based ids so focus survives the full re-render after a click
    // (app.js captureFocus restores by id): focus follows the slot, not the item.
    el('div', { class: 'policy__rowactions' }, [
      el('button', { class: 'button button--quiet', type: 'button', id: `${id}-up`, text: 'Up', disabled: disabled || index === 0,
        on: { click: () => onDraft({ kind: 'move', index, delta: -1 }) } }),
      el('button', { class: 'button button--quiet', type: 'button', id: `${id}-down`, text: 'Down', disabled: disabled || index === draft.fallback_models.length - 1,
        on: { click: () => onDraft({ kind: 'move', index, delta: 1 }) } }),
      el('button', { class: 'button button--quiet', type: 'button', id: `${id}-remove`, text: 'Remove', disabled,
        on: { click: () => onDraft({ kind: 'remove', index }) } }),
    ]),
  ]);
}

function inEffect(settings, now) {
  const { policy, source, updated_at, updated_by } = settings;
  if (!policy) return el('p', { class: 'empty', text: 'The service has not reported a model policy.' });
  const provenance = source === 'dashboard'
    ? `set on this dashboard${updated_by ? ` by ${updated_by}` : ''}${updated_at ? `, ${relativeTime(updated_at, now)}` : ''}`
    : `from config/reviewer.json (${source})`;
  return el('div', { class: 'fields' }, [
    field('In effect now', describePolicy(policy), { mono: true }),
    field('Source', provenance, { title: updated_at ? formatTimestamp(updated_at) : null }),
    settings.config_policy && source === 'dashboard'
      ? field('Config default', describePolicy(settings.config_policy), { mono: true })
      : null,
  ].filter(Boolean));
}

function repositoryItem(row, index, { readOnly, saving, now, onRemove }) {
  const removing = saving === row.repo;
  return el('li', { class: 'repo' }, [
    el('div', { class: 'repo__head' }, [
      el('span', { class: 'repo__name mono', text: row.repo }),
      statusChip(row.source === 'dashboard' ? 'added' : 'config', row.source === 'dashboard' ? 'active' : 'muted'),
      // Position-based id, like the fallback rows: after a removal focus lands
      // on the next row's button instead of being lost.
      !readOnly && row.source === 'dashboard'
        ? el('button', {
          class: 'button button--quiet repo__remove',
          type: 'button',
          id: `repo-remove-${index}`,
          'aria-label': `Remove ${row.repo}`,
          disabled: saving !== null,
          'aria-busy': removing ? 'true' : null,
          text: removing ? 'Removing...' : 'Remove',
          on: { click: () => onRemove(row.repo) },
        })
        : null,
    ]),
    el('span', {
      class: 'repo__meta quiet',
      text: describeRepositorySource(row, now),
      title: row.added_at ? formatTimestamp(row.added_at) : null,
    }),
    el('span', { class: 'repo__meta quiet', text: describeRepositoryAgents(row) }),
    row.problem ? el('p', { class: 'notice', role: 'status', text: describeRepositoryProblem(row) }) : null,
  ]);
}

function repositoryRules(model) {
  return { rows: model.rows, allowedOwners: model.allowedOwners, maxAdded: model.maxAdded };
}

function repositoryForm(model, { onDraft, onAdd }) {
  const adding = model.saving === 'add';
  const form = repositoryFormState(model.draft, repositoryRules(model), { busy: model.saving !== null });
  const owner = model.allowedOwners.length > 0 ? model.allowedOwners[0] : 'owner';
  return el('form', { class: 'repo-add', on: { submit: (event) => { event.preventDefault(); onAdd(); } } }, [
    el('label', { class: 'control__label', for: 'repo-add-input', text: 'Add a repository' }),
    el('div', { class: 'repo-add__row' }, [
      // readonly, not disabled, while adding: a disabled input drops focus.
      el('input', {
        class: 'control repo-add__input mono',
        id: 'repo-add-input',
        type: 'text',
        value: model.draft,
        placeholder: `${owner}/<repository>`,
        autocomplete: 'off',
        autocapitalize: 'off',
        spellcheck: 'false',
        readonly: adding,
        'aria-describedby': 'repo-add-errors repo-add-note',
        on: { input: (event) => onDraft(event.target.value) },
      }),
      el('button', {
        class: 'button button--primary',
        type: 'submit',
        id: 'repo-add-button',
        disabled: !form.canAdd,
        'aria-busy': adding ? 'true' : null,
        text: adding ? 'Adding...' : 'Add repository',
      }),
    ]),
    el('ul', { class: 'policy__errors', id: 'repo-add-errors', role: 'alert', hidden: form.errors.length === 0 },
      form.errors.map((message) => el('li', { text: message }))),
    el('p', { class: 'prose prose--quiet', id: 'repo-add-note', text: model.note }),
  ]);
}

/** Patch the add button and the inline errors while typing, without rebuilding the input. */
export function syncRepositoryForm(form) {
  const button = document.getElementById('repo-add-button');
  if (button) button.disabled = !form.canAdd;
  const errors = document.getElementById('repo-add-errors');
  if (errors) {
    replace(errors, form.errors.map((message) => el('li', { text: message })));
    errors.hidden = form.errors.length === 0;
  }
}

function errorNotice(error, onReload) {
  return el('div', { class: 'notice notice--strong', role: 'alert' }, [
    el('strong', { text: error.title }),
    error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
    error.recovery ? el('span', { class: 'notice__detail', text: error.recovery }) : null,
    onReload ? el('button', { class: 'button', type: 'button', text: 'Reload', on: { click: onReload } }) : null,
  ]);
}

export function renderRepositorySettings({
  model,
  readOnly = false,
  now = Date.now(),
  onDraft,
  onAdd,
  onRemove,
  onReload,
}) {
  const body = [
    el('h2', { class: 'section__heading', id: 'repositories-heading', text: 'Repositories' }),
    el('p', { class: 'prose prose--quiet', text: 'The service polls these repositories for pull requests. Repositories from config/reviewer.json change through git; the admin can add more here.' }),
  ];
  if (model.error) body.push(errorNotice(model.error, onReload));
  // An add or remove the server refused: its reason, verbatim.
  if (model.actionError) body.push(errorNotice(model.actionError, null));
  if (model.configError) body.push(el('p', { class: 'notice', role: 'status', text: model.configError }));
  if (model.notice) body.push(el('div', { class: 'notice', role: 'status' }, [el('span', { text: model.notice })]));
  if (model.rows.length > 0) {
    body.push(el('ul', { class: 'repos' }, model.rows.map((row, index) => repositoryItem(row, index, {
      readOnly,
      saving: model.saving,
      now,
      onRemove,
    }))));
  } else if (model.loaded) {
    body.push(el('p', { class: 'empty', text: 'The service reported no repositories.' }));
  }
  if (model.loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading the repositories...' }));
  if (readOnly) {
    body.push(el('p', { class: 'prose prose--quiet', text: 'Read-only account: only an admin can add or remove repositories.' }));
  } else if (model.loaded) {
    body.push(repositoryForm(model, { onDraft, onAdd }));
  }
  return el('section', { class: 'panel__section', 'aria-labelledby': 'repositories-heading' }, body);
}

function adminItem(row, index, { controls, saving, now, onRemove }) {
  const removing = saving === row.email;
  return el('li', { class: 'repo' }, [
    el('div', { class: 'repo__head' }, [
      el('span', { class: 'repo__name mono', text: row.email }),
      statusChip('admin', 'active'),
      // Position-based id, like the repository rows: focus survives a removal.
      controls.showRemove
        ? el('button', {
          class: 'button button--quiet repo__remove',
          type: 'button',
          id: `admin-remove-${index}`,
          'aria-label': `Remove ${row.email}`,
          disabled: saving !== null,
          'aria-busy': removing ? 'true' : null,
          text: removing ? 'Removing...' : 'Remove',
          on: { click: () => onRemove(row.email) },
        })
        : null,
    ]),
    el('span', {
      class: 'repo__meta quiet',
      text: describeAdminRow(row, now),
      title: row.added_at ? formatTimestamp(row.added_at) : null,
    }),
  ]);
}

function adminForm(model, { onDraft, onAdd }) {
  const adding = model.saving === 'add';
  const form = adminFormState(model.draft, adminRules(model), { busy: model.saving !== null });
  return el('form', { class: 'repo-add', on: { submit: (event) => { event.preventDefault(); onAdd(); } } }, [
    el('label', { class: 'control__label', for: 'admin-add-input', text: 'Grant admin to an email address' }),
    el('div', { class: 'repo-add__row' }, [
      // readonly, not disabled, while adding: a disabled input drops focus.
      el('input', {
        class: 'control repo-add__input mono',
        id: 'admin-add-input',
        // text, not email: the browser's own email check and value rewriting
        // would disagree with the server's rule, which adminFormState mirrors.
        type: 'text',
        inputmode: 'email',
        value: model.draft,
        placeholder: 'name@example.com',
        autocomplete: 'off',
        autocapitalize: 'off',
        spellcheck: 'false',
        readonly: adding,
        'aria-describedby': 'admin-add-errors admin-add-note',
        on: { input: (event) => onDraft(event.target.value) },
      }),
      el('button', {
        class: 'button button--primary',
        type: 'submit',
        id: 'admin-add-button',
        disabled: !form.canAdd,
        'aria-busy': adding ? 'true' : null,
        text: adding ? 'Adding...' : 'Add admin',
      }),
    ]),
    el('ul', { class: 'policy__errors', id: 'admin-add-errors', role: 'alert', hidden: form.errors.length === 0 },
      form.errors.map((message) => el('li', { text: message }))),
    el('p', {
      class: 'prose prose--quiet',
      id: 'admin-add-note',
      text: 'The address must be the Google account the person signs in with, verified by Google, and '
        + 'a Gmail address or a Google Workspace account: for any other address Google does not vouch for '
        + 'who holds it now, so that sign-in is refused. They get every admin right except this list.',
    }),
  ]);
}

/** Patch the add button and the inline errors while typing, without rebuilding the input. */
export function syncAdminForm(form) {
  const button = document.getElementById('admin-add-button');
  if (button) button.disabled = !form.canAdd;
  const errors = document.getElementById('admin-add-errors');
  if (errors) {
    replace(errors, form.errors.map((message) => el('li', { text: message })));
    errors.hidden = form.errors.length === 0;
  }
}

/** Who is admin. Rendered for every role: a viewer and an added admin read it,
 *  only the owner (the server's `can_edit`) gets Remove and the add form. */
export function renderAdminSettings({
  model,
  now = Date.now(),
  onDraft,
  onAdd,
  onRemove,
  onReload,
}) {
  const controls = adminControls(model);
  const body = [
    el('h2', { class: 'section__heading', id: 'admins-heading', text: 'Admins' }),
    el('p', {
      class: 'prose prose--quiet',
      text: 'Admins retry reviews and change the model policy, the review agents and the repositories. '
        + 'The owner is always admin and is the only one who grants or revokes it. A grant applies on the '
        + "person's next request; removing one signs that person out.",
    }),
  ];
  if (model.error) body.push(errorNotice(model.error, onReload));
  // An add or remove the server refused: its reason, verbatim.
  if (model.actionError) body.push(errorNotice(model.actionError, null));
  if (model.storeError) body.push(el('p', { class: 'notice', role: 'status', text: model.storeError }));
  if (model.notice) body.push(el('div', { class: 'notice', role: 'status' }, [el('span', { text: model.notice })]));
  if (model.loaded && model.owner) {
    body.push(el('ul', { class: 'repos' }, [
      el('li', { class: 'repo' }, [
        el('div', { class: 'repo__head' }, [
          el('span', { class: 'repo__name mono', text: model.owner }),
          statusChip('owner', 'muted'),
        ]),
        el('span', { class: 'repo__meta quiet', text: OWNER_NOTE }),
      ]),
      ...model.rows.map((row, index) => adminItem(row, index, { controls, saving: model.saving, now, onRemove })),
    ]));
  }
  if (model.loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading the admins...' }));
  if (controls.note) body.push(el('p', { class: 'prose prose--quiet', text: controls.note }));
  else if (controls.showForm) body.push(adminForm(model, { onDraft, onAdd }));
  return el('section', { class: 'panel__section', 'aria-labelledby': 'admins-heading' }, body);
}

function authorRow(row, now) {
  return el('tr', { class: 'row' }, [
    cell('Author', row.login, { mono: true }),
    cell('Review language', describeLanguage(row.lang)),
    cell('Language set', row.lang && row.lang_set_at ? relativeTime(row.lang_set_at, now) : '—'),
    cell('Welcome comment', describeWelcome(row, now)),
  ]);
}

function feedbackItem(row, now) {
  const link = feedbackLink(row);
  return el('li', { class: 'repo feedback' }, [
    el('div', { class: 'repo__head' }, [
      el('span', { class: 'repo__name mono', text: describeFeedbackSource(row) }),
      row.created_at
        ? el('span', { class: 'repo__meta quiet', text: relativeTime(row.created_at, now), title: formatTimestamp(row.created_at) })
        : null,
      link ? el('a', { class: 'link link--quiet', href: link, rel: 'noreferrer noopener', text: 'Open the comment' }) : null,
    ]),
    // Author-written: text only, line breaks kept by CSS.
    el('p', { class: 'feedback__text', text: row.text }),
  ]);
}

/** PR authors' settings and feedback. No control on this section changes anything, for any account. */
export function renderAuthorSettings({ model, now = Date.now(), onReload, onMoreFeedback }) {
  const body = [
    el('h2', { class: 'section__heading', id: 'authors-heading', text: 'PR authors' }),
    el('p', { class: 'prose prose--quiet', text: READ_ONLY_NOTE }),
  ];
  if (model.error) body.push(errorNotice(model.error, onReload));
  if (model.authors.length > 0) {
    body.push(el('div', { class: 'tablewrap' }, [
      el('table', { class: 'table' }, [
        el('caption', { class: 'sr-only', text: 'PR authors with a review language or a welcome comment' }),
        el('thead', {}, [el('tr', {}, [
          headerCell('Author', { mono: true }),
          headerCell('Review language'),
          headerCell('Language set'),
          headerCell('Welcome comment'),
        ])]),
        el('tbody', {}, model.authors.map((row) => authorRow(row, now))),
      ]),
    ]));
    if (model.authorsCapped) body.push(el('p', { class: 'prose prose--quiet', text: authorsCappedNote(model.authors.length) }));
  } else if (model.loaded) {
    body.push(el('p', { class: 'empty', text: 'No author has set a language or been welcomed yet.' }));
  }
  body.push(el('h2', { class: 'section__heading', id: 'feedback-heading', text: 'Feedback' }));
  body.push(el('p', { class: 'prose prose--quiet', text: FEEDBACK_NOTE }));
  if (model.feedbackReset) body.push(el('p', { class: 'notice', role: 'status', text: FEEDBACK_RESET_NOTE }));
  if (model.feedback.length > 0) {
    body.push(el('ul', { class: 'repos', 'aria-labelledby': 'feedback-heading' }, model.feedback.map((row) => feedbackItem(row, now))));
  } else if (model.loaded) {
    body.push(el('p', { class: 'empty', text: 'No feedback yet.' }));
  }
  if (model.hasOlder && model.loaded) {
    body.push(el('button', {
      class: 'button button--quiet',
      type: 'button',
      id: 'feedback-more',
      disabled: model.loading,
      text: 'Show older feedback',
      on: { click: onMoreFeedback },
    }));
  }
  if (model.loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading PR authors and feedback...' }));
  return el('section', { class: 'panel__section', 'aria-labelledby': 'authors-heading' }, body);
}

export function renderSettings({
  settings,
  catalog,
  draft,
  loading,
  saving,
  error,
  notice,
  now = Date.now(),
  onDraft,
  onSave,
  onReset,
  onReload,
  readOnly = false,
  repositories = null,
  admins = null,
  authors = null,
}) {
  const body = [];
  const catalogReady = !!(catalog && catalog.available && catalog.models.length > 0);
  const disabled = saving || !catalogReady || !draft;

  if (error) {
    body.push(el('div', { class: 'notice notice--strong', role: 'alert' }, [
      el('strong', { text: error.title }),
      error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
      el('span', { class: 'notice__detail', text: error.recovery }),
      el('button', { class: 'button', type: 'button', text: 'Reload', on: { click: onReload } }),
    ]));
  }
  if (notice) body.push(el('div', { class: 'notice', role: 'status' }, [el('span', { text: notice })]));

  if (settings) body.push(inEffect(settings, now));

  if (readOnly) {
    body.push(el('p', { class: 'prose prose--quiet', text: 'Read-only account: the policy in effect is shown above; only an admin can change it.' }));
    return el('section', { class: 'panel', 'aria-labelledby': 'settings-heading' }, [
      el('div', { class: 'panel__head' }, [
        el('h1', { class: 'panel__title', id: 'settings-heading', text: 'Settings' }),
        el('p', { class: 'panel__intent', text: 'Which models review pull requests, at what effort, and in which fallback order.' }),
      ]),
      ...body,
      repositories ? renderRepositorySettings({ ...repositories, readOnly: true, now }) : null,
      admins ? renderAdminSettings({ ...admins, now }) : null,
      authors ? renderAuthorSettings({ ...authors, now }) : null,
    ]);
  }

  if (catalog && !catalog.available) {
    body.push(el('div', { class: 'notice notice--strong', role: 'status' }, [
      el('strong', { text: 'Model catalog unavailable' }),
      el('span', { class: 'notice__detail', text: catalog.error || 'llmux did not answer the catalog request.' }),
      el('span', { class: 'notice__detail', text: 'The policy cannot be edited until the catalog loads; the policy in effect is unchanged.' }),
      el('button', { class: 'button', type: 'button', text: 'Retry loading', on: { click: onReload } }),
    ]));
  }

  if (draft) {
    const validation = validatePolicyDraft(draft, catalog);
    const unchanged = !!(settings && settings.policy && settings.source === 'dashboard' && policyEquals(draft, settings.policy));
    body.push(el('form', { class: 'policy', on: { submit: (event) => { event.preventDefault(); if (validation.ok && !disabled && !unchanged) onSave(); } } }, [
      el('div', { class: 'filters' }, [
        el('div', { class: 'filters__group filters__group--grow' }, [
          el('label', { class: 'control__label', for: 'policy-model', text: 'Default model' }),
          el('select', {
            class: 'control control--select',
            id: 'policy-model',
            disabled,
            on: { change: (event) => onDraft({ kind: 'model', model: event.target.value }) },
          }, modelOptions(catalog, draft.model)),
        ]),
        el('div', { class: 'filters__group' }, [
          el('label', { class: 'control__label', for: 'policy-effort', text: 'Effort' }),
          effortSelect('policy-effort', catalog, draft.model, draft.effort, (effort) => onDraft({ kind: 'effort', effort }), { disabled }),
        ]),
      ]),
      el('h2', { class: 'section__heading', text: 'Fallback models, in order' }),
      el('p', { class: 'prose prose--quiet', text: 'When the default model has no usage left or keeps failing, the runner tries these in order. Transient errors are retried on each model first; a used-up usage window skips to the next model at once.' }),
      draft.fallback_models.length === 0
        ? el('p', { class: 'empty', text: 'No fallback: a failure on the default model fails the agent.' })
        : el('ol', { class: 'policy__list' }, draft.fallback_models.map((_model, index) => fallbackRow(index, draft, catalog, disabled, onDraft))),
      el('button', {
        class: 'button button--quiet',
        type: 'button',
        id: 'policy-add-fallback',
        text: 'Add fallback',
        disabled: disabled || draft.fallback_models.length >= MAX_FALLBACKS,
        on: { click: () => onDraft({ kind: 'add' }) },
      }),
      el('p', { class: 'prose prose--quiet policy__claude', text: CLAUDE_NOTE }),
      validation.errors.length > 0
        ? el('ul', { class: 'policy__errors', role: 'alert' }, validation.errors.map((message) => el('li', { text: message })))
        : null,
      el('div', { class: 'policy__actions' }, [
        el('button', {
          class: 'button button--primary',
          type: 'submit',
          id: 'policy-save',
          disabled: disabled || !validation.ok || unchanged,
          'aria-busy': saving ? 'true' : null,
          text: saving ? 'Saving...' : unchanged ? 'Saved' : 'Save policy',
        }),
        settings && settings.source === 'dashboard'
          ? el('button', { class: 'button', type: 'button', id: 'policy-reset', disabled: saving, text: 'Reset to config default', on: { click: onReset } })
          : null,
      ]),
      el('p', { class: 'prose prose--quiet', text: 'Saving applies to the next review attempt the service starts; attempts already running keep their policy. A retry can still pick a different model for that one run.' }),
    ]));
  }

  if (catalog && catalog.available) {
    body.push(el('p', { class: 'tablenote' }, [
      el('span', { text: `catalog read from llmux ${relativeTime(catalog.fetched_at, now)}`, title: formatTimestamp(catalog.fetched_at) }),
      el('span', { text: `${catalog.models.filter((model) => model.usable).length} usable models, ${catalog.models.filter((model) => !model.usable).length} not available` }),
    ]));
  }
  if (loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading the model catalog and policy...' }));

  return el('section', { class: 'panel', 'aria-labelledby': 'settings-heading' }, [
    el('div', { class: 'panel__head' }, [
      el('h1', { class: 'panel__title', id: 'settings-heading', text: 'Settings' }),
      el('p', { class: 'panel__intent', text: 'Which models review pull requests, at what effort, and in which fallback order. The choice is stored by the service and applies to every repository it reviews.' }),
    ]),
    ...body,
    repositories ? renderRepositorySettings({ ...repositories, readOnly: false, now }) : null,
    // Its own rule, not readOnly: an added admin edits the sections above, not this one.
    admins ? renderAdminSettings({ ...admins, now }) : null,
    authors ? renderAuthorSettings({ ...authors, now }) : null,
  ]);
}
