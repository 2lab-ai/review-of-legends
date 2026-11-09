/** A minimal DOM, just enough of `document`/`Element` for dom.js's `el`/
 *  `append`/`replace`/`domId` and view-agents.js's `document.getElementById`
 *  and `<details>.open` to run under node:test. No npm dependency: `Element`
 *  extends Node's own global `EventTarget`, so `addEventListener`/
 *  `dispatchEvent` (click, input, change, toggle) are the real thing, not a
 *  re-implementation.
 *
 * Deliberately NOT a general-purpose DOM: no `querySelector`, no CSS, no
 * layout. Call `createDomShim()` once per test (or per `describe`) and set
 * `globalThis.document = shim.document` before rendering — each call is a
 * fresh document with its own `getElementById` registry, so tests cannot
 * leak ids into one another.
 */

class ShimNode extends EventTarget {
  constructor(nodeType) {
    super();
    this.nodeType = nodeType;
    this.parentNode = null;
    this.childNodes = [];
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index !== -1) this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }
}

class ShimText extends ShimNode {
  constructor(text) {
    super(3);
    this.data = String(text);
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class ShimElement extends ShimNode {
  constructor(tagName, registry) {
    super(1);
    this.tagName = String(tagName).toUpperCase();
    this._registry = registry;
    this.attributes = new Map();
    this.dataset = {};
    this._open = false;
  }

  // Boolean DOM properties reflect their attributes; syncAgentEditor patches
  // .disabled directly after input, while the initial render sets an attribute.
  get disabled() {
    return this.hasAttribute('disabled');
  }

  set disabled(value) {
    if (value) this.setAttribute('disabled', '');
    else this.removeAttribute('disabled');
  }

  get id() {
    return this.attributes.get('id') || '';
  }

  /** `<details>.open`: setting it (from a click handler, or from a script like
   *  `openPrompt`) queues a `toggle` notification exactly as a real browser
   *  does for ANY change to the open state, scripted or not (the HTML Standard's
   *  details-notification-task-steps) — dispatched synchronously here so a
   *  test does not need a microtask flush. A no-op set (already at that
   *  value) dispatches nothing, matching the same rule. */
  get open() {
    return this._open;
  }

  set open(value) {
    const next = !!value;
    if (next === this._open) return;
    this._open = next;
    this.dispatchEvent(new Event('toggle'));
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this.childNodes = [];
    const text = value === null || value === undefined ? '' : String(value);
    if (text !== '') this.appendChild(new ShimText(text));
  }

  setAttribute(name, value) {
    const stringValue = value === true ? '' : String(value);
    this.attributes.set(name, stringValue);
    if (name === 'id' && this._registry) this._registry.set(stringValue, this);
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }
}

/** A fresh, isolated document: its own `getElementById` registry, so one
 *  test's ids cannot collide with another's. */
export function createDomShim() {
  const registry = new Map();
  const document = {
    createElement(tag) {
      return new ShimElement(tag, registry);
    },
    createElementNS(_namespace, tag) {
      return new ShimElement(tag, registry);
    },
    createTextNode(text) {
      return new ShimText(text);
    },
    getElementById(id) {
      return registry.get(id) || null;
    },
  };
  return { document };
}

/** Every element (tags: button, textarea, li, ...) under `root`, depth-first,
 *  root included — the walk the assertions below need; not a CSS engine. */
export function* walkElements(root) {
  if (!root || root.nodeType !== 1) return;
  yield root;
  for (const child of root.childNodes) yield* walkElements(child);
}

/** Every element under `root` whose tag matches (case-insensitive). */
export function findAll(root, tagName) {
  const want = String(tagName).toUpperCase();
  return [...walkElements(root)].filter((node) => node.tagName === want);
}

/** The one element under `root` with this id, the way `getElementById` finds
 *  it in a real document, without needing the shared registry (a tree built
 *  but never attached to `document.body` is not in it). */
export function findById(root, id) {
  return [...walkElements(root)].find((node) => node.id === id) || null;
}
