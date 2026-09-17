'use strict';

// Run the production dock, menu, and composer functions against synthetic DOM
// and time. No journal data, browser profile, provider, or network is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(
  process.argv[2] || path.join(__dirname, '../src/agent_trace_studio/assets/dashboard.js'), 'utf8',
);
function functionSource(name) {
  const start = source.indexOf('\n  function ' + name + '(');
  const end = source.indexOf('\n  function ', start + 1);
  assert.notEqual(start, -1, 'Dashboard function must exist: ' + name);
  return source.slice(start, end < 0 ? source.length : end);
}

function fixture() {
  let now = 0;
  let timerId = 0;
  const timers = new Map();
  const frames = [];
  const sent = [];
  let saves = 0;
  const document = { activeElement: null, keyboardMode: false };
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.parentNode = null;
      this.listeners = {};
      this.attributes = {};
      this.style = {};
      this.dataset = {};
      this.hidden = false;
      this.disabled = false;
      this.value = '';
      const classes = new Set();
      this.classList = {
        contains: (key) => classes.has(key),
        add: (key) => classes.add(key),
        remove: (key) => classes.delete(key),
        toggle: (key, value) => value ? classes.add(key) : classes.delete(key),
      };
    }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    append(...children) { children.forEach((child) => this.appendChild(child)); }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
    get firstChild() { return this.children[0]; }
    after(child) {
      const parent = this.parentNode;
      parent.children.splice(parent.children.indexOf(this) + 1, 0, child);
      child.parentNode = parent;
    }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    contains(target) { return this === target || this.children.some((child) => child.contains(target)); }
    querySelectorAll(selector) {
      const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll('*')]);
      if (selector === '*') return descendants;
      return descendants.filter((child) => child.tagName === 'BUTTON'
        && (selector !== 'button:not(:disabled)' || !child.disabled));
    }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    emit(type, options = {}) {
      const event = {
        type, target: this, button: 0, pointerId: 1, pointerType: 'mouse', clientX: 0, clientY: 0,
        defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...options,
      };
      let target = this;
      do {
        (target.listeners[type] || []).forEach((callback) => callback(event));
        target = ['keydown', 'focusin', 'focusout', 'pointerdown', 'click'].includes(type) ? target.parentNode : null;
      } while (target);
      return event;
    }
    focus() {
      if (this.disabled || document.activeElement === this) return;
      const previous = document.activeElement;
      document.activeElement = this;
      previous?.emit('focusout', { relatedTarget: this });
      this.emit('focus');
      this.emit('focusin');
    }
    click() { if (!this.disabled) this.emit('click'); }
    matches(selector) { return selector === ':focus-visible' && document.keyboardMode && document.activeElement === this; }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    setPointerCapture(id) { this.capturedPointer = id; }
    getBoundingClientRect() {
      const isButton = this.id === 'qa-floating-button';
      const isMenu = this.id === 'qa-prompt-menu';
      const width = isButton ? 54 : isMenu ? Math.min(304, window.innerWidth - 24) : 460;
      const height = isButton ? 54 : isMenu ? Math.min(300, Number.parseFloat(this.style.maxHeight) || 300) : 600;
      const left = Number.parseFloat(this.style.left) || (isButton ? window.innerWidth - 76 : 0);
      const top = Number.parseFloat(this.style.top) || (isButton ? window.innerHeight - 76 : 0);
      return { left, top, width, height, right: left + width, bottom: top + height };
    }
  }
  const root = new Node('document');
  const body = new Node('body');
  root.append(body);
  document.activeElement = body;
  Object.assign(document, {
    createElement: (tag) => new Node(tag),
    createElementNS: (_namespace, tag) => new Node(tag),
    getElementById: (id) => root.querySelectorAll('*').find((node) => node.id === id),
    addEventListener: (...args) => root.addEventListener(...args),
  });
  const window = {
    innerWidth: 1280, innerHeight: 900,
    setTimeout: (callback, delay) => { timers.set(++timerId, { callback, due: now + delay }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
    requestAnimationFrame: (callback) => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: () => {},
    addEventListener: (...args) => root.addEventListener(...args),
  };
  for (const id of ['qa-floating-button', 'qa-conversation-popover', 'qa-conversation-toggle', 'qa-unread-count', 'qa-question', 'qa-messages']) {
    const node = new Node(id.includes('button') ? 'button' : 'div');
    node.id = id;
    body.append(node);
  }
  const state = {
    qaConversationOpen: false, qaConversationCollapsed: true, qaUnreadCount: 0, qaBusy: false,
    qaFloatingButtonPosition: null, selectedEvent: null, hasTrace: true,
  };
  const runtime = { interactive: true, qa: { configured: true }, agent: {}, audit_rules: {} };
  const context = vm.createContext({
    document, window, state, runtime, qaPromptMenuController: null,
    qaFloatingViewportMargin: 12, qaConversationGap: 12,
    selectedTrace: () => state.hasTrace ? { session_id: 'synthetic', events: [] } : null,
    selectedTraceEvent: () => state.selectedEvent,
    renderCollapseToggle: (node, collapsed) => node.setAttribute('aria-expanded', String(!collapsed)),
    saveQAFloatingButtonPosition: () => { saves += 1; },
    persistQAConversationCollapsed: () => {},
    positionQAConversationNearButton: () => {},
    sendAgentMessage: (message) => sent.push(message),
    fetch: () => { throw new Error('Prompt menu must not request network access'); },
  });
  vm.runInContext([
    'element', 'clear', 'bounded', 'qaPromptIcon', 'qaFloatingSuggestedPrompts', 'prefillQAPrompt',
    'setupQAPromptMenu', 'qaSuggestedPrompts', 'useQASuggestion', 'renderQAConversationChrome',
    'setupQAFloatingDock', 'applyQAFloatingButtonPosition', 'constrainedQAFloatingButtonPosition',
    'openQAConversation', 'closeQAConversation', 'toggleQAConversation', 'scrollQAConversationToBottom',
  ].map(functionSource).join('\n'), context);
  context.renderQA = () => context.renderQAConversationChrome();
  context.setupQAFloatingDock();
  context.renderQAConversationChrome();
  const get = (id) => document.getElementById(id);
  const button = get('qa-floating-button');
  const menu = get('qa-prompt-menu');
  const tick = (milliseconds) => {
    now += milliseconds;
    for (const [id, timer] of [...timers]) if (timer.due <= now) { timers.delete(id); timer.callback(); }
    while (frames.length) frames.shift()();
  };
  return {
    context, document, window, state, runtime, body, root, button, menu, sent, get, tick,
    items: () => menu.querySelectorAll('button'),
    hover: () => { document.keyboardMode = false; button.emit('pointerenter'); },
    key: (node, key) => { document.keyboardMode = true; return node.emit('keydown', { key }); },
    saves: () => saves,
  };
}

const hover = fixture();
assert(hover.menu.hidden);
hover.get('qa-question').value = 'Keep my draft';
hover.hover();
assert(!hover.menu.hidden);
assert.equal(hover.items().length, 4);
assert.equal(hover.items()[0].children[1].textContent, 'Latest update');
assert.equal(hover.button.attributes['aria-expanded'], 'false', 'Hover must not claim the conversation is open.');
assert.equal(hover.get('qa-question').value, 'Keep my draft');
hover.button.emit('pointerleave');
hover.tick(100);
hover.menu.emit('pointerenter');
hover.tick(500);
assert(!hover.menu.hidden, 'The menu stays open when crossing the icon-to-menu gap.');
hover.menu.emit('pointerleave');
hover.tick(219);
assert(!hover.menu.hidden);
hover.tick(1);
assert(hover.menu.hidden);
hover.hover();
assert.equal(hover.items().length, 4, 'Reopening does not duplicate prompts.');
hover.body.emit('pointerdown');
assert(hover.menu.hidden, 'Outside clicks dismiss the menu.');
assert.deepEqual(hover.sent, []);

const keyboard = fixture();
keyboard.document.keyboardMode = true;
keyboard.button.focus();
assert(!keyboard.menu.hidden, 'Keyboard focus exposes suggested prompts.');
assert(keyboard.key(keyboard.button, 'ArrowDown').defaultPrevented);
assert.equal(keyboard.document.activeElement, keyboard.items()[0]);
keyboard.key(keyboard.items()[0], 'ArrowUp');
assert.equal(keyboard.document.activeElement, keyboard.items()[3]);
keyboard.key(keyboard.items()[3], 'Home');
assert.equal(keyboard.document.activeElement, keyboard.items()[0]);
keyboard.key(keyboard.items()[0], 'End');
assert.equal(keyboard.document.activeElement, keyboard.items()[3]);
keyboard.key(keyboard.items()[3], 'Escape');
keyboard.tick(300);
assert(keyboard.menu.hidden, 'Escape must not reopen the menu while restoring icon focus.');
assert.equal(keyboard.document.activeElement, keyboard.button);
keyboard.key(keyboard.button, 'ArrowUp');
assert.equal(keyboard.document.activeElement, keyboard.items()[3]);
keyboard.body.focus();
keyboard.tick(300);
assert(keyboard.menu.hidden, 'Tabbing away dismisses the menu.');

const choice = fixture();
choice.hover();
choice.items()[0].click();
choice.tick(0);
assert(choice.state.qaConversationOpen);
assert(!choice.state.qaConversationCollapsed);
assert(choice.menu.hidden);
assert.equal(choice.get('qa-question').value, 'What is the latest item in this session?');
assert.equal(choice.document.activeElement, choice.get('qa-question'));
assert.equal(choice.get('qa-question').selectionStart, choice.get('qa-question').value.length);
assert.deepEqual(choice.sent, [], 'Choosing a floating prompt only prepares a draft, never sends it.');
choice.hover();
assert(choice.menu.hidden, 'Suggestions do not cover an open conversation.');
choice.document.keyboardMode = true;
choice.context.closeQAConversation();
assert(choice.menu.hidden, 'Closing the conversation restores focus without reopening suggestions.');
choice.button.click();
assert(choice.state.qaConversationOpen, 'A normal icon click still opens the conversation.');
assert.equal(choice.get('qa-question').value, 'What is the latest item in this session?');
choice.context.useQASuggestion({ prompt: 'Existing in-console behavior' });
assert.deepEqual(choice.sent, ['Existing in-console behavior']);

for (const unavailable of ['offline', 'unconfigured', 'empty', 'busy']) {
  const test = fixture();
  if (unavailable === 'offline') test.runtime.interactive = false;
  if (unavailable === 'unconfigured') test.runtime.qa.configured = false;
  if (unavailable === 'empty') test.state.hasTrace = false;
  if (unavailable === 'busy') test.state.qaBusy = true;
  test.hover();
  assert(test.items().every((item) => item.disabled), unavailable + ' prompts must be disabled.');
  test.items()[0].click();
  test.context.prefillQAPrompt({ prompt: 'Must not replace the draft' });
  assert(!test.state.qaConversationOpen);
  assert.equal(test.get('qa-question').value, '');
  assert.deepEqual(test.sent, []);
  test.key(test.button, 'ArrowDown');
  assert.equal(test.document.activeElement, test.menu, 'Unavailable status remains keyboard-accessible.');
}

const updates = fixture();
updates.hover();
const stableItem = updates.items()[0];
updates.state.qaBusy = true;
updates.context.renderQAConversationChrome();
assert(stableItem.disabled);
updates.state.qaBusy = false;
updates.context.renderQAConversationChrome();
assert(!stableItem.disabled);
assert.equal(updates.items()[0], stableItem, 'Polling does not replace focused prompt buttons.');
for (const [field, label] of [
  ['selectedEvent', 'Explain selected event'],
  ['traceCheckpoint', 'Review selected checkpoint'],
  ['assuranceContract', 'Review selected audit finding'],
]) {
  updates.state[field] = {};
  updates.body.emit('pointerdown');
  updates.hover();
  assert.equal(updates.items()[1].children[1].textContent, label);
}

const drag = fixture();
drag.hover();
drag.button.emit('pointerdown', { clientX: 1200, clientY: 820 });
assert(drag.menu.hidden);
drag.button.emit('pointermove', { clientX: 300, clientY: 40 });
assert(drag.button.classList.contains('is-dragging'));
drag.hover();
assert(drag.menu.hidden, 'Dragging does not reopen the suggestions.');
drag.button.emit('pointerup');
drag.button.click();
assert(!drag.state.qaConversationOpen, 'A drag must not become a conversation click.');
assert.equal(drag.saves(), 1);
drag.tick(101);
drag.hover();
assert(!drag.menu.hidden);
assert.equal(drag.menu.dataset.anchor, 'below');
drag.button.click();
assert(drag.state.qaConversationOpen);

const touch = fixture();
touch.button.emit('pointerenter', { pointerType: 'touch' });
assert(touch.menu.hidden);
touch.button.click();
assert(touch.state.qaConversationOpen, 'Touch activation retains the existing chat behavior.');

for (const [width, height, left, top, anchor] of [
  [1280, 900, 1200, 820, 'above'], [1280, 900, 12, 12, 'below'],
  [320, 600, 254, 500, 'above'], [320, 360, 12, 12, 'below'],
  [800, 240, 374, 93, 'above'], [800, 240, 374, 40, 'below'],
]) {
  const test = fixture();
  test.window.innerWidth = width;
  test.window.innerHeight = height;
  test.state.qaFloatingButtonPosition = { left, top };
  test.context.applyQAFloatingButtonPosition();
  test.hover();
  const bounds = test.menu.getBoundingClientRect();
  const icon = test.button.getBoundingClientRect();
  assert.equal(test.menu.dataset.anchor, anchor);
  assert(bounds.left >= 12 && bounds.right <= width - 12);
  assert(bounds.top >= 12 && bounds.bottom <= height - 12);
  assert(bounds.bottom <= icon.top - 8 || bounds.top >= icon.bottom + 8, 'Prompts never obscure the icon.');
  test.window.innerWidth = 600;
  test.window.innerHeight = 400;
  test.root.emit('resize');
  test.tick(0);
  assert(test.menu.getBoundingClientRect().right <= 588, 'Resize repositions an open prompt menu.');
}

console.log('Prompt menu hover, keyboard, draft safety, availability, drag, touch, and viewport checks passed.');
