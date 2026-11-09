/** Minimal DOM builder.
 *
 * There is deliberately no way to pass markup: text always goes through
 * textContent and attributes are set with setAttribute. Server strings (PR
 * titles, agent errors, llmux account names) therefore cannot become HTML, which
 * is the client half of the "no raw HTML injection" rule.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Batches renders onto one animation frame.
 *
 *  A reconnect replay or a busy poll cycle delivers events far faster than a human
 *  reads, and rendering per event rebuilt the whole <main> dozens of times in a
 *  burst. Collapsing to one frame keeps the DOM work bounded; because the render
 *  pass itself captures and restores focus, a burst can no longer pull the caret
 *  out of the search box either. */
export function createRenderScheduler(render, { requestFrame, cancelFrame } = {}) {
  const request = requestFrame
    || ((fn) => (typeof globalThis.requestAnimationFrame === 'function'
      ? globalThis.requestAnimationFrame(fn)
      : setTimeout(fn, 16)));
  const cancel = cancelFrame
    || ((handle) => (typeof globalThis.cancelAnimationFrame === 'function'
      ? globalThis.cancelAnimationFrame(handle)
      : clearTimeout(handle)));
  let handle = null;

  function cancelPending() {
    if (handle !== null) {
      cancel(handle);
      handle = null;
    }
  }

  return {
    schedule() {
      if (handle !== null) return;
      handle = request(() => {
        handle = null;
        render();
      });
    },
    /** For state the operator just caused (a click), where a frame of latency
     *  would read as an unresponsive control. */
    now() {
      cancelPending();
      render();
    },
    cancel: cancelPending,
  };
}

/** Defers work until the pointer gesture in progress (if any) is over.
 *
 *  Re-rendering on blur is safe for keyboard focus moves, but a mouse or touch
 *  press blurs the focused field on pointerdown: replacing the pressed button
 *  before pointerup would swallow its click. `whenIdle(fn)` runs `fn` at once
 *  when no pointer is down, otherwise after the click that follows the release
 *  (or `settleMs` after it when no click comes, or on pointercancel). */
export function createGestureGate(target, { settleMs = 300, setTimer, clearTimer } = {}) {
  const startTimer = setTimer || ((fn, ms) => setTimeout(fn, ms));
  const stopTimer = clearTimer || ((handle) => clearTimeout(handle));
  let pressed = false;
  target.addEventListener('pointerdown', () => { pressed = true; }, true);
  for (const type of ['pointerup', 'pointercancel']) {
    target.addEventListener(type, () => { pressed = false; }, true);
  }
  return {
    whenIdle(fn) {
      if (!pressed) {
        fn();
        return;
      }
      let done = false;
      let timer = null;
      const settle = () => {
        timer = startTimer(finish, settleMs);
      };
      function finish() {
        if (done) return;
        done = true;
        if (timer !== null) stopTimer(timer);
        target.removeEventListener('pointerup', settle, true);
        target.removeEventListener('pointercancel', finish, true);
        target.removeEventListener('click', finish, true);
        fn();
      }
      target.addEventListener('pointerup', settle, true);
      target.addEventListener('pointercancel', finish, true);
      target.addEventListener('click', finish, true);
    },
  };
}

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'class') {
      node.className = String(value);
    } else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) {
        if (dataValue === null || dataValue === undefined) continue;
        node.dataset[dataKey] = String(dataValue);
      }
    } else if (key === 'on') {
      for (const [type, handler] of Object.entries(value)) {
        if (typeof handler === 'function') node.addEventListener(type, handler);
      }
    } else if (key === 'value') {
      node.value = String(value);
    } else if (key === 'disabled' || key === 'checked' || key === 'selected') {
      node[key] = Boolean(value);
      if (value === true) node.setAttribute(key, '');
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' || typeof child === 'number'
      ? document.createTextNode(String(child))
      : child);
  }
  return node;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function replace(node, children) {
  clear(node);
  return append(node, children);
}

/** A DOM id from arbitrary parts (run ids, agent ids): anything outside
 *  [A-Za-z0-9_-] becomes "-", so the id is also safe inside a selector. */
export function domId(...parts) {
  return parts.map((part) => String(part).replace(/[^A-Za-z0-9_-]/g, '-')).join('-');
}

/** Ids of the open `<details id>` under `root`. A render rebuilds every node, so
 *  a disclosure the operator opened would otherwise snap shut on each event. */
export function captureOpenDetails(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  return Array.from(root.querySelectorAll('details[open][id]'), (node) => node.id).filter(Boolean);
}

/** Re-open what `captureOpenDetails` saw; ids the new render lacks are ignored. */
export function restoreOpenDetails(root, ids) {
  if (!root || !Array.isArray(ids) || ids.length === 0 || typeof root.querySelectorAll !== 'function') return;
  const open = new Set(ids);
  for (const node of root.querySelectorAll('details[id]')) {
    if (open.has(node.id)) node.open = true;
  }
}

/** Inline square glyph. Squares (not dots) keep the sharp-cornered, monochrome
 *  language of the reference, and every glyph is paired with a text label. */
export function glyph(kind) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `glyph glyph--${kind}`);
  svg.setAttribute('viewBox', '0 0 10 10');
  svg.setAttribute('width', '10');
  svg.setAttribute('height', '10');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const rect = document.createElementNS(SVG_NS, 'rect');
  rect.setAttribute('x', '1');
  rect.setAttribute('y', '1');
  rect.setAttribute('width', '8');
  rect.setAttribute('height', '8');
  svg.appendChild(rect);
  return svg;
}

/** Label + value pair used across the run header and account cards. */
export function field(label, value, { mono = false, title = null } = {}) {
  return el('div', { class: 'field' }, [
    el('span', { class: 'field__label', text: label }),
    el('span', {
      class: mono ? 'field__value field__value--mono' : 'field__value',
      text: value,
      title,
    }),
  ]);
}

export function statusChip(label, tone, { live = false } = {}) {
  return el('span', { class: `chip chip--${tone}`, dataset: { tone } }, [
    live ? glyph('live') : null,
    el('span', { class: 'chip__text', text: label }),
  ]);
}

export function srOnly(text) {
  return el('span', { class: 'sr-only', text });
}

export function cell(label, children, { mono = false, wide = false } = {}) {
  const classes = ['cell'];
  if (mono) classes.push('cell--mono');
  if (wide) classes.push('cell--wide');
  return el('td', { class: classes.join(' '), dataset: { label } }, children);
}

export function headerCell(label, { mono = false } = {}) {
  return el('th', { scope: 'col', class: mono ? 'cell--mono' : null, text: label });
}
