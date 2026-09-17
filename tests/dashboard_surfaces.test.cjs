'use strict';

// Synthetic DOM/API checks for the real surface setup, controls, source loading,
// run details, and chat scroll behavior. This does not claim browser layout QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/agent_trace_studio/assets/dashboard.js'), 'utf8');
function functionSource(name) {
  const start = source.search(new RegExp('\\n  (?:async )?function ' + name + '\\('));
  assert.notEqual(start, -1, name + ' must exist');
  const remaining = source.slice(start + 1);
  const end = remaining.search(/\n  (?:async )?function /);
  return remaining.slice(0, end < 0 ? remaining.length : end);
}

function fixture({ legacy = false, chatSettings = false, interactive = true } = {}) {
  const calls = [];
  const actions = [];
  let responses = [];
  let applied = 0;
  let modalShows = 0;
  const document = { activeElement: null };
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag;
      this.children = [];
      this.parentNode = null;
      this.className = '';
      this.listeners = {};
      this.attributes = {};
      this.dataset = {};
      this.style = {};
      this.hidden = false;
      this.disabled = false;
      this.open = false;
      this.value = '';
      this._text = '';
      this.scrollHeight = 600;
      this.clientHeight = 300;
      this.scrollTop = 0;
      this.classList = {
        contains: (name) => this.className.split(/\s+/).includes(name),
        toggle: (name, enabled) => {
          const names = new Set(this.className.split(/\s+/).filter(Boolean));
          if (enabled) names.add(name); else names.delete(name);
          this.className = [...names].join(' ');
        },
        add: (name) => this.classList.toggle(name, true),
        remove: (name) => this.classList.toggle(name, false),
      };
    }
    get firstChild() { return this.children[0]; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parentNode = null; }); this.children = []; }
    set innerHTML(_value) { throw new Error('UI content must not be inserted as HTML'); }
    append(...nodes) { for (const node of nodes) { node.remove(); node.parentNode = this; this.children.push(node); } }
    appendChild(node) { this.append(node); return node; }
    prepend(...nodes) { for (const node of [...nodes].reverse()) { node.remove(); node.parentNode = this; this.children.unshift(node); } }
    removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; }
    remove() { this.parentNode?.removeChild(this); }
    before(...nodes) {
      for (const node of nodes) {
        node.remove();
        const parent = this.parentNode;
        parent.children.splice(parent.children.indexOf(this), 0, node);
        node.parentNode = parent;
      }
    }
    after(node) {
      node.remove();
      this.parentNode.children.splice(this.parentNode.children.indexOf(this) + 1, 0, node);
      node.parentNode = this.parentNode;
    }
    contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
    matches(selector) {
      if (selector[0] === '#') return this.id === selector.slice(1);
      if (selector[0] === '.') return this.classList.contains(selector.slice(1));
      return selector === '*' || this.tagName === selector;
    }
    querySelectorAll(selector) {
      const [first, ...rest] = selector.split(' ');
      const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll('*')]);
      const matches = descendants.filter((child) => child.matches(first));
      return rest.length ? matches.flatMap((child) => child.querySelectorAll(rest.join(' '))) : matches;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    emit(type, options = {}) {
      const event = { type, target: this, clientX: 50, clientY: 50, preventDefault() { this.defaultPrevented = true; }, ...options };
      let node = this;
      do {
        (node.listeners[type] || []).forEach((callback) => callback(event));
        node = ['click', 'keydown', 'pointerdown', 'submit'].includes(type) ? node.parentNode : null;
      } while (node);
      return event;
    }
    click() { if (!this.disabled) this.emit('click'); }
    focus() { if (!this.disabled) document.activeElement = this; }
    showModal() { this.open = true; modalShows += 1; }
    close() { if (this.open) { this.open = false; this.emit('close'); } }
    getBoundingClientRect() { return { left: 20, top: 20, right: 620, bottom: 600 }; }
  }
  const root = new Element('document');
  const body = new Element('body');
  root.append(body);
  document.body = body;
  document.activeElement = body;
  Object.assign(document, {
    createElement: (tag) => new Element(tag),
    getElementById: (id) => body.querySelector('#' + id),
    querySelector: (selector) => body.querySelector(selector),
    addEventListener: (...args) => root.addEventListener(...args),
  });
  const add = (parent, tag, id, className = '') => {
    const node = new Element(tag);
    node.id = id;
    node.className = className;
    parent.append(node);
    return node;
  };
  const main = add(body, 'main', 'floating-workspace');
  const agentPanel = add(main, 'section', 'agent-selection-panel');
  const agentSettings = legacy || chatSettings ? agentPanel : add(agentPanel, 'div', 'agent-selection-settings');
  add(agentSettings, 'span', 'agent-actions-status');
  const workspace = add(main, 'section', 'source-workspace');
  const sourceHeading = add(workspace, 'div', '', 'workspace-heading');
  const grid = add(workspace, 'div', '', 'source-workspace-grid');
  add(grid, 'span', 'source-status');
  const conversation = add(body, 'section', 'qa-conversation-popover');
  const header = add(conversation, 'header', '', 'qa-conversation-header');
  const headerActions = add(header, 'div', '', 'qa-conversation-header-actions');
  add(headerActions, 'button', 'qa-conversation-toggle');
  const messages = add(conversation, 'div', 'qa-messages');
  add(conversation, 'form', 'qa-form');
  let controlOwner;
  let sourceOwner;
  if (legacy) {
    controlOwner = add(main, 'section', 'studio-panel');
    add(controlOwner, 'button', 'qa-configure-button');
    sourceOwner = grid;
  } else {
    add(chatSettings ? headerActions : agentSettings, 'button', 'qa-configure-button');
    const context = add(conversation, 'details', 'qa-context-details');
    add(context, 'span', 'qa-context-summary');
    const chatBody = add(conversation, 'div', 'qa-conversation-body');
    chatBody.append(messages);
    const runDetails = add(chatBody, 'details', 'qa-run-details');
    add(runDetails, 'span', 'qa-run-summary');
    controlOwner = context;
    sourceOwner = add(body, 'dialog', 'source-dialog');
    add(sourceHeading, 'button', 'add-source-button');
  }
  const contextNodes = add(controlOwner, 'section', '', 'agent-context');
  add(contextNodes, 'strong', 'agent-context-source');
  add(controlOwner, 'div', 'agent-capability-list', 'agent-capability-row');
  const runOwner = legacy ? controlOwner : document.getElementById('qa-run-details');
  add(runOwner, 'div', 'agent-activity');
  const runPanel = add(runOwner, 'section', 'agent-run-panel');
  const recovery = add(runPanel, 'section', 'agent-run-recovery');
  recovery.hidden = true;
  for (const name of ['activate', 'continue', 'restart', 'discard']) add(recovery, 'button', 'agent-run-' + name);
  const loader = add(sourceOwner, 'section', '', 'source-loader');
  const heading = add(loader, 'div', '', 'source-panel-heading');
  add(heading, legacy ? 'h3' : 'h2', legacy ? '' : 'source-dialog-title');
  add(loader, 'p', 'source-loader-status');
  if (!legacy) {
    add(heading, 'button', 'source-dialog-close');
    add(loader, 'p', 'source-dialog-status');
  }
  const session = add(loader, 'form', 'session-id-form');
  add(session, 'input', 'session-id');
  add(session, 'button', 'load-session-id-button');
  const pathForm = add(loader, 'form', 'path-form');
  add(pathForm, 'input', 'session-path');
  add(pathForm, 'button', 'load-path-button');
  add(loader, 'button', 'upload-button');
  add(loader, 'input', 'session-upload');
  const savedControls = Object.fromEntries(['qa-configure-button', 'agent-context-source', 'agent-run-activate', 'session-id', 'session-path', 'upload-button'].map((id) => [id, document.getElementById(id)]));
  const state = { qaMessages: [], qaBusy: false, qaStopBusy: false, qaConversationOpen: false, sourceBusy: false, agentRun: null };
  const runtime = { interactive, qa: { configured: interactive }, agent: {} };
  const context = vm.createContext({
    document, state, runtime, data: { traces: [{ session_id: 'synthetic-existing' }] },
    window: { addEventListener: (...args) => root.addEventListener(...args), setTimeout: () => 1, clearTimeout: () => {}, requestAnimationFrame: (callback) => callback() },
    formatInteger: String, loadedTraceKeys: () => new Set(['synthetic-existing']),
    terminalAgentStatus: (status) => ['completed', 'failed'].includes(status),
    agentStatusLabel: (status) => status,
    openQAConfig: () => actions.push('settings'),
    actOnAgentRun: (action) => actions.push(action),
    closeQAConversation: () => { actions.push('close-chat'); state.qaConversationOpen = false; },
    applyPayload: (payload) => { applied += 1; context.data = payload; },
    apiJson: async (url, options) => {
      calls.push({ url, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response || { traces: [{ session_id: 'synthetic-added' }] };
    },
  });
  for (const match of source.matchAll(/\n  (?:async )?function (\w+)\(/g)) {
    if (!(match[1] in context)) context[match[1]] = () => {};
  }
  vm.runInContext([
    'element', 'clear', 'setupStudioSurfaces', 'openSourceDialog', 'closeSourceDialog',
    'setSourceStatus', 'setSourceBusy', 'loadPath', 'loadSessionId', 'uploadFiles',
    'renderConversationRunDetails', 'scrollQAConversationToBottom', 'renderQA', 'setupControls',
  ].map(functionSource).join('\n'), context);
  context.setupStudioSurfaces();
  // Other existing controls are inert nodes so the production setupControls can
  // register all listeners; only the migrated controls under test have owners.
  for (const match of source.matchAll(/getElementById\('([^']+)'\)/g)) {
    if (!document.getElementById(match[1]) && !match[1].startsWith('studio-panel')) add(body, 'div', match[1]);
  }
  document.getElementById('studio-context-menu').hidden = true;
  context.setupControls();
  context.setSourceBusy(false);
  const get = (id) => document.getElementById(id);
  const escape = () => {
    root.emit('keydown', { key: 'Escape' });
    if (get('source-dialog').open) get('source-dialog').close(); // Native dialog default action.
  };
  return {
    context, document, state, runtime, root, body, get, calls, actions, savedControls, escape,
    responses: (values) => { responses = values; }, applied: () => applied, modalShows: () => modalShows,
  };
}

(async () => {
  for (const layout of [{}, { chatSettings: true }, { legacy: true }]) {
    const test = fixture(layout);
    assert.equal(test.get('studio-panel'), null);
    for (const [id, node] of Object.entries(test.savedControls)) assert.equal(test.get(id), node, 'Controls must move, not be duplicated: ' + id);
    assert(test.get('agent-selection-panel').contains(test.get('qa-configure-button')));
    assert(!test.get('qa-conversation-popover').contains(test.get('qa-configure-button')));
    assert(test.get('qa-context-details').contains(test.get('agent-context-source')));
    assert(test.get('qa-run-details').contains(test.get('agent-run-activate')));
    assert(test.get('source-dialog').contains(test.get('session-id-form')));
    assert(!test.get('source-workspace').contains(test.get('session-path')));
    assert(test.get('qa-conversation-body').contains(test.get('qa-messages')));
    test.context.setupStudioSurfaces();
    assert.equal(test.get('add-source-button').listeners.click.length, 1, 'Surface setup is idempotent.');
    test.get('qa-configure-button').click();
    for (const name of ['activate', 'continue', 'restart', 'discard']) test.get('agent-run-' + name).click();
    assert.deepEqual(test.actions, ['settings', 'activate', 'continue', 'restart', 'discard']);
    assert.equal(test.calls.length, 0, 'Moving controls must not trigger any workflow or network request.');

    test.get('add-source-button').click();
    assert(test.get('source-dialog').open);
    assert.equal(test.document.activeElement, test.get('session-id'));
    test.context.openSourceDialog();
    assert.equal(test.modalShows(), 1);
    test.get('session-path').value = '/synthetic/draft.jsonl';
    test.get('source-dialog').emit('click', { clientX: 25, clientY: 25 });
    assert(test.get('source-dialog').open, 'Inside padding is not the backdrop.');
    test.state.qaConversationOpen = true;
    test.escape();
    assert(!test.get('source-dialog').open);
    assert(test.state.qaConversationOpen, 'Escape dismisses the source modal without closing the chat underneath.');
    assert.equal(test.document.activeElement, test.get('add-source-button'));
    assert.equal(test.get('session-path').value, '/synthetic/draft.jsonl');
    test.context.openSourceDialog();
    test.get('source-dialog').emit('click', { clientX: 0, clientY: 0 });
    assert(!test.get('source-dialog').open, 'Backdrop click dismisses the popup.');
    test.context.openSourceDialog();
    test.get('source-dialog-close').click();
    assert(!test.get('source-dialog').open);
  }

  const load = fixture();
  load.context.openSourceDialog();
  load.get('session-id').value = 'synthetic-requested';
  await load.context.loadSessionId('synthetic-requested');
  assert.equal(load.calls[0].url, '/api/session/id');
  assert.equal(JSON.parse(load.calls[0].options.body).session_id, 'synthetic-requested');
  assert(!load.get('source-dialog').open);
  assert.equal(load.get('session-id').value, '');
  assert.equal(load.document.activeElement, load.get('add-source-button'));
  assert.equal(load.applied(), 1);
  load.context.openSourceDialog();
  await load.context.loadPath('/synthetic/trace.jsonl');
  assert.equal(load.calls[1].url, '/api/session/path');
  assert.equal(JSON.parse(load.calls[1].options.body).path, '/synthetic/trace.jsonl');
  assert(!load.get('source-dialog').open);
  assert(!load.state.sourceBusy);

  const failure = fixture();
  failure.context.openSourceDialog();
  failure.get('session-path').value = '/synthetic/missing.jsonl';
  failure.responses([new Error('Synthetic source unavailable')]);
  await failure.context.loadPath(failure.get('session-path').value);
  assert(failure.get('source-dialog').open);
  assert.equal(failure.get('source-dialog-status').textContent, 'Synthetic source unavailable');
  assert(failure.get('source-dialog-status').classList.contains('error-text'));
  assert(!failure.get('source-dialog-status').hidden);
  assert.equal(failure.get('session-path').value, '/synthetic/missing.jsonl');
  assert(!failure.get('session-path').disabled);
  assert.equal(failure.applied(), 0);

  const upload = fixture();
  upload.context.openSourceDialog();
  upload.responses([{ traces: [{ session_id: 'synthetic-uploaded' }] }, new Error('Synthetic second file failed')]);
  await upload.context.uploadFiles([{ name: 'one.jsonl' }, { name: 'two.jsonl' }]);
  assert.equal(upload.calls.length, 2);
  assert(upload.calls.every((call) => call.url === '/api/session/upload'));
  assert.equal(upload.applied(), 1, 'Successful files still load when a later file fails.');
  assert(upload.get('source-dialog').open, 'Partial failure stays visible in the popup.');
  assert(upload.get('source-dialog-status').textContent.includes('two.jsonl'));
  await upload.context.uploadFiles([{ name: 'retry.jsonl' }]);
  assert(!upload.get('source-dialog').open);
  assert(!upload.state.sourceBusy);

  const offline = fixture({ interactive: false });
  offline.context.openSourceDialog();
  assert(offline.get('source-dialog').open);
  assert(!offline.get('source-loader-status').hidden);
  for (const id of ['session-id', 'session-path', 'load-session-id-button', 'load-path-button', 'upload-button']) assert(offline.get(id).disabled);
  assert.equal(offline.document.activeElement, offline.get('source-dialog-close'));
  assert.equal(offline.calls.length, 0);

  const run = fixture();
  run.context.renderConversationRunDetails();
  assert(run.get('qa-run-details').hidden);
  run.state.agentRun = { run_id: 'synthetic-run', status: 'paused' };
  run.get('agent-run-recovery').hidden = false;
  run.context.renderConversationRunDetails();
  assert(!run.get('qa-run-details').hidden && run.get('qa-run-details').open);
  assert(run.get('qa-run-summary').textContent.includes('Action needed'));
  run.get('qa-run-details').open = false;
  run.context.renderConversationRunDetails();
  assert(!run.get('qa-run-details').open, 'Polling respects the user collapsing an already-shown recovery card.');
  run.state.agentRun.status = 'blocked';
  run.context.renderConversationRunDetails();
  assert(run.get('qa-run-details').open, 'New recovery state is revealed.');

  const chat = fixture();
  const body = chat.get('qa-conversation-body');
  body.scrollHeight = 1000;
  body.clientHeight = 200;
  body.scrollTop = 100;
  chat.context.renderQA();
  assert.equal(body.scrollTop, 100, 'Polling does not pull readers away from prior messages or recovery controls.');
  body.scrollTop = 790;
  chat.context.renderQA();
  assert.equal(body.scrollTop, 1000, 'Readers near the bottom follow new output in the new scroll container.');
  body.scrollTop = 0;
  chat.context.scrollQAConversationToBottom();
  assert.equal(body.scrollTop, 1000);
  assert.equal(chat.get('qa-messages').scrollTop, 0, 'Messages no longer own the nested scroll position.');

  console.log('Canonical and legacy chat layout, source popup, loading, errors, recovery, and scroll checks passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
