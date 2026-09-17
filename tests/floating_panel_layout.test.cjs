'use strict';

// Exercise the real session-brief renderer and layout lifecycle with synthetic
// geometry. The body becomes header-only as soon as the renderer hides content.
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

function fixture({ height = '', collapsed = false, savedHeight = null, headerHeight = 100, mobileHeaderHeight = headerHeight } = {}) {
  const nodes = {};
  const events = {};
  const frames = [];
  const storage = new Map();
  let stacked = false;
  function node(id) {
    const classes = new Set();
    return nodes[id] = {
      id, hidden: false, dataset: {}, attributes: {}, children: [],
      style: { height: '', removeProperty(key) { this[key] = ''; } },
      classList: {
        add: (key) => classes.add(key),
        contains: (key) => classes.has(key),
        toggle: (key, value) => value ? classes.add(key) : classes.delete(key),
      },
      setAttribute(key, value) { this.attributes[key] = value; },
      querySelectorAll: () => [],
      appendChild(child) { this.children.push(child); },
      removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
      get firstChild() { return this.children[0]; },
    };
  }
  const workspace = node('floating-workspace');
  workspace.clientWidth = 1280;
  workspace.getBoundingClientRect = () => ({ left: 0, top: 0 });
  const panel = node('session-brief-panel');
  const content = node('session-brief-content');
  const toggle = node('session-brief-toggle');
  for (const id of ['status', 'button', 'auto-field', 'auto']) node('session-brief-' + id);
  panel.style.height = height;
  panel.dataset.floatingReady = 'true';
  panel.offsetTop = 16;
  const currentHeaderHeight = () => stacked ? mobileHeaderHeight : headerHeight;
  panel.querySelector = () => ({
    getBoundingClientRect: () => ({ height: currentHeaderHeight() }),
    querySelector: () => toggle,
  });
  panel.getBoundingClientRect = () => ({
    left: 16, top: panel.offsetTop, width: 1200,
    height: panel.classList.contains('floating-panel-collapsed') ? currentHeaderHeight() + 2
      : !stacked && Number.parseFloat(panel.style.height)
        || Math.max(100, currentHeaderHeight() + 2 + (content.hidden ? 0 : content.bodyHeight || 0)),
  });
  Object.defineProperty(panel, 'offsetHeight', { get: () => panel.getBoundingClientRect().height });
  Object.defineProperty(panel, 'clientHeight', { get: () => panel.offsetHeight - 2 });
  const state = {
    floatingPanelsReady: true,
    floatingPanelLayout: { panels: { [panel.id]: { left: 16, top: 16, width: 1200, height: savedHeight, z: 10 } } },
    sessionBriefCollapsed: collapsed,
    sessionBrief: { session_id: 'synthetic', available: true, result: { bodyHeight: 600 } },
  };
  const context = vm.createContext({
    state, runtime: { interactive: true, agent: {}, live: {}, qa: {} },
    floatingPanelLayoutStorageKey: 'synthetic-layout', floatingPanelStackMedia: '(max-width: 780px)',
    floatingPanelZCounter: 20,
    document: { getElementById: (id) => nodes[id], querySelectorAll: () => [panel] },
    window: {
      matchMedia: () => ({ matches: stacked }),
      getComputedStyle: () => ({ paddingLeft: '0', paddingTop: '0', paddingRight: '0', paddingBottom: '0', minHeight: '100px' }),
      requestAnimationFrame: (callback) => { frames.push(callback); return frames.length; },
      cancelAnimationFrame: () => {}, addEventListener: (event, callback) => { events[event] = callback; },
      localStorage: { setItem: (key, value) => storage.set(key, value) },
    },
    bindFloatingPanel: () => {},
    selectedTrace: () => ({ session_id: 'synthetic', events: [] }),
    formatInteger: String,
    renderCheckpointResult: (target, result) => { target.bodyHeight = result.bodyHeight; },
  });
  vm.runInContext([
    'clear', 'bounded', 'renderCollapseToggle', 'syncFloatingPanelCollapse', 'renderSessionBrief',
    'floatingPanelElements', 'floatingWorkspaceBounds', 'constrainedFloatingPanelGeometry',
    'applyFloatingPanelGeometry', 'floatingPanelCurrentGeometry', 'updateFloatingWorkspaceHeight',
    'saveFloatingPanelLayout', 'resetFloatingPanelLayout', 'ensureFloatingPanelPlaced', 'setupFloatingPanels',
    ...(source.includes('function recoverFloatingPanelHeight(') ? ['recoverFloatingPanelHeight'] : []),
  ].map(functionSource).join('\n'), context);
  const render = (value) => {
    state.sessionBriefCollapsed = value;
    context.renderSessionBrief();
    while (frames.length) frames.shift()();
  };
  const resize = (mobile) => {
    stacked = mobile;
    events.resize();
    while (frames.length) frames.shift()();
  };
  const saved = () => {
    context.saveFloatingPanelLayout();
    return JSON.parse(storage.get('synthetic-layout')).panels[panel.id].height;
  };
  return { context, state, panel, content, toggle, render, saved, resize };
}

const auto = fixture();
auto.render(false);
assert.equal(auto.panel.offsetHeight, 702);
for (let cycle = 0; cycle < 3; cycle += 1) {
  auto.render(true);
  assert.equal(auto.panel.offsetHeight, 102);
  assert.equal(auto.toggle.attributes['aria-expanded'], 'false');
  assert.equal(auto.saved(), null, 'Collapsing must not persist the header height.');
  auto.render(false);
  assert.equal(auto.panel.offsetHeight, 702, 'The whole brief is visible again.');
  assert.equal(auto.panel.style.height, '', 'Automatic sizing survives repeated toggles.');
  assert.equal(auto.toggle.attributes['aria-expanded'], 'true');
}
auto.render(true);
auto.state.sessionBrief.result.bodyHeight = 1000;
auto.render(true);
auto.render(false);
assert.equal(auto.panel.offsetHeight, 1102, 'A refreshed brief can grow while collapsed.');

const manual = fixture({ height: '640px', savedHeight: 640 });
manual.render(false);
manual.render(true);
assert.equal(manual.saved(), 640, 'Saving a collapsed panel preserves a manual height.');
manual.render(false);
assert.equal(manual.panel.offsetHeight, 640);

for (const savedHeight of [100, 102]) {
  const legacy = fixture({ height: `${savedHeight}px`, savedHeight });
  legacy.render(false);
  assert.equal(legacy.panel.offsetHeight, 702, 'Previously saved header-only heights recover.');
  assert.equal(legacy.saved(), null);
}

for (const headerHeight of [76, 94, 100]) {
  const legacyReload = fixture({ savedHeight: Math.max(100, headerHeight + 2), headerHeight });
  legacyReload.state.floatingPanelsReady = false;
  legacyReload.render(false);
  legacyReload.context.setupFloatingPanels();
  assert.equal(legacyReload.panel.style.height, '', 'Recovery runs after saved geometry is applied on startup.');
  assert.equal(legacyReload.panel.offsetHeight, headerHeight + 602);
}

const wrapped = fixture({ height: '200px', savedHeight: 200, mobileHeaderHeight: 260 });
wrapped.state.floatingPanelsReady = false;
wrapped.render(false);
wrapped.context.setupFloatingPanels();
wrapped.resize(true);
wrapped.render(true);
wrapped.render(false);
assert.equal(wrapped.panel.style.height, '200px', 'A tall mobile header must not discard a valid desktop height.');
wrapped.resize(false);
assert.equal(wrapped.panel.offsetHeight, 200);

for (const height of ['', '640px']) {
  const reloaded = fixture({ height, collapsed: true, savedHeight: Number.parseFloat(height) || null });
  reloaded.state.floatingPanelsReady = false;
  reloaded.render(true);
  reloaded.context.setupFloatingPanels();
  reloaded.resize(false);
  assert.equal(reloaded.panel.style.height, height, 'Desktop resize preserves expanded sizing while collapsed.');
  reloaded.resize(true);
  reloaded.render(false);
  reloaded.render(true);
  reloaded.resize(false);
  reloaded.render(false);
  assert.equal(reloaded.panel.style.height, height, 'Mobile toggles preserve the desktop sizing mode.');
  assert.equal(reloaded.panel.offsetHeight, height ? 640 : 702);
}

const reset = fixture({ height: '640px', savedHeight: 640 });
reset.render(true);
reset.context.resetFloatingPanelLayout({ clearSaved: false });
assert.equal(reset.saved(), null, 'Resetting a collapsed panel restores automatic height.');
reset.render(false);
assert.equal(reset.panel.offsetHeight, 702);

console.log('Floating panel collapse, restore, persistence, and responsive layout checks passed.');
